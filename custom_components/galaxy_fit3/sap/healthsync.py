"""Health sync protocol on route 0a0a (SAP service 10) -- binary, little-endian, plaintext.

Every message starts with one header byte: bit 7 = frame type, bit 6 = message type (0 request,
1 response), low six bits = message id. The ids used here:

==== ======================= ==========================================================
id   name                    bytes seen / sent
==== ======================= ==========================================================
32   capability              watch ``a0`` (request, ~875 bytes in two SAP fragments);
                             phone answers ``e0`` (:data:`CAPABILITY_RESPONSE`)
34   GM capability           watch ``a2`` -> phone ``e2`` (handled in :mod:`.reactive`)
3    REQUEST_DATA            ``03`` request / ``43`` response; no parameter-count byte
2    SYNC_DATA               ``82`` request / ``42`` response; carries the health records
1    CHECK_STATUS            ``01`` / ``41``
4    WEARABLE_MSG            ``04`` / ``44`` (exercise status and similar; not health records)
==== ======================= ==========================================================

The builders reproduce the exact bytes the watch expects, and the record layouts parse to the values
asserted in ``tests/test_healthsync.py``. A sync runs in this order: REQUEST_DATA from the phone, the
watch's response, the phone's SYNC_DATA, the watch's response, the watch's SYNC_DATA, the phone's
response.

Times are unsigned 32-bit little-endian seconds since 1990-01-01 00:00:00 UTC.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

ROUTE_HEALTH = b"\x0a\x0a"

# Seconds between the Unix epoch and the protocol's 1990-01-01 UTC epoch.
EPOCH_OFFSET = 631_152_000

OP_CAPABILITY_REQUEST = 0xA0  # watch -> phone
OP_CAPABILITY_RESPONSE = 0xE0  # phone -> watch
OP_REQUEST_DATA = 0x03
OP_REQUEST_DATA_RESPONSE = 0x43
OP_SYNC_DATA = 0x82
OP_SYNC_DATA_RESPONSE = 0x42
OP_CHECK_STATUS = 0x01

TAG_SEQUENCE = 0xE0
TAG_MESSAGE_INFO = 0x04
TAG_DEVICE_INFO = 0x05
TAG_STEP_COUNT = 0x07
TAG_HEART_RATE = 0x0A

RESULT_SUCCESS = 0x01  # result code: success

# The phone's reply to the watch's capability request: the capability values a health app sends
# (protocol 4.51, sender "shealth", receiver "wearable", binning pedometer 4, sync address, config os
# "android", wearable messages 5.03 / gzip / 10000 bytes, launch support, and a tracker-feature block).
# It sends no feature list and no data manifest. The four bytes after ``a2`` are the app's version code.
HEALTH_APP_VERSION_CODE = 7_013_005
_CAPABILITY_RESPONSE_HEAD = bytes.fromhex("e00ba10433c002c100c202a2")
_CAPABILITY_RESPONSE_TAIL = bytes.fromhex(
    "b101ae01040300ac03ad0201c301010301020503041027"
    "b315030d017017020a000308000ce9030afc0301010100"
)


def build_capability_response(health_app_version_code: int = HEALTH_APP_VERSION_CODE) -> bytes:
    """The ``e0`` capability response the watch waits for after its ``a0`` capability request."""
    return (
        _CAPABILITY_RESPONSE_HEAD
        + struct.pack("<I", health_app_version_code)
        + _CAPABILITY_RESPONSE_TAIL
    )


OP_LARGE_DATA_CHUNK = 0x3F  # message id 63: one chunk of a message too big for a single health message
OP_LARGE_DATA_ACK = 0x3E  # message id 62: acknowledgement after the last chunk


class LargeDataReceiver:
    """Reassemble a health message the watch sends as large-data chunks.

    Chunk layout: ``3f <flags> <chunk number u32 LE> <body>``; ``flags & 3`` is 0 first, 1 middle,
    2 last. After the last chunk the receiver acknowledges with ``3e 09 <chunk count u32 LE>``.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def reset(self) -> None:
        self._chunks.clear()

    def feed(self, app: bytes) -> tuple[bytes, bytes] | None:
        """Feed one chunk. Returns ``(whole message, acknowledgement)`` after the last chunk."""
        if len(app) < 6:
            return None
        position = app[1] & 0x03
        number = struct.unpack_from("<I", app, 2)[0]
        if position == 0:
            self._chunks = [app[6:]]
            return None
        if number != len(self._chunks):
            return None  # a repeat or an out-of-order chunk: ignore it
        self._chunks.append(app[6:])
        if position != 2:
            return None
        whole = b"".join(self._chunks)
        ack = bytes([OP_LARGE_DATA_ACK, 0x09]) + struct.pack("<I", len(self._chunks))
        self._chunks = []
        return whole, ack


