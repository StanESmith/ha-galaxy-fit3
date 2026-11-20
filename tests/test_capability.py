"""Tests for the SAP capability service->channel map parser (layer [6]).

The test vector is the real capability-RESPONSE frame captured on the BLE SAP
pipe during a fresh Galaxy-Wearable pairing (2026-11-07). It carries only the
numeric service ids / channel bindings (no MACs, names, or secrets), so it is
safe to keep in the repo as a protocol fixture.

Record layout after the ``0f 0f 00 01`` marker: ``[channelId][0x03][serviceId][0x02]``;
channelId ``0xFF`` = advertised-but-unbound.
"""

from __future__ import annotations

import pytest

from sap import capability


# Full captured capability-list frame, byte-exact (leading frame header +
# 0f0f0001 marker + records + trailing marker byte and 2-byte CRC). The parser
# locates the marker itself, so the whole frame is a fair fixture.
#   00 53 3d 40 00 40 | 0f 0f 00 01 | <records> | ff a8 b7
# records (ch,svc): 19,23 1,21 2,28 3,2 4,8 5,6 6,25 7,24 8,1  then unbound 0xff:
#   3 4 7 9 20 22 26 27 29 30
_CAP_FRAME_FULL = bytes.fromhex(
    "00533d4000400f0f0001"
    "130317020103150202031c02030302020403080205030602060319020703180208030102"
    "ff030302ff030402ff030702ff030902ff031402ff031602ff031a02ff031b02ff031d02ff031e02"
    "ffa8b7"
)


def test_parse_service_channel_map_real_capture() -> None:
    m = capability.parse_service_channel_map(_CAP_FRAME_FULL)

    # Watch-face (service 6) is the one feature confirmed on-wire -> channel 5.
    assert m.channel_for(0x06) == 0x05
    # OOBE/pairing service 1 -> channel 8.
    assert m.channel_for(0x01) == 0x08
    # A few more bound channels from the capture.
    assert m.channel_for(0x17) == 0x13  # svc23 -> ch19
    assert m.channel_for(0x15) == 0x01  # svc21 -> ch1
    assert m.channel_for(0x1C) == 0x02  # svc28 -> ch2
    assert m.channel_for(0x02) == 0x03  # svc2  -> ch3

    # Unbound services carry channel 0xFF and must NOT appear in `bound`.
    assert m.channel_for(0x1E) is None  # svc30 (OTA) advertised but unbound
    assert 0x1E in m.services
    assert 0x1E not in m.bound

    # The CRITICAL caveat: health/time/battery services (our assumed 10/11)
    # are absent from this capture -- guard against a regression that invents them.
    assert 0x0A not in m.services  # service 10 (assumed HR/steps) absent
    assert 0x0B not in m.services  # service 11 (assumed time/battery) absent

    # Nine services are bound to a real channel in this capture.
    assert len(m.bound) == 9
    # Total advertised services: 9 bound + 10 unbound.
    assert len(m.services) == 19


def test_parse_missing_marker_raises() -> None:
    with pytest.raises(ValueError, match="marker"):
        capability.parse_service_channel_map(b"\x00\x01\x02\x03no marker here")


def test_parse_marker_but_no_records_raises() -> None:
    # Marker present but immediately followed by non-record bytes.
    with pytest.raises(ValueError, match="no service records"):
        capability.parse_service_channel_map(capability.CAP_MAP_MARKER + b"\xaa\xbb")
