"""SAP app frame codec for the BLE pipe (battery / watch-face features).

On-wire layout (solved from a real capture)::

    [H0][H1][H2][H3] [B4][B5] [ROUTE(2)] [APP ...] [CRC(2)]
    \\__ 4-byte header _/ \\______ CRC-covered region (H1 bytes) ______/

``H0:H1`` is a 16-bit big-endian length of the CRC-covered region (``len(APP) + 4``; so ``H0`` is
0x00 for anything under 256 bytes and 0x01 for the ~270-500 byte watch-face catalog and device-info
frames -- it is NOT a "reliable" flag). ``H2:H3`` is CRC-16/ARC of ``H0 H1``, big-endian (verified
against every distinct length in two captured pairing sessions). The trailing CRC is CRC-16/ARC over
``frame[4:-2]``, big-endian. The 105-byte hello frames start with 0x68 and follow none of this.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

ROUTE_BATTERY = b"\x0b\x0b"
ROUTE_SETTINGS = ROUTE_BATTERY  # service 11 (full settings): battery requests and the phone's real set-time (msg id 0)
ROUTE_WATCHFACE = b"\x06\x06"
ROUTE_TIME = b"\x01\x01"  # service 1: the phone's time frame (and OOBE / device info)

# -- SAP connection "hello" (frame class 0x68) -----------------------------------------------
# After the LE-SC bond the watch announces itself on the pipe with a 105-byte hello
# (``68 14 ...`` carrying an ASCII marker string + its name), and the host must reply with its
# own 105-byte hello (``68 15 ...`` carrying ``SAP_<32-hex id>`` + its name) before the watch will
# start a SAP session. Layout (both directions are 105 bytes):
#   [0:7]  7-byte header (host reply = 68 15 04 01 00 ff 03)
#   [7:43] "SAP_" + 32 hex chars (36 bytes, the host's SAP id)
#   [43:50] version/MTU block 01 01 f4 00 00 03 d4
#   [50:66][66:82][82:98] three 16-byte NUL-padded name fields (name / manufacturer / model)
#   [98:104] 6-byte MAC (the phone sent all 0xFF, accepted)
#   [104] trailing 0x02
HELLO_CLASS = 0x68
HELLO_ANNOUNCE = 0x14  # watch -> host (marker string + name)
HELLO_REPLY = 0x15  # host -> watch (SAP_<id>)
_HELLO_HDR = bytes([0x68, 0x15, 0x04, 0x01, 0x00, 0xFF, 0x03])
_HELLO_VER = bytes.fromhex("0101f4000003d4")
HELLO_LEN = 105


def is_hello_announce(data: bytes) -> bool:
    """True if ``data`` is the watch's hello announce."""
    return len(data) >= 2 and data[0] == HELLO_CLASS and data[1] == HELLO_ANNOUNCE


def sap_id_from_seed(seed: str) -> str:
    """Derive a stable 32-hex SAP id from a seed (e.g. the HA adapter address)."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32].upper()


def _name_field(s: str) -> bytes:
    b = s.encode("ascii", "ignore")[:16]
    return b + b"\x00" * (16 - len(b))


def build_sap_hello(
    sap_id_hex: str,
    name: str = "Home Assistant",
    manufacturer: str = "Home Assistant",
    model: str = "Home Assistant",
    mac: bytes = b"\xff" * 6,
) -> bytes:
    """Build the host's 105-byte ``SAP_<id>`` hello reply (frame class 0x68, type 0x15)."""
    sid = ("SAP_" + sap_id_hex).encode("ascii")
    if len(sid) != 36:
        raise ValueError("sap_id_hex must be 32 hex chars")
    frame = (
        _HELLO_HDR
        + sid
        + _HELLO_VER
        + _name_field(name)
        + _name_field(manufacturer)
        + _name_field(model)
        + mac[:6].ljust(6, b"\xff")
        + b"\x02"
    )
    assert len(frame) == HELLO_LEN, len(frame)
    return frame