def to_protocol_time(unix_seconds: float) -> int:
    return max(0, int(unix_seconds) - EPOCH_OFFSET) & 0xFFFFFFFF


def from_protocol_time(value: int) -> int:
    """Protocol seconds -> Unix seconds."""
    return value + EPOCH_OFFSET


def _seq(seq: int) -> bytes:
    return bytes([TAG_SEQUENCE]) + struct.pack("<H", seq & 0xFFFF)


def _message_info(result: int) -> bytes:
    return bytes([TAG_MESSAGE_INFO, 0x02, 0x01, result & 0xFF])


def _device_info(last_sync: int) -> bytes:
    """Device-info block as a health app fills it: device 0, type 0, protocol version 4.51, the
    time it last received data from this watch (protocol seconds, 0 = never), no start/end window,
    last chunk."""
    body = (
        b"\x01\x00\x02\x00\x00\x03\x04\x33"
        + b"\x04" + struct.pack("<I", last_sync)
        + b"\x05" + bytes(4)
        + b"\x06" + bytes(4)
        + b"\x07\x01"
    )
    return bytes([TAG_DEVICE_INFO, len(body)]) + body


def build_sync_ack(seq: int, last_sync: int = 0, result: int = RESULT_SUCCESS) -> bytes:
    """Response to a watch SYNC_DATA (``82``): ``42 03 e0 <seq> 04 02 01 <result> 05 ...``."""
    return bytes([OP_SYNC_DATA_RESPONSE, 0x03]) + _seq(seq) + _message_info(result) + _device_info(last_sync)


def build_request_data_response(seq: int, last_sync: int = 0) -> bytes:
    """Response to a watch REQUEST_DATA (``03``)."""
    return bytes([OP_REQUEST_DATA_RESPONSE]) + _seq(seq) + _message_info(RESULT_SUCCESS) + _device_info(last_sync)


def build_request_data(seq: int, last_sync: int = 0) -> bytes:
    """Phone-initiated REQUEST_DATA (``03``): stage one of a sync. ``last_sync`` tells the watch
    what the phone already has."""
    return bytes([OP_REQUEST_DATA]) + _seq(seq) + _device_info(last_sync)


def build_mobile_sync_data(seq: int, last_sync: int = 0, profile: bytes = b"") -> bytes:
    """Phone-initiated SYNC_DATA (``82``): stage two of a sync, sent after the watch has answered the
    REQUEST_DATA. It carries no phone-side records unless ``profile`` (a :func:`build_user_profile`
    block) is given; the second byte counts the blocks: sequence, device info and the profile."""
    return bytes([OP_SYNC_DATA, 0x03 if profile else 0x02]) + _seq(seq) + _device_info(last_sync) + profile


DISTANCE_UNITS = {"km": 1, "mi": 2}
PROFILE_BLOCK = 0x0B  # USER_PROFILE (data type 11)
PROFILE_UPDATE_TIME = 0x05
PROFILE_DISTANCE_UNIT = 0x07


def build_user_profile(distance_unit: str, update_time: int) -> bytes:
    """A user-profile block that sets only the distance unit .

    ``0b <u32 length>`` then the update time (``05`` + protocol seconds, which the watch uses to decide
    whether the profile is newer than its own), then ``<u32 length>`` and the entries, here just
    ``07 <1 = km, 2 = mi>``. Entries that are left out are not touched. The layout is the one a phone health app
    sends; whether the watch applies it from us is checked live.
    """
    try:
        unit = DISTANCE_UNITS[distance_unit]
    except KeyError:
        raise ValueError(f"distance unit must be one of {sorted(DISTANCE_UNITS)}, not {distance_unit!r}") from None
    entries = bytes([PROFILE_DISTANCE_UNIT, unit])
    body = bytes([PROFILE_UPDATE_TIME]) + struct.pack("<I", update_time & 0xFFFFFFFF) + struct.pack("<I", len(entries)) + entries
    return bytes([PROFILE_BLOCK]) + struct.pack("<I", len(body)) + body


