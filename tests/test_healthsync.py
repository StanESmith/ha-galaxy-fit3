"""Health sync codec (route 0a0a).

The builders must reproduce the expected wire bytes exactly, and the synthetic record frames below must
decode to the values asserted.
"""

from __future__ import annotations

import pytest

from sap import appframe as af
from sap import healthsync as hs

R = bytes.fromhex

T0 = 847_000_000  # protocol seconds (1990 epoch) == Unix 1_478_152_000
STEP = (
    "01c0317c32" "02fc317c32" "0300000000" "05fd317c32" "10d2040000" "1100a05444"
    "120000a03f" "1300002a42" "1464000000" "156e040000" "1660ea0000" "1701"
)
STEP_99 = STEP.replace("10d2040000", "1063000000")
HR = "01c0317c32" "02ca317c32" "0300000000" "05cb317c32" "2048"
HR_88 = HR[:-2] + "58"


def test_builders_match_the_reference_serializer() -> None:
    di = "0519010002000003043304c0317c32050000000006000000000701"
    assert hs.build_sync_ack(5, T0) == R("4203e0050004020101" + di)
    assert hs.build_request_data_response(5, T0) == R("43e0050004020101" + di)
    assert hs.build_request_data(7, T0) == R("03e00700" + di)
    assert hs.build_request_data(7) == R("03e00700051901000200000304330400000000050000000006000000000701")
    assert hs.build_mobile_sync_data(8, T0) == R("8202e00800" + di)
    assert hs.build_check_status(7) == R("0101e00700")


def test_user_profile_sets_only_the_distance_unit() -> None:
    # 0b <len> | 05 <update time> | <len entries> | 07 <1 km / 2 mi>   (createUserProfileByte, one entry)
    km = hs.build_user_profile("km", T0)
    mi = hs.build_user_profile("mi", T0)
    assert mi == R("0b0b000000" "05c0317c32" "02000000" "0702")
    assert km == mi[:-1] + b"\x01"
    with pytest.raises(ValueError):
        hs.build_user_profile("yd", T0)


def test_mobile_sync_data_counts_the_profile_block() -> None:
    di = "0519010002000003043304c0317c32050000000006000000000701"
    profile = hs.build_user_profile("mi", T0)
    assert hs.build_mobile_sync_data(8, T0, profile) == R("8203e00800" + di) + profile
    assert hs.build_mobile_sync_data(8, T0) == R("8202e00800" + di)


def test_on_demand_heart_rate_measure_matches_the_reference_serializer() -> None:
    assert hs.build_on_demand_measure(7) == R("04e00700198505024c270305031a090000008006810795e8039601")


def test_capability_response_bytes() -> None:
    assert hs.build_capability_response() == R(
        "e00ba10433c002c100c202a28d026b00b101ae01040300ac03ad0201c301010301020503041027"
        "b315030d017017020a000308000ce9030afc0301010100"
    )


def test_time_conversion_uses_the_1990_epoch() -> None:
    assert hs.from_protocol_time(T0) == 1_478_152_000
    assert hs.to_protocol_time(1_478_152_000) == T0


def test_sync_data_with_steps_and_heart_rate() -> None:
    # two step records (has-next 1 then 0), then one heart-rate record
    msg = R("8203e00500" "07" "01" + STEP + "00" + STEP_99 + "0a" "00" "16000000" + HR)
    d = hs.parse_sync_message(msg)
    assert (d.op, d.seq, d.stopped_at_tag) == (0x82, 5, None)
    assert [s.count for s in d.steps] == [1234, 99]
    s = d.steps[0]
    assert (s.start, s.end, s.update) == (1_478_152_000, 1_478_152_060, 1_478_152_061)
    assert (s.distance, s.speed, s.calorie) == (850.5, 1.25, 42.5)
    assert (s.run_steps, s.walk_steps, s.duration) == (100, 1134, 60000)
    assert [(h.bpm, h.start, h.end) for h in d.heart_rates] == [(72, 1_478_152_000, 1_478_152_010)]


def test_sync_data_with_two_heart_rate_records() -> None:
    d = hs.parse_sync_message(R("8202e00500" "0a" "01" "16000000" + HR + "00" "16000000" + HR_88))
    assert [h.bpm for h in d.heart_rates] == [72, 88]


def test_device_info_and_message_info() -> None:
    d = hs.parse_sync_message(R("03e005000402010105190100024c2703050304c0317c3205c0317c320644357c320701"))
    assert (d.op, d.seq, d.result) == (0x03, 5, hs.RESULT_SUCCESS)
    assert d.device_info.device_type == 10060
    assert (d.device_info.last_sync, d.device_info.start, d.device_info.end) == (T0, T0, T0 + 900)
    assert d.device_info.last_chunk is True


def test_unknown_data_type_stops_the_parse_but_keeps_what_came_before() -> None:
    d = hs.parse_sync_message(R("8203e00500" "0a" "00" "16000000" + HR + "70" "0102"))
    assert [h.bpm for h in d.heart_rates] == [72]
    assert d.stopped_at_tag == 0x70


def test_truncated_message_raises() -> None:
    with pytest.raises(ValueError):
        hs.parse_sync_message(R("8202e00500" "07" "00" + STEP[:40]))
    with pytest.raises(ValueError):
        hs.parse_sync_message(b"")


def test_sequence_of() -> None:
    assert hs.sequence_of(R("8203e04a2707")) == 0x274A
    assert hs.sequence_of(R("03e0050005")) == 5
    assert hs.sequence_of(R("34")) is None


def test_fragmented_message_is_reassembled() -> None:
    # first fragment: b4=02 with the route; last fragment: b4=06, no route (those two bytes are payload)
    body = bytes(range(256)) * 3
    first = af.decode_app_frame(af.build_frame(0x02, 0x40, hs.ROUTE_HEALTH, body[:490]))
    rest = body[490:]
    last = af.decode_app_frame(af.build_frame(0x06, 0x41, rest[:2], rest[2:]))
    re = af.MessageReassembler()
    assert re.feed(first) is None
    whole = re.feed(last)
    assert whole.route == hs.ROUTE_HEALTH and whole.app == body

    single = af.decode_app_frame(af.build_frame(0x00, 0x40, hs.ROUTE_HEALTH, b"\x34"))
    assert re.feed(single) is single
    assert re.feed(last) is None  # a continuation with no first fragment is dropped


def test_large_data_chunks_are_joined_and_acknowledged() -> None:
    rx = hs.LargeDataReceiver()
    body = bytes(range(200))
    assert rx.feed(R("3f08" "00000000") + body[:80]) is None
    assert rx.feed(R("3f09" "01000000") + body[80:150]) is None
    assert rx.feed(R("3f09" "01000000") + body[80:150]) is None  # a repeated chunk is ignored
    whole, ack = rx.feed(R("3f0a" "02000000") + body[150:])
    assert whole == body and ack == R("3e09" "03000000")