def crc16_arc(data: bytes) -> int:
    """CRC-16/ARC (poly 0x8005 reflected = 0xA001, init 0, xorout 0)."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc


@dataclass(slots=True)
class AppFrame:
    h2: int
    h3: int
    b4: int
    b5: int
    route: bytes
    app: bytes


def header_checksum(h0: int, h1: int) -> int:
    """The ``H2:H3`` header bytes: CRC-16/ARC of the two length bytes."""
    return crc16_arc(bytes([h0, h1]))


def encode_app_frame(h2: int, h3: int, b4: int, b5: int, route: bytes, app: bytes) -> bytes:
    """Encode an app frame with explicit ``H2 H3`` bytes (see :func:`build_frame` to compute them)."""
    assert len(route) == 2
    region = bytes([b4, b5]) + route + app
    n = len(region)
    if n > 0xFFFF:
        raise ValueError("frame too large")
    frame_wo_crc = bytes([(n >> 8) & 0xFF, n & 0xFF, h2, h3]) + region
    crc = crc16_arc(frame_wo_crc[4:])
    return frame_wo_crc + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def build_frame(b4: int, b5: int, route: bytes, app: bytes) -> bytes:
    """Build an app frame of any size, computing the length and the ``H2:H3`` header checksum."""
    n = len(app) + 4
    c = header_checksum((n >> 8) & 0xFF, n & 0xFF)
    return encode_app_frame((c >> 8) & 0xFF, c & 0xFF, b4, b5, route, app)


def is_reply(request_op: int, rx_op: int) -> bool:
    """True if ``rx_op`` (first app byte of a frame) answers ``request_op`` on the same route.

    A reply carries the request's low six bits with bit 0x40 set (``02``->``42``, ``0c``->``4c``,
    ``a2``->``e2``) or, for the big route-0606 answers, ``c0``/``c1`` to requests ``00``/``01``.
    """
    return bool(rx_op & 0x40) and ((rx_op ^ request_op) & 0x3F) == 0


HELLO_FRAME_LEN = 105


class FrameAssembler:
    """Reassemble the watch's byte stream into whole frames.

    GATT notifications are not guaranteed to line up with frames: a frame can be split across
    notifications (small MTU) or several can arrive back to back. ``H0:H1 + 6`` is a frame's length;
    the 105-byte ``0x68`` hello frames are fixed-size. Garbage resynchronises by dropping the buffer.
    """

    MAX_FRAME = 4096

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        while len(self._buf) >= 2:
            if self._buf[0] == HELLO_CLASS:
                need = HELLO_FRAME_LEN
            else:
                need = ((self._buf[0] << 8) | self._buf[1]) + 6
            if need > self.MAX_FRAME or need < 10 and self._buf[0] != HELLO_CLASS:
                self._buf.clear()  # not a frame boundary; resync on the next notification
                break
            if len(self._buf) < need:
                break
            out.append(bytes(self._buf[:need]))
            del self._buf[:need]
        return out


# SAP fragments a message that does not fit one 500-byte frame. ``B4`` bit 0x02 marks a frame as
# part of a fragmented message and bit 0x04 marks a continuation; a continuation has NO route, so the
# two bytes at the route position are payload. Seen on the wire (the watch's ~875-byte health
# capability on route 0a0a): first ``02`` + route + 490 bytes, last ``06`` + 385 bytes. A message
# longer than two frames has not been captured; a middle frame is assumed to be ``04``.
FRAG_FLAG = 0x02
FRAG_CONTINUATION = 0x04


class MessageReassembler:
    """Join SAP-fragmented app frames back into one message (see :data:`FRAG_FLAG`)."""

    MAX_MESSAGE = 65536

    def __init__(self) -> None:
        self._head: AppFrame | None = None
        self._buf = bytearray()

    def reset(self) -> None:
        self._head = None
        self._buf.clear()

    def feed(self, fr: AppFrame) -> AppFrame | None:
        """Return a complete message frame, or ``None`` while a fragmented one is still arriving."""
        if fr.b4 & 0x80:  # transport ACK, never fragmented
            return fr
        frag = fr.b4 & (FRAG_FLAG | FRAG_CONTINUATION)
        if frag == 0:
            return fr
        if frag == FRAG_FLAG:  # first fragment
            self._head = fr
            self._buf = bytearray(fr.app)
            return None
        if self._head is None:  # continuation with no start: drop it
            return None
        self._buf += fr.route + fr.app
        if len(self._buf) > self.MAX_MESSAGE:
            self.reset()
            return None
        if frag == FRAG_CONTINUATION:  # middle fragment (assumed encoding)
            return None
        head, app = self._head, bytes(self._buf)
        self.reset()
        return AppFrame(h2=head.h2, h3=head.h3, b4=head.b4 & ~0x06, b5=head.b5, route=head.route, app=app)


def decode_app_frame(data: bytes) -> AppFrame:
    """Parse a received app frame, verifying the CRC (``ValueError('bad CRC')`` on mismatch)."""
    if len(data) < 10:
        raise ValueError("frame too short")
    crc = crc16_arc(data[4:-2])
    if bytes([(crc >> 8) & 0xFF, crc & 0xFF]) != bytes(data[-2:]):
        raise ValueError("bad CRC")
    return AppFrame(
        h2=data[2], h3=data[3], b4=data[4], b5=data[5], route=bytes(data[6:8]), app=bytes(data[8:-2])
    )


def build_battery_query(window: int = 0) -> bytes:
    b5 = 0x40 | ((window & 1) << 4)
    return build_frame(0x01, b5, ROUTE_BATTERY, b"\x02")


def build_watchface_catalog_request(window: int = 0) -> bytes:
    b5 = 0x20 | ((window & 1) << 4)
    return build_frame(0x01, b5, ROUTE_WATCHFACE, b"\x01")


# Route-0606 commands; the watch replies with op | 0x40.
WATCHFACE_SHOW = 0x03  # show this face (also puts back a removed one); the watch sends it too when changed on the watch
WATCHFACE_REMOVE = 0x05  # takes the face OFF the watch: never use it to switch
WATCHFACE_ORDER = 0x0A  # the phone's face list, shown face first; the phone's app sends it after every change


def watchface_item(face_id: int, group: int) -> bytes:
    """The 4-byte face reference every route-0606 command carries: ``04 <face id> 1d <group>``."""
    return bytes([0x04, face_id & 0xFF, 0x1D, group & 0xFF])


def watchface_show_app(face_id: int, group: int) -> bytes:
    return bytes([WATCHFACE_SHOW]) + watchface_item(face_id, group)


def watchface_order_app(faces: list[tuple[int, int]]) -> bytes:
    """``0a 19 01 <count> <item>...`` for ``(face_id, group)`` pairs, the shown face first."""
    return bytes([WATCHFACE_ORDER, 0x19, 0x01, len(faces)]) + b"".join(watchface_item(f, g) for f, g in faces)


def build_watchface_show(face_id: int, group: int, window: int = 0) -> bytes:
    b5 = 0x20 | ((window & 1) << 4)
    return build_frame(0x01, b5, ROUTE_WATCHFACE, watchface_show_app(face_id, group))


def build_watchface_remove(face_id: int, group: int, window: int = 0) -> bytes:
    """Remove a face from the watch (the frame first mistaken for "select"). Nothing sends it."""
    b5 = 0x20 | ((window & 1) << 4)
    return build_frame(0x01, b5, ROUTE_WATCHFACE, bytes([WATCHFACE_REMOVE]) + watchface_item(face_id, group))


def parse_battery_response(app: bytes) -> int | None:
    """Return the battery percent from ``42|02 06 00 05 <pct>``, else None."""
    if len(app) == 5 and app[1] == 0x06 and app[2] == 0x00 and app[3] == 0x05:
        return app[-1]
    return None


@dataclass(slots=True)
class WatchFace:
    face_id: int
    package: str
    name: str
    group: int = 0  # the face's group id; needed (with face_id) to build a select frame
    active: bool = False  # the face currently shown (``0b 01`` after the group; ``0b 00`` on the others)


def watchface_label(face: "WatchFace") -> str:
    """Human label for a face in the HA dropdown, e.g. ``Face 22`` (names are only ``wf_name-000NN``)."""
    return f"Face {face.face_id}"


def watchface_by_label(faces: list["WatchFace"], label: str) -> "WatchFace | None":
    """Reverse of :func:`watchface_label`: find the catalog face a dropdown label refers to."""
    for face in faces:
        if watchface_label(face) == label:
            return face
    return None


def parse_watchface_catalog(app: bytes) -> list[WatchFace]:
    """Scan for ``05 05 <5 ascii> 06 0E <14 ascii>``; face_id is the byte before the ``05 05``.

    The 14-byte name is NUL-terminated (13 visible chars + ``00``); the NUL is stripped. After the
    name each entry continues ``00 08 00 09 <x> 0a <group> 0b <active> ...``, so the group is the byte
    right after the ``0a`` tag (used by :func:`watchface_item`) and ``0b 01`` marks the face
    currently shown."""
    faces: list[WatchFace] = []
    i = 1
    n = len(app)
    while i + 2 + 5 + 2 + 14 <= n:
        if (
            app[i] == 0x05
            and app[i + 1] == 0x05
            and app[i + 7] == 0x06
            and app[i + 8] == 0x0E
        ):
            pkg = app[i + 2 : i + 7]
            name = app[i + 9 : i + 23]
            if pkg.isascii() and name.isascii():
                # after the name each entry has ``08 <x> 09 <x> 0a <group> 0b ...`` but the ``0a``
                # tag sits at a slightly variable offset, so scan a short window for it.
                group = 0
                active = False
                for j in range(i + 23, min(i + 33, n - 1)):
                    if app[j] == 0x0A:
                        group = app[j + 1]
                        active = j + 3 < n and app[j + 2] == 0x0B and app[j + 3] == 0x01
                        break
                faces.append(
                    WatchFace(
                        app[i - 1],
                        pkg.decode("ascii"),
                        name.decode("ascii").rstrip("\x00"),
                        group,
                        active,
                    )
                )
                i += 23
                continue
        i += 1
    return faces