def build_check_status(seq: int) -> bytes:
    """Phone-initiated CHECK_STATUS: ``01 01 e0 <seq>``."""
    return bytes([OP_CHECK_STATUS, 0x01]) + _seq(seq)


TRACKER_HEART_RATE = 1
TRACKER_SPO2 = 2
TRACKER_STRESS = 3
DEVICE_TYPE_FIT3 = 10060


def build_on_demand_measure(seq: int, tracker: int = TRACKER_HEART_RATE, version: int = 1000) -> bytes:
    """Wearable message asking the watch to take a measurement now ("measure on watch"). Layout: ``04`` + seq + header (``19``: receiver 5 = remote deep link, device type,
    message version 5.03) + body (``1a`` + u32 length: message 6 = "measurement", action 7 = VIEW,
    ``95`` version, ``96`` tracker id). ``version`` is the watch's advertised measure-on-watch
    version; what a health app really sends there is not known.
    """
    body = bytes([0x80, 0x06, 0x81, 0x07, 0x95]) + struct.pack("<H", version) + bytes([0x96, tracker & 0xFF])
    return (
        b"\x04" + _seq(seq)
        + bytes([0x19, 0x85, 0x05, 0x02]) + struct.pack("<H", DEVICE_TYPE_FIT3) + bytes([0x03, 0x05, 0x03])
        + b"\x1a" + struct.pack("<I", len(body)) + body
    )


@dataclass(slots=True)
class StepRecord:
    start: int | None = None  # Unix seconds
    end: int | None = None
    update: int | None = None
    count: int | None = None
    distance: float | None = None  # metres
    speed: float | None = None
    calorie: float | None = None  # kcal
    run_steps: int | None = None
    walk_steps: int | None = None
    duration: int | None = None  # milliseconds


TAG_RESTING_HEART_RATE = 21316  # a heart-rate record with this tag is a resting heart rate

@dataclass(slots=True)
class HeartRateRecord:
    start: int | None = None  # Unix seconds
    end: int | None = None
    update: int | None = None
    bpm: int | None = None
    bpm_min: int | None = None
    bpm_max: int | None = None
    tag: int | None = None  # TAG_RESTING_HEART_RATE marks a resting heart rate


@dataclass(slots=True)
class DeviceInfo:
    device_type: int | None = None
    last_sync: int | None = None  # protocol seconds, as received (echoed back verbatim)
    start: int | None = None
    end: int | None = None
    last_chunk: bool = True


@dataclass(slots=True)
class SyncData:
    op: int
    seq: int | None = None
    result: int | None = None
    device_info: DeviceInfo | None = None
    steps: list[StepRecord] = field(default_factory=list)
    heart_rates: list[HeartRateRecord] = field(default_factory=list)
    # First data-type tag this parser does not know. Records are not length-prefixed in general, so
    # parsing stops there; anything after it in the message is not decoded.
    stopped_at_tag: int | None = None
    stopped_at_offset: int | None = None


_STEP_FIELD_COUNT = 12  # FrameFieldCount.PEDO_STEP_COUNT


def _u32(b: bytes, i: int) -> int:
    return struct.unpack_from("<I", b, i)[0]


def _f32(b: bytes, i: int) -> float:
    return struct.unpack_from("<f", b, i)[0]


def _parse_step_record(b: bytes, i: int) -> tuple[StepRecord, int]:
    rec = StepRecord()
    for _ in range(_STEP_FIELD_COUNT):
        tag = b[i]
        i += 1
        if tag == 23:  # version code, one byte
            i += 1
            continue
        if tag in (1, 2, 5):
            value = from_protocol_time(_u32(b, i))
            setattr(rec, {1: "start", 2: "end", 5: "update"}[tag], value)
        elif tag == 3:
            pass  # time offset
        elif tag == 16:
            rec.count = _u32(b, i)
        elif tag == 17:
            rec.distance = _f32(b, i)
        elif tag == 18:
            rec.speed = _f32(b, i)
        elif tag == 19:
            rec.calorie = _f32(b, i)
        elif tag == 20:
            rec.run_steps = _u32(b, i)
        elif tag == 21:
            rec.walk_steps = _u32(b, i)
        elif tag == 22:
            rec.duration = _u32(b, i)
        else:
            raise ValueError(f"unknown step-count field {tag:#04x}")
        i += 4
    return rec, i


