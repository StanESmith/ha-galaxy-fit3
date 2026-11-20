"""Tests for the SAP message transport: Lite fragmentation + the async message channel."""

from __future__ import annotations

import asyncio

import pytest
from sap import constants as c
from sap import transport
from sap.transport import LiteReassembler, MessageChannel, fragment_payload


def _reassemble(frames: list[bytes]) -> bytes | None:
    r = LiteReassembler()
    out = None
    for fr in frames:
        out = r.push(fr)
    return out


def test_single_frame_round_trip() -> None:
    payload = b"hello watch"
    frames = fragment_payload(payload, max_payload=64)
    assert len(frames) == 1
    assert _reassemble(frames) == payload


def test_multi_fragment_round_trip() -> None:
    payload = bytes(range(256)) * 2  # 512 bytes, forces many fragments at small MTU
    frames = fragment_payload(payload, max_payload=18)
    assert len(frames) > 1
    assert all(len(f) <= 18 + c.LITE_HEADER_LEN for f in frames)
    assert _reassemble(frames) == payload


def test_fragment_boundary_exact_multiple() -> None:
    payload = b"A" * 36  # exactly 2 * 18
    frames = fragment_payload(payload, max_payload=18)
    assert _reassemble(frames) == payload


def test_out_of_order_fragment_raises() -> None:
    frames = fragment_payload(b"X" * 100, max_payload=18)
    r = LiteReassembler()
    r.push(frames[0])  # FRAG_FIRST
    with pytest.raises(transport.ReassemblyError):
        r.push(frames[2])  # skip frames[1] -> sequence gap


def test_continuation_without_start_raises() -> None:
    frames = fragment_payload(b"X" * 100, max_payload=18)
    r = LiteReassembler()
    with pytest.raises(transport.ReassemblyError):
        r.push(frames[1])  # a MIDDLE with no FRAG_FIRST


def test_encrypted_flag_survives_on_single() -> None:
    from sap import frame

    [fr] = fragment_payload(b"secret", max_payload=64, encrypted=True)
    header, _ = frame.decode_frame(fr)
    assert header.encrypted is True


def test_message_channel_loopback() -> None:
    """Two channels wired to each other exchange whole messages across fragmentation."""

    async def scenario() -> None:
        chan_a: MessageChannel
        chan_b: MessageChannel

        async def a_writes(frame_bytes: bytes) -> None:
            chan_b.feed_notification(frame_bytes)

        async def b_writes(frame_bytes: bytes) -> None:
            chan_a.feed_notification(frame_bytes)

        chan_a = MessageChannel(a_writes, mtu_frame=20)
        chan_b = MessageChannel(b_writes, mtu_frame=20)

        big = bytes(range(200))
        await chan_a.send(big)
        assert await chan_b.recv(timeout=1.0) == big

        await chan_b.send(b"pong")
        assert await chan_a.recv(timeout=1.0) == b"pong"

    asyncio.run(scenario())


def test_message_channel_recv_timeout() -> None:
    async def scenario() -> None:
        chan = MessageChannel(lambda _f: asyncio.sleep(0), mtu_frame=20)
        with pytest.raises(asyncio.TimeoutError):
            await chan.recv(timeout=0.05)

    asyncio.run(scenario())
