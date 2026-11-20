"""The replayed phone setup exchange must encode to valid, decodable frames."""

from __future__ import annotations

from sap import appframe as af
from sap import setup_sequence as ss


def test_every_setup_frame_roundtrips() -> None:
    assert len(ss.SETUP_FRAMES) >= 25
    for h2, h3, b4, b5, route_hex, app_hex in ss.SETUP_FRAMES:
        app = ss.build_time_app(1794058070) if app_hex is None else bytes.fromhex(app_hex)
        wire = af.encode_app_frame(h2, h3, b4, b5, bytes.fromhex(route_hex), app)
        d = af.decode_app_frame(wire)  # raises on a bad CRC
        assert (d.h2, d.h3, d.b4, d.b5) == (h2, h3, b4, b5)
        assert d.route.hex() == route_hex and d.app == app
        # the computed header (length + checksum) equals the captured one for every real frame
        assert af.build_frame(b4, b5, bytes.fromhex(route_hex), app) == wire


def test_time_frame_matches_capture_and_length_is_fixed() -> None:
    cap = bytes.fromhex("830164000200030a31373934303538303730040150460500")
    assert ss.build_time_app(1794058070) == cap
    assert len(ss.build_time_app()) == len(cap)  # live clock keeps the frame length (H1/H2 stay valid)


def test_time_frame_carries_the_hosts_utc_offset() -> None:
    from sap import tzinfo

    eastern = ss.build_time_app(1794058070)
    assert eastern.endswith(bytes.fromhex("040150460500"))  # captured: UTC-5
    tokyo = ss.build_time_app(1794058070, 32400)
    assert tokyo.endswith(bytes.fromhex("04") + tzinfo.offset_triple(32400) + bytes.fromhex("0500"))
    assert len(tokyo) == len(eastern)  # same length, so the frame header/checksum are unchanged
