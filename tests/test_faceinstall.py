"""Face install framing and the RFCOMM exchange, checked against the 2026-11-19 phone capture."""

from __future__ import annotations

import socket
import struct
import threading
import zlib

import pytest

from sap import faceinstall as fi

# route-1e1e announce a phone app sent for Progress gauge 1 (face 16, 3,158,418 bytes)
ANNOUNCE_16 = "0103060419534d2d523339305f30303031365f323536783430322e62696e0592313000"


def test_announce_matches_capture() -> None:
    assert fi.announce_app(fi.face_filename(16), 3_158_418) == bytes.fromhex(ANNOUNCE_16)


def test_header_matches_capture() -> None:
    assert fi.header(fi.face_filename(16), 3_158_418) == b"33bin,/user/wf/SM-R390_00016_256x402.bin,3158418"
    assert fi.face_id_from_filename("SM-R390_00016_256x402.bin") == 16


def test_blocks_carry_crc32_and_roundtrip() -> None:
    data = bytes(range(256)) * 400  # 102,400 bytes = 2 full blocks + 23,200
    framed = list(fi.blocks(data))
    assert [len(b) for b in framed] == [fi.BLOCK_SIZE + 4, fi.BLOCK_SIZE + 4, len(data) - 2 * fi.BLOCK_SIZE + 4]
    assert framed[0][-4:] == struct.pack("<I", zlib.crc32(data[: fi.BLOCK_SIZE]))
    assert fi.unframe(b"".join(framed), len(data)) == data
    broken = bytearray(b"".join(framed))
    broken[5] ^= 0xFF
    with pytest.raises(ValueError):
        fi.unframe(bytes(broken), len(data))


class _FakeWatchService(threading.Thread):
    """The watch's side of the RFCOMM channel over a socketpair: reads commands, answers like the capture."""

    def __init__(self, sock: socket.socket, size: int, *, bad_reply_at: bytes | None = None) -> None:
        super().__init__(daemon=True)
        self.sock, self.size, self.bad_reply_at = sock, size, bad_reply_at
        self.received = bytearray()
        self.log: list[bytes] = []

    def _recv_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            part = self.sock.recv(n - len(out))
            if not part:
                raise ConnectionError
            out += part
        return out

    def _reply(self, cmd: bytes, answer: bytes) -> None:
        self.log.append(cmd)
        self.sock.sendall(b"999" if cmd == self.bad_reply_at else answer)

    def run(self) -> None:
        try:
            self._reply(self._recv_exact(2), b"300")
            head = self.sock.recv(200)
            self._reply(head, b"330")
            remaining = self.size
            while remaining:
                n = min(fi.BLOCK_SIZE, remaining)
                block = self._recv_exact(n + 4)
                assert struct.pack("<I", zlib.crc32(block[:n])) == block[n:]
                self.received += block[:n]
                remaining -= n
                self.sock.sendall(b"310")
            self._reply(self._recv_exact(2), b"320")
            self._reply(self._recv_exact(2), b"340")
        except (ConnectionError, OSError):
            pass


def test_rfcomm_session_sends_the_file_like_the_phone() -> None:
    data = bytes((i * 7) & 0xFF for i in range(100_000))
    ours, theirs = socket.socketpair()
    watch = _FakeWatchService(theirs, len(data))
    watch.start()
    session = fi.RfcommSession(ours)
    progress = []
    session.command(b"30", b"300")
    session.send_file(fi.face_filename(16), data, lambda n, total: progress.append((n, total)))
    session.command(b"34", b"340")
    session.close()
    watch.join(5)
    assert bytes(watch.received) == data
    assert watch.log[0] == b"30" and watch.log[1].startswith(b"33bin,/user/wf/SM-R390_00016_256x402.bin,100000")
    assert watch.log[2:] == [b"32", b"34"]
    assert progress[-1] == (3, 3)


def test_rfcomm_session_stops_on_an_unexpected_reply() -> None:
    ours, theirs = socket.socketpair()
    watch = _FakeWatchService(theirs, 10, bad_reply_at=b"30")
    watch.start()
    session = fi.RfcommSession(ours)
    with pytest.raises(fi.TransferError):
        session.command(b"30", b"300")
    session.close()


def test_connect_gives_up_at_once_when_the_kernel_refuses_the_security(monkeypatch) -> None:
    import errno as _errno

    calls = []

    class _Sock:
        def __init__(self, *_a):
            pass

        def settimeout(self, _t):
            pass

        def bind(self, _addr):
            pass

        def connect(self, _addr):
            calls.append(1)
            raise PermissionError(_errno.EACCES, "Permission denied")

        def close(self):
            pass

    monkeypatch.setattr(fi.socket, "socket", _Sock)
    monkeypatch.setattr(fi.socket, "AF_BLUETOOTH", 31, raising=False)
    monkeypatch.setattr(fi.socket, "BTPROTO_RFCOMM", 3, raising=False)
    with pytest.raises(fi.SecurityRefused):
        fi.RfcommSession.connect("00:00:00:00:00:01", "AA:BB:CC:DD:EE:FF")
    assert calls == [1]
