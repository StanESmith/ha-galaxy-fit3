"""Tests for the SAP Lite frame codec (layer [2]) -- byte-exact."""

from __future__ import annotations

from sap import constants as c
from sap import frame


def test_data_header_roundtrip_single_encrypted() -> None:
    h = frame.LiteHeader(
        frame_type=c.LITE_FRAME_DATA,
        message_type=0,
        fragment_type=c.FRAG_SINGLE,
        reliable=False,
        transaction_id=0,
        window_id=0,
        encrypted=True,
    )
    wire = frame.encode_lite_header(h)
    # byte0 = all zero; byte1 = encryption bit (0x04)
    assert wire == b"\x00\x04"
    back = frame.decode_lite_header(wire)
    assert back.frame_type == c.LITE_FRAME_DATA
    assert back.encrypted is True
    assert back.compressed is False
    assert back.fragment_type == c.FRAG_SINGLE


def test_data_header_fields_pack() -> None:
    h = frame.LiteHeader(
        frame_type=c.LITE_FRAME_DATA,
        message_type=5,
        fragment_type=c.FRAG_FIRST,
        reliable=True,
        transaction_id=3,
        window_id=1,
        compressed=True,
    )
    b0, b1 = frame.encode_lite_header(h)
    assert (b0 & c.LITE_FRAME_TYPE_MASK) >> c.LITE_FRAME_TYPE_SHIFT == 0
    assert (b0 & c.LITE_MESSAGE_TYPE_MASK) >> c.LITE_MESSAGE_TYPE_SHIFT == 5
    assert (b0 & c.LITE_FRAGMENT_TYPE_MASK) >> c.LITE_FRAGMENT_TYPE_SHIFT == c.FRAG_FIRST
    assert b0 & c.LITE_RELIABILITY_MASK == 1
    assert (b1 & c.LITE_TRANSACTION_ID_MASK) >> c.LITE_TRANSACTION_ID_SHIFT == 3
    assert (b1 & c.LITE_WINDOW_ID_MASK) >> c.LITE_WINDOW_ID_SHIFT == 1
    assert b1 & c.LITE_COMPRESSION_MASK


def test_continuation_fragment_carries_seq_id() -> None:
    h = frame.LiteHeader(
        frame_type=c.LITE_FRAME_DATA,
        fragment_type=c.FRAG_LAST,
        transaction_id=2,
        seq_id=9,
    )
    wire = frame.encode_lite_header(h)
    back = frame.decode_lite_header(wire)
    assert back.fragment_type == c.FRAG_LAST
    assert back.seq_id == 9
    # continuation frames do not expose the encrypted/compressed flags
    assert back.encrypted is False


def test_encode_data_frame_prepends_header() -> None:
    payload = b"hello-watch"
    h = frame.LiteHeader()
    out = frame.encode_data_frame(payload, h)
    hdr, body = frame.decode_frame(out)
    assert body == payload
    assert hdr.frame_type == c.LITE_FRAME_DATA


def test_control_ack_bytes() -> None:
    # CONTROL(2)<<6 | ACK(1)<<3 = 0x88
    assert frame.encode_ack(transaction_id=0, window_id=0, status=0, sequence=5) == b"\x88\x00\x00\x05"


def test_control_abort_bytes() -> None:
    # CONTROL(2)<<6 | ABORT(2)<<3 = 0x90
    assert frame.encode_abort(transaction_id=0) == b"\x90\x00\x00"


def test_short_buffer_rejected() -> None:
    try:
        frame.decode_lite_header(b"\x00")
    except ValueError:
        return
    raise AssertionError("expected ValueError on short buffer")
