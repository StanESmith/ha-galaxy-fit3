"""Reactive replies, checked against request/reply pairs from two real pairing sessions."""

from __future__ import annotations

from sap import appframe as af
from sap import healthsync as hs
from sap import reactive as rx

R = bytes.fromhex


def test_0a0a_echoes_the_watch_counter_in_both_sessions() -> None:
    # session 1 and session 2 differ only in the counter byte, which the reply must echo
    assert rx.reply_for(R("0a0a"), R("a202e01627e12d01")) == R("e203e01627e12d01d001")
    assert rx.reply_for(R("0a0a"), R("a202e01327e12d01")) == R("e203e01327e12d01d001")


def test_fixed_replies() -> None:
    assert rx.reply_for(R("0808"), R("0c")).startswith(R("4c27"))
    assert b"Eastern Standard Time" in rx.reply_for(R("0808"), R("0c"))
    assert rx.reply_for(R("0808"), R("0d")) == R("cd2400000000")
    assert rx.reply_for(R("0404"), R("8600")) == R("c60247024801")
    assert rx.reply_for(R("0707"), R("161500160017001801190120002100")) == R("561600180119011500170020002100")


def test_capability_map_is_acknowledged_but_a_bare_ack_byte_is_not() -> None:
    cap = R("0001130317020103150202031c020303020204030802")  # starts the map, long enough
    assert rx.reply_for(R("0f0f"), cap + b"\x00" * 60) == b"\x00"
    assert rx.reply_for(R("0f0f"), b"\x00") is None  # our own ack echoed back must not loop


def test_unknown_requests_get_no_reply() -> None:
    assert rx.reply_for(R("0909"), R("05")) is None
    assert rx.reply_for(R("0707"), R("9001060131")) is None
    assert rx.reply_for(R("0b0b"), b"") is None


def test_replies_follow_the_opcode_rule() -> None:
    for route, req in [("0808", "0c"), ("0404", "8600"), ("0a0a", "a202e01327e12d01")]:
        reply = rx.reply_for(R(route), R(req))
        assert af.is_reply(R(req)[0], reply[0]), (route, req)


def test_requests_answered_consistently_in_both_sessions() -> None:
    assert rx.reply_for(R("0b0b"), R("102000")) == R("01046400")
    assert rx.reply_for(R("0909"), R("010101")) == R("57010101")
    assert rx.reply_for(R("0909"), R("010102")) == R("57010102")  # echoes the last byte
    assert rx.reply_for(R("0909"), R("03")) is None  # the other 0909 watch frames get no reply


def test_informational_pushes_get_no_reply() -> None:
    # frames the watch pushed in the capture that the phone never answered
    for route, app in [("0000", "1827"), ("0a0a", "89e0"), ("0a0a", "04e0"), ("0a0a", "a017e0"),
                       ("0b0b", "232001"), ("0b0b", "622101"), ("0808", "07110000"), ("1818", "c002"),
                       ("1a1a", "80010400"), ("0202", "040302"), ("0707", "9001060131")]:
        assert rx.reply_for(R(route), R(app)) is None, (route, app)


def test_timezone_reply_follows_the_supplied_zone_and_defaults_to_the_capture() -> None:
    from sap import tzinfo as tz

    captured = R("4c27010901504628154561737465726e205374616e646172642054696d65")
    assert rx.reply_for(R("0808"), R("0c")) == captured  # default = the real capture, byte for byte
    assert tz.build_timezone_reply() == captured
    tokyo = rx.reply_for(R("0808"), R("0c"), (32400, "Japan Standard Time"))
    assert tokyo == R("4c27010900") + R("907e") + R("2813") + b"Japan Standard Time"  # east of UTC: sign 0
    assert rx.reply_for(R("0808"), R("0c"), (-28800, "Pacific Standard Time"))[4:7] == R("01 80 70".replace(" ", ""))
    assert rx.reply_for(R("0808"), R("0c"), (19800, "India Standard Time"))[4:7] == R("00584d")  # +5:30


def test_offset_triple_and_names() -> None:
    from sap import tzinfo as tz

    assert tz.offset_triple(-18000) == R("015046") and tz.offset_triple(0) == R("000000")
    assert tz.timezone_name("America/New_York", -18000) == "Eastern Standard Time"
    assert tz.timezone_name("Europe/London", 0) == "Greenwich Mean Time"
    assert tz.timezone_name("Asia/Kathmandu", 20700) == "GMT+05:45"  # not in the table -> generic name
    assert tz.timezone_name("America/St_Johns", -12600) == "GMT-03:30"


def test_reply_window_class_follows_the_captured_phone() -> None:
    assert rx.reply_class(R("0f0f")) == 0x20  # the phone used 0x20/0x30 for the capability-map ack
    assert rx.reply_class(R("0a0a")) == 0x40 and rx.reply_class(R("0808")) == 0x40
    assert rx.MAX_REPLIES[("0f0f", 0x00)] == 2


def test_0a0a_installed_claim_sets_status_2_with_version() -> None:
    # claim_health_installed flips the GM_STATUS to 2 (installed) + d2 <versionCode LE32> and bumps
    # the param count by 2 instead of 1 (the honest reply keeps status 1 / uninstalled).
    ver = rx.HEALTH_APP_VERSION_CODE.to_bytes(4, "little")
    got = rx.reply_for(R("0a0a"), R("a202e01627e12d01"), claim_health_installed=True)
    assert got == R("e204e01627e12d01") + b"\xd0\x02\xd2" + ver
    # default (and honest) stays uninstalled
    assert rx.reply_for(R("0a0a"), R("a202e01627e12d01")).endswith(b"\xd0\x01")


def test_health_sync_data_is_acknowledged_with_success() -> None:
    # SYNC_DATA (82) -> 42 02 e0 <seq16> 04 02 01 01; the last byte is ActionResult SUCCESS (1).
    assert rx.reply_for(R("0a0a"), R("8203e04a270701")) == hs.build_sync_ack(0x274A)


def test_watch_wearable_messages_get_no_reply() -> None:
    # the periodic exercise-status wearable message: a health app's answer format is not implemented
    assert rx.reply_for(R("0a0a"), R("04e05027198509024c270305031a07800395d0079301")) is None


def test_request_data_is_answered_with_success() -> None:
    req = R("03e0050005190100024c2703050304c0317c3205c0317c320644357c320701")
    assert rx.reply_for(R("0a0a"), req, health_last_sync=7) == hs.build_request_data_response(5, 7)


def test_capability_request_is_answered_only_when_claiming_a_health_app() -> None:
    cap = R("a017e01527a001a10433") + b"\x00" * 20
    assert rx.reply_for(R("0a0a"), cap) is None
    got = rx.reply_for(R("0a0a"), cap, claim_health_installed=True)
    assert got[:2] == R("e00b") and rx.HEALTH_APP_VERSION_CODE.to_bytes(4, "little") in got
    assert rx.MAX_REPLIES[("0a0a", 0xA0)] == 3
