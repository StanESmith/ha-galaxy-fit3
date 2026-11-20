"""SAP "Lite" frame codec (layer [2]) -- the BLE framing used by the Galaxy Fit3.

Byte-exact, and unit-tested in ``tests/test_frame.py``.

A Lite frame = 2-byte header followed by the payload. The header multiplexes a session's data and
carries the per-record ``encrypted``/``compressed`` flags and fragmentation state.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import constants as c


@dataclass(slots=True)
class LiteHeader:
    """Decoded fields of a 2-byte SAP Lite data-frame header."""

    frame_type: int = c.LITE_FRAME_DATA
    message_type: int = 0
    fragment_type: int = c.FRAG_SINGLE
    reliable: bool = False
    transaction_id: int = 0
    window_id: int = 0
    compressed: bool = False
    encrypted: bool = False
    seq_id: int = 0  # only meaningful on continuation fragments (FRAG_MIDDLE/FRAG_LAST)


def encode_lite_header(h: LiteHeader) -> bytes:
    """Encode a :class:`LiteHeader` to its 2 wire bytes."""
    b0 = (
        ((h.frame_type << c.LITE_FRAME_TYPE_SHIFT) & c.LITE_FRAME_TYPE_MASK)
        | ((h.message_type << c.LITE_MESSAGE_TYPE_SHIFT) & c.LITE_MESSAGE_TYPE_MASK)
        | ((h.fragment_type << c.LITE_FRAGMENT_TYPE_SHIFT) & c.LITE_FRAGMENT_TYPE_MASK)
        | (int(h.reliable) & c.LITE_RELIABILITY_MASK)
    )
    b1 = ((h.transaction_id << c.LITE_TRANSACTION_ID_SHIFT) & c.LITE_TRANSACTION_ID_MASK) | (
        (h.window_id << c.LITE_WINDOW_ID_SHIFT) & c.LITE_WINDOW_ID_MASK
    )
    if h.fragment_type in (c.FRAG_MIDDLE, c.FRAG_LAST):
        # continuation fragments carry a 4-bit sequence id in the low nibble
        b1 |= h.seq_id & c.LITE_SEQ_ID_MASK
    else:
        # first/single fragments carry the compression + encryption flags
        if h.compressed:
            b1 |= c.LITE_COMPRESSION_MASK
        if h.encrypted:
            b1 |= c.LITE_ENCRYPTION_MASK
    return bytes((b0 & 0xFF, b1 & 0xFF))


def decode_lite_header(data: bytes) -> LiteHeader:
    """Decode the 2 header bytes at the start of ``data``."""
    if len(data) < c.LITE_HEADER_LEN:
        raise ValueError("buffer too short for a Lite header")
    b0, b1 = data[0], data[1]
    frag = (b0 & c.LITE_FRAGMENT_TYPE_MASK) >> c.LITE_FRAGMENT_TYPE_SHIFT
    h = LiteHeader(
        frame_type=(b0 & c.LITE_FRAME_TYPE_MASK) >> c.LITE_FRAME_TYPE_SHIFT,
        message_type=(b0 & c.LITE_MESSAGE_TYPE_MASK) >> c.LITE_MESSAGE_TYPE_SHIFT,
        fragment_type=frag,
        reliable=bool(b0 & c.LITE_RELIABILITY_MASK),
        transaction_id=(b1 & c.LITE_TRANSACTION_ID_MASK) >> c.LITE_TRANSACTION_ID_SHIFT,
        window_id=(b1 & c.LITE_WINDOW_ID_MASK) >> c.LITE_WINDOW_ID_SHIFT,
    )
    if frag in (c.FRAG_MIDDLE, c.FRAG_LAST):
        h.seq_id = b1 & c.LITE_SEQ_ID_MASK
    else:
        h.compressed = bool(b1 & c.LITE_COMPRESSION_MASK)
        h.encrypted = bool(b1 & c.LITE_ENCRYPTION_MASK)
    return h


def encode_data_frame(payload: bytes, header: LiteHeader) -> bytes:
    """Prepend a Lite data-frame header to ``payload``."""
    header.frame_type = c.LITE_FRAME_DATA
    return encode_lite_header(header) + payload


def decode_frame(data: bytes) -> tuple[LiteHeader, bytes]:
    """Split a received Lite frame into (header, payload)."""
    header = decode_lite_header(data)
    return header, data[c.LITE_HEADER_LEN :]


def encode_ack(transaction_id: int, window_id: int, status: int, sequence: int) -> bytes:
    """Build a Lite CONTROL/ACK frame (4-byte header)."""
    b0 = (
        ((c.LITE_FRAME_CONTROL << c.LITE_FRAME_TYPE_SHIFT) & c.LITE_FRAME_TYPE_MASK)
        | ((c.LITE_CONTROL_ACK << c.LITE_MESSAGE_TYPE_SHIFT) & c.LITE_MESSAGE_TYPE_MASK)
    )
    b1 = (transaction_id << c.LITE_TRANSACTION_ID_SHIFT) & c.LITE_TRANSACTION_ID_MASK
    status_byte = (status & c.LITE_ACK_STATUS_MASK) | (
        (window_id << c.LITE_ACK_WINDOW_ID_SHIFT) & c.LITE_ACK_WINDOW_ID_MASK
    )
    return bytes((b0 & 0xFF, b1 & 0xFF, status_byte & 0xFF, sequence & 0xFF))


def encode_abort(
    transaction_id: int,
    reason: int = c.LITE_ABORT_REASON_MAX_RETRY,
    initiator: int = c.LITE_ABORT_INITIATOR_SENDER,
) -> bytes:
    """Build a Lite CONTROL/ABORT frame (2-byte header + 1-byte body)."""
    b0 = (
        ((c.LITE_FRAME_CONTROL << c.LITE_FRAME_TYPE_SHIFT) & c.LITE_FRAME_TYPE_MASK)
        | ((c.LITE_CONTROL_ABORT << c.LITE_MESSAGE_TYPE_SHIFT) & c.LITE_MESSAGE_TYPE_MASK)
    )
    b1 = (transaction_id << c.LITE_TRANSACTION_ID_SHIFT) & c.LITE_TRANSACTION_ID_MASK
    body = (reason & c.LITE_ABORT_REASON_MASK) | (
        (initiator << c.LITE_ABORT_INITIATOR_SHIFT) & c.LITE_ABORT_INITIATOR_MASK
    )
    return bytes((b0 & 0xFF, b1 & 0xFF, body & 0xFF))
