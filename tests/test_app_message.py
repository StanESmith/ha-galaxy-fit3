"""Tests for the Fit3 app-message TLV (layer [7]).

The header byte is byte-exact; the set-time packet is locked to the reconstruction documented in
``app_message.py`` (big-endian, param-id emitted). If the capture shows a different endianness or
param layout, these vectors update alongside the code.
"""

from __future__ import annotations

from sap import app_message as am
from sap import constants as c


def test_header_byte() -> None:
    # format=variable(1)<<7 | type=request(0)<<6 | id=0  -> 0x80
    assert am.build_header(0, c.APP_MSG_TYPE_REQUEST, c.APP_MSG_FORMAT_VARIABLE) == 0x80
    # format=fixed(0) | type=response(1)<<6 | id=2 -> 0x42
    assert am.build_header(2, c.APP_MSG_TYPE_RESPONSE, c.APP_MSG_FORMAT_FIXED) == 0x42


def test_header_id_range() -> None:
    try:
        am.build_header(64, 0, 1)
    except ValueError:
        return
    raise AssertionError("expected ValueError for 7-bit message id")


def test_set_time_packet_vector() -> None:
    out = am.build_set_time(epoch_utc_s=0x12345678, is_24h=True, tz_offset_seconds=0)
    expected = bytes(
        [
            0x80,  # header: variable request id 0
            0x04,  # NO_OF_PARAM
            0x00, 0x00,  # param 0 (roaming) = 0
            0x01, 0x00, 0x00, 0x00, 0x00, 0x12, 0x34, 0x56, 0x78,  # param 1 (epoch, 8B BE)
            0x04, 0x01,  # param 4 (is24h) = 1
            0x02, 0x00,  # param 2 (tz sign) = 0 (non-negative)
            0x00, 0x00,  # bare short: tz offset = 0
        ]
    )
    assert out == expected


def test_set_time_negative_offset_sets_sign() -> None:
    # -5h = -18000s -> sign byte = 1, magnitude 300 (minutes) as the bare short
    out = am.build_set_time(epoch_utc_s=0, is_24h=False, tz_offset_seconds=-18000)
    assert out[0] == 0x80
    assert out[1] == 0x04
    # is24h param (id 4) value 0
    assert out[13:15] == bytes([0x04, 0x00])
    # sign param (id 2) value 1
    assert out[15:17] == bytes([0x02, 0x01])
    # bare short magnitude = 300 minutes
    assert out[17:19] == (300).to_bytes(2, "big")
