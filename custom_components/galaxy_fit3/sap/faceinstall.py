"""Installing a watch face: the route-1e1e announce (LE) and the file transfer (Bluetooth Classic RFCOMM).

Decoded from a capture of a phone installing faces:

1. route 0606 ``04 <item>`` (install request), watch ``44 16 01 <item>``
2. route 1e1e ``01 03 06 04 <len> <filename> 05 <size u32 LE>``, watch ``41 ...``
3. route 1e1e ``02``, watch ``42 ...``: the watch now accepts a Classic connection
4. RFCOMM channel 29: ``30``/``300``, ``33bin,/user/wf/<filename>,<size>``/``330``, the file in blocks of
   :data:`BLOCK_SIZE` bytes each followed by its CRC-32 (little-endian) and acked ``310``, then ``32``/``320``
   (the watch installs it) and ``34``/``340``
5. route 1e1e ``03``, watch ``43 ...``: install session closed

The RFCOMM part is a plain blocking socket, run in an executor thread by the client.
"""

from __future__ import annotations

import errno
import logging
import re
import socket
import struct
import time
import zlib
from collections.abc import Callable, Iterator

_LOGGER = logging.getLogger(__name__)

ROUTE_INSTALL = b"\x1e\x1e"
RFCOMM_CHANNEL = 29  # "SPP SERVICE2", UUID db764ac8-4b08-7f25-aafe-59d03c27bae3 in the watch's SDP record
SPP_UUID = "db764ac8-4b08-7f25-aafe-59d03c27bae3"
BLOCK_SIZE = 39_600  # file bytes per acknowledged block (40 RFCOMM frames of 990 bytes)
REMOTE_DIR = "/user/wf/"

OP_ANNOUNCE = 0x01
OP_OPEN = 0x02
OP_CLOSE = 0x03

_FILENAME = re.compile(r"^SM-R390_(\d{5})_256x402\.bin$")


def face_filename(face_id: int) -> str:
    """The name the watch expects for a face file, e.g. ``SM-R390_00016_256x402.bin``."""
    return f"SM-R390_{face_id:05d}_256x402.bin"


def announce_app(filename: str, size: int) -> bytes:
    """Route-1e1e ``01`` message announcing a file of ``size`` bytes."""
    name = filename.encode("ascii")
    return bytes([OP_ANNOUNCE, 0x03, 0x06, 0x04, len(name)]) + name + b"\x05" + struct.pack("<I", size)


def header(filename: str, size: int) -> bytes:
    """The RFCOMM file header, ``33bin,/user/wf/<filename>,<size>``."""
    return f"33bin,{REMOTE_DIR}{filename},{size}".encode("ascii")


def blocks(data: bytes) -> Iterator[bytes]:
    """The file as sent: each block of up to :data:`BLOCK_SIZE` bytes followed by its CRC-32 (LE)."""
    for start in range(0, len(data), BLOCK_SIZE):
        chunk = data[start : start + BLOCK_SIZE]
        yield chunk + struct.pack("<I", zlib.crc32(chunk))


def unframe(stream: bytes, size: int) -> bytes:
    """Inverse of :func:`blocks` (tests and capture checks): strip and verify the CRCs."""
    out = bytearray()
    pos = 0
    while len(out) < size:
        n = min(BLOCK_SIZE, size - len(out))
        chunk, crc = stream[pos : pos + n], stream[pos + n : pos + n + 4]
        if struct.pack("<I", zlib.crc32(chunk)) != crc:
            raise ValueError(f"bad CRC in block at file offset {len(out)}")
        out += chunk
        pos += n + 4
    return bytes(out)


class TransferError(Exception):
    """The watch answered something other than the expected acknowledgement, or nothing at all."""


class SecurityRefused(TransferError):
    """The Classic link could not be secured with the bond's key (adapter lacks Classic Secure Connections)."""


class RfcommSession:
    """One RFCOMM connection to the watch's file service. Blocking; call from an executor thread."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""

    @classmethod
    def connect(
        cls, local_address: str, watch_address: str, channel: int = RFCOMM_CHANNEL, *, attempts: int = 4, timeout: float = 15.0
    ) -> RfcommSession:
        """Connect from the adapter at ``local_address``; retries a few times (the watch may need a moment
        after the announce before it answers a page)."""
        last: OSError | None = None
        for attempt in range(attempts):
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            sock.settimeout(timeout)
            try:
                sock.bind((local_address, 0))
                sock.connect((watch_address, channel))
                return cls(sock)
            except OSError as err:
                sock.close()
                if err.errno == errno.EACCES:
                    # The kernel refused the link's security, e.g. "Invalid security: Missing AES-CCM usage":
                    # the bond's Classic key is a Secure Connections key and the adapter can only encrypt with
                    # E0 (Bluetooth 4.0 controllers). Retrying cannot help.
                    raise SecurityRefused(
                        "the Bluetooth adapter could not secure the Classic link to the watch (the kernel needs "
                        "AES-CCM encryption for this bond; Bluetooth 4.0 adapters cannot do it on Classic)"
                    ) from err
                last = err
                _LOGGER.debug("RFCOMM connect attempt %d failed: %s", attempt + 1, err)
                time.sleep(1.0)
        raise TransferError(f"could not open the watch's file channel: {last}")

    def close(self) -> None:
        self._sock.close()

    def _read_reply(self, timeout: float) -> bytes:
        """Replies are three ASCII digits (``300``, ``310``...)."""
        self._sock.settimeout(timeout)
        while len(self._buf) < 3:
            data = self._sock.recv(64)
            if not data:
                raise TransferError("the watch closed the file channel")
            self._buf += data
        reply, self._buf = self._buf[:3], self._buf[3:]
        return reply

    def command(self, payload: bytes, expect: bytes, timeout: float = 10.0) -> None:
        self._sock.sendall(payload)
        reply = self._read_reply(timeout)
        if reply != expect:
            raise TransferError(f"sent {payload[:40]!r}, expected {expect!r}, got {reply!r}")

    def send_file(
        self, filename: str, data: bytes, progress: Callable[[int, int], None] | None = None, *, finish_timeout: float = 60.0
    ) -> None:
        """Header, acknowledged blocks, then ``32`` (the watch checks and installs the file)."""
        self.command(header(filename, len(data)), b"330")
        total = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE
        for n, block in enumerate(blocks(data), 1):
            self.command(block, b"310", timeout=15.0)
            if progress:
                progress(n, total)
        self.command(b"32", b"320", timeout=finish_timeout)


def face_id_from_filename(filename: str) -> int | None:
    m = _FILENAME.match(filename)
    return int(m.group(1)) if m else None
