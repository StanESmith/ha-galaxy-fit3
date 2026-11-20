"""SAP message transport (layer [3], Phase 4): Lite fragmentation + an async message channel.

Sits between the BLE pipe (:mod:`.ble`, which moves raw GATT notifications) and the protocol logic
(handshake, capability, app messages). It turns a stream of SAP **Lite frames** into whole messages
and back, handling fragmentation of payloads that exceed one BLE write.

Design choice (documented in ``ble.py``): rather than rely on the capture-gated GATT-notification
boundary marker, the sender **fragments at the Lite layer** so every frame fits one ATT write and
therefore one notification. Reassembly is then the known Lite fragment layer
(``FRAG_FIRST``/``FRAG_MIDDLE``/``FRAG_LAST`` with the 4-bit sequence), which is unit-tested here.
A single Lite frame that is itself split across notifications is the only remaining capture-gated
case and is not produced by this sender.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from . import constants as c
from . import frame


class ReassemblyError(Exception):
    """A Lite fragment arrived out of order, with a bad sequence, or without a start."""


def fragment_payload(
    payload: bytes,
    max_payload: int = c.DEFAULT_ATT_WRITE_SIZE - c.LITE_HEADER_LEN,
    *,
    encrypted: bool = False,
    transaction_id: int = 0,
) -> list[bytes]:
    """Split ``payload`` into one or more Lite data frames, each ``<= max_payload`` body bytes.

    One frame -> ``FRAG_SINGLE``. Several -> ``FRAG_FIRST`` (carries the ``encrypted`` flag), then
    ``FRAG_MIDDLE`` ... ``FRAG_LAST`` carrying a 4-bit running sequence (wraps at 16).
    """
    if max_payload < 1:
        raise ValueError("max_payload must be >= 1")
    if len(payload) <= max_payload:
        header = frame.LiteHeader(
            fragment_type=c.FRAG_SINGLE, encrypted=encrypted, transaction_id=transaction_id
        )
        return [frame.encode_data_frame(payload, header)]

    chunks = [payload[i : i + max_payload] for i in range(0, len(payload), max_payload)]
    frames: list[bytes] = []
    last = len(chunks) - 1
    for idx, chunk in enumerate(chunks):
        if idx == 0:
            h = frame.LiteHeader(
                fragment_type=c.FRAG_FIRST, encrypted=encrypted, transaction_id=transaction_id
            )
        else:
            h = frame.LiteHeader(
                fragment_type=c.FRAG_LAST if idx == last else c.FRAG_MIDDLE,
                transaction_id=transaction_id,
                seq_id=idx % c.LITE_SEQ_WRAP,
            )
        frames.append(frame.encode_data_frame(chunk, h))
    return frames


class LiteReassembler:
    """Reassemble Lite data fragments into complete message payloads.

    Control frames (ACK/ABORT) are ignored here (handled by the reliability layer, if used).
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._active = False
        self._expect_seq = 0

    def reset(self) -> None:
        self._buf = bytearray()
        self._active = False
        self._expect_seq = 0

    def push(self, frame_bytes: bytes) -> bytes | None:
        """Feed one received Lite frame. Return a complete payload, or ``None`` if more is due."""
        header, payload = frame.decode_frame(frame_bytes)
        if header.frame_type != c.LITE_FRAME_DATA:
            return None  # control frame; not our concern here

        ft = header.fragment_type
        if ft == c.FRAG_SINGLE:
            self.reset()
            return payload
        if ft == c.FRAG_FIRST:
            self._buf = bytearray(payload)
            self._active = True
            self._expect_seq = 1 % c.LITE_SEQ_WRAP
            return None
        if ft in (c.FRAG_MIDDLE, c.FRAG_LAST):
            if not self._active:
                raise ReassemblyError("continuation fragment without a FRAG_FIRST start")
            if header.seq_id != self._expect_seq:
                self.reset()
                raise ReassemblyError(
                    f"fragment out of order: got seq {header.seq_id}, expected {self._expect_seq}"
                )
            self._buf += payload
            self._expect_seq = (self._expect_seq + 1) % c.LITE_SEQ_WRAP
            if ft == c.FRAG_LAST:
                out = bytes(self._buf)
                self.reset()
                return out
            return None
        raise ReassemblyError(f"unknown fragment type {ft}")


WriteFrame = Callable[[bytes], Awaitable[None]]


class MessageChannel:
    """Async whole-message send/recv over a frame writer + a notification feed.

    Transport-agnostic so it can run against :class:`~.ble.BleTransport` in production and a fake
    loopback in tests. Outbound payloads are Lite-fragmented to ``mtu_frame`` bytes/frame; inbound
    notifications (one Lite frame each) are reassembled and queued as whole messages.
    """

    def __init__(self, write_frame: WriteFrame, mtu_frame: int = c.DEFAULT_ATT_WRITE_SIZE) -> None:
        self._write = write_frame
        self._max_payload = max(1, mtu_frame - c.LITE_HEADER_LEN)
        self._reasm = LiteReassembler()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._tx_id = 0

    def feed_notification(self, data: bytes) -> None:
        """Called for each inbound GATT notification (one Lite frame)."""
        msg = self._reasm.push(data)
        if msg is not None:
            self._queue.put_nowait(msg)

    async def send(self, payload: bytes, *, encrypted: bool = False) -> None:
        self._tx_id = (self._tx_id + 1) % 8  # 3-bit transaction id
        for fr in fragment_payload(
            payload, self._max_payload, encrypted=encrypted, transaction_id=self._tx_id
        ):
            await self._write(fr)

    async def recv(self, timeout: float = c.CONNECT_TIMEOUT_S) -> bytes:
        return await asyncio.wait_for(self._queue.get(), timeout)