def _parse_heart_rate_record(b: bytes) -> HeartRateRecord:
    rec = HeartRateRecord()
    i = 0
    while i < len(b):
        tag = b[i]
        i += 1
        if tag in (1, 2, 5):
            setattr(rec, {1: "start", 2: "end", 5: "update"}[tag], from_protocol_time(_u32(b, i)))
            i += 4
        elif tag in (3, 151, 153):  # time offset, create / modify versions
            i += 4
        elif tag in (32, 33, 34, 35):
            name = {32: "bpm", 33: "bpm_min", 34: "bpm_max"}.get(tag)
            if name:
                setattr(rec, name, b[i])
            i += 1
        elif tag == 36:  # binning: byte length, then 4-byte bins
            i += 4 + _u32(b, i)
        elif tag == 37:
            rec.tag = struct.unpack_from("<H", b, i)[0]
            i += 2
        else:
            raise ValueError(f"unknown heart-rate field {tag:#04x}")
    return rec


def _parse_device_info(b: bytes) -> DeviceInfo:
    info = DeviceInfo()
    i = 0
    while i < len(b):
        tag = b[i]
        i += 1
        if tag in (1, 7):
            if tag == 7:
                info.last_chunk = b[i] != 0
            i += 1
        elif tag in (2, 3):
            if tag == 2:
                info.device_type = struct.unpack_from("<H", b, i)[0]
            i += 2
        elif tag in (4, 5, 6, 8):
            name = {4: "last_sync", 5: "start", 6: "end"}.get(tag)
            if name:
                setattr(info, name, _u32(b, i))
            i += 4
        else:
            raise ValueError(f"unknown device-info field {tag:#04x}")
    return info


def parse_sync_message(app: bytes) -> SyncData:
    """Parse a SYNC_DATA (``82``/``42``) or REQUEST_DATA (``03``/``43``) message.

    Decodes the sequence number, message info, device info, step-count and heart-rate records. Stops
    at the first data type it does not know (see :attr:`SyncData.stopped_at_tag`); truncated or
    malformed input raises ``ValueError``.
    """
    if not app:
        raise ValueError("empty health message")
    out = SyncData(op=app[0])
    # REQUEST_DATA has no parameter-count byte; SYNC_DATA has one after the header.
    i = 1 if (app[0] & 0x3F) == 3 else 2
    try:
        while i < len(app):
            tag = app[i]
            i += 1
            if tag == TAG_SEQUENCE:
                out.seq = struct.unpack_from("<H", app, i)[0]
                i += 2
            elif tag == TAG_MESSAGE_INFO:
                n = app[i]
                block = app[i + 1 : i + 1 + n]
                if len(block) >= 2 and block[0] == 0x01:
                    out.result = block[1]
                i += 1 + n
            elif tag == TAG_DEVICE_INFO:
                n = app[i]
                out.device_info = _parse_device_info(app[i + 1 : i + 1 + n])
                i += 1 + n
            elif tag == TAG_STEP_COUNT:
                while True:  # [has-next][record], repeated while has-next is non-zero
                    has_next = app[i]
                    rec, i = _parse_step_record(app, i + 1)
                    out.steps.append(rec)
                    if not has_next:
                        break
            elif tag == TAG_HEART_RATE:
                while True:  # [has-next][byte length u32][record], repeated while has-next is non-zero
                    has_next = app[i]
                    n = _u32(app, i + 1)
                    block = app[i + 5 : i + 5 + n]
                    if len(block) != n:
                        raise ValueError("truncated heart-rate record")
                    out.heart_rates.append(_parse_heart_rate_record(block))
                    i += 5 + n
                    if not has_next:
                        break
            else:
                out.stopped_at_tag = tag
                out.stopped_at_offset = i - 1
                break
    except (IndexError, struct.error) as err:
        raise ValueError(f"truncated health message at offset {i}") from err
    return out


def sequence_of(app: bytes) -> int | None:
    """The message's sequence number (``e0`` tag right after the header), or ``None``."""
    for i in (1, 2):
        if len(app) >= i + 3 and app[i] == TAG_SEQUENCE:
            return struct.unpack_from("<H", app, i + 1)[0]
    return None
