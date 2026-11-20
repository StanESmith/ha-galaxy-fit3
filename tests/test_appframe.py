"""Tests for the SAP app frame codec (battery / watch-face), using real captured frames."""

from __future__ import annotations

import pytest

from sap import appframe as af

BATT_Q = "000503c001500b0b02f3db"
BATT_R48 = "000906c000400b0b420600053048c4"
BATT_R47 = "000906c000400b0b420600052f8085"
WF_CAT_Q = "000503c001200606016114"
# "05" removes a face from the watch (first mistaken for "select"; 2026-11-19 capture proved it)
WF_SEL_4F = "000906c00130060605044f1d04969c"
WF_SEL_2E = "000906c00120060605042e1d02464c"
# a phone app showing a face ("03") and then sending its face list ("0a"), phone capture 2026-11-19
WF_SHOW_FE = "000906c0013006060304fe1d007245"
WF_SHOW_6A = "000906c00120060603046a1d0152c4"
WF_ORDER = "00241b00013006060a190107046a1d0104fe1d00044f1d0404161d00041c1d00046c1d01042e1d02f6b6"
WF_SEL_RESP = "000bc7410040060645044f1d041a01dd64"
WF_CAT_R = (
    "010e548000400606c10307180a04fe05053130303030060e77665f6e616d652d303032353400080009000a000b01"
    "0c000d011e011808041605053130303034060e77665f6e616d652d303030323200080009010a000b000c00180804"
    "4f05053130303032060e77665f6e616d652d303030373900080409020a040b000c001808041c05053130303034"
    "060e77665f6e616d652d303030323800080009030a000b000c001808046c05053130303033060e77665f6e616d65"
    "2d303031303800080109040a010b000c001808046a05053130303032060e77665f6e616d652d3030313036000801"
    "09050a010b000c001808042e05053130303038060e77665f6e616d652d303030343600080209060a020b000c00"
    "d0bf"
)


@pytest.mark.parametrize("hexv", [BATT_Q, WF_CAT_Q, WF_SEL_4F, WF_SEL_2E, WF_SHOW_FE, WF_SHOW_6A, WF_ORDER])
def test_crc_matches_tx_vectors(hexv: str) -> None:
    f = bytes.fromhex(hexv)
    crc = af.crc16_arc(f[4:-2])
    assert bytes([crc >> 8, crc & 0xFF]) == f[-2:]


def test_builders_exact_bytes() -> None:
    assert af.build_battery_query(window=1) == bytes.fromhex(BATT_Q)
    assert af.build_watchface_catalog_request(window=0) == bytes.fromhex(WF_CAT_Q)
    assert af.build_watchface_remove(0x4F, 0x04, window=1) == bytes.fromhex(WF_SEL_4F)
    assert af.build_watchface_remove(0x2E, 0x02, window=0) == bytes.fromhex(WF_SEL_2E)
    assert af.build_watchface_show(0xFE, 0x00, window=1) == bytes.fromhex(WF_SHOW_FE)
    assert af.build_watchface_show(0x6A, 0x01, window=0) == bytes.fromhex(WF_SHOW_6A)
    order = [(0x6A, 1), (0xFE, 0), (0x4F, 4), (0x16, 0), (0x1C, 0), (0x6C, 1), (0x2E, 2)]
    assert af.build_frame(0x01, 0x30, af.ROUTE_WATCHFACE, af.watchface_order_app(order)) == bytes.fromhex(WF_ORDER)


@pytest.mark.parametrize(
    ("hexv", "route"),
    [
        (BATT_R48, af.ROUTE_BATTERY),
        (BATT_R47, af.ROUTE_BATTERY),
        (WF_SEL_RESP, af.ROUTE_WATCHFACE),
        (WF_CAT_R, af.ROUTE_WATCHFACE),
    ],
)
def test_decode_rx_vectors(hexv: str, route: bytes) -> None:
    raw = bytes.fromhex(hexv)
    fr = af.decode_app_frame(raw)
    assert fr.route == route
    assert fr.app == raw[8:-2]


def test_decode_roundtrip_and_bad_crc() -> None:
    fr = af.decode_app_frame(bytes.fromhex(BATT_R48))
    assert fr.app == bytes.fromhex("4206000530")
    bad = bytearray.fromhex(BATT_R48)
    bad[-1] ^= 0xFF
    with pytest.raises(ValueError, match="bad CRC"):
        af.decode_app_frame(bytes(bad))


def test_parse_battery_response() -> None:
    assert af.parse_battery_response(bytes.fromhex("4206000530")) == 48
    assert af.parse_battery_response(bytes.fromhex("420600052f")) == 47
    assert af.parse_battery_response(b"\x01\x02\x03") is None
    assert af.parse_battery_response(bytes.fromhex("4200000530")) is None


def test_parse_watchface_catalog() -> None:
    app = bytes.fromhex(WF_CAT_R)[8:-2]
    faces = af.parse_watchface_catalog(app)
    assert len(faces) >= 7
    names = {f.name for f in faces}
    assert names == {
        "wf_name-00254", "wf_name-00022", "wf_name-00079", "wf_name-00028",
        "wf_name-00108", "wf_name-00106", "wf_name-00046",
    }
    assert all(len(f.package) == 5 for f in faces)
    assert any(f.name == "wf_name-00079" and f.face_id == 0x4F for f in faces)
    assert any(f.name == "wf_name-00046" and f.face_id == 0x2E for f in faces)
    # group id is parsed from the catalog and matches the captured frames
    # (04 4f 1d 04 -> group 4; 04 2e 1d 02 -> group 2).
    by_id = {f.face_id: f for f in faces}
    assert by_id[0x4F].group == 0x04
    assert by_id[0x2E].group == 0x02
    assert af.watchface_item(0x4F, by_id[0x4F].group) == bytes.fromhex("044f1d04")
    # only the face shown on the watch carries ``0b 01`` (face 254 when the catalogue was captured)
    assert [f.face_id for f in faces if f.active] == [0xFE]


def test_build_sap_hello_structure() -> None:
    sid = af.sap_id_from_seed("AA:BB:CC:DD:EE:FF")
    assert len(sid) == 32 and all(ch in "0123456789ABCDEF" for ch in sid)
    hello = af.build_sap_hello(sid, name="Home Assistant")
    assert len(hello) == af.HELLO_LEN == 105
    assert hello[:7] == bytes([0x68, 0x15, 0x04, 0x01, 0x00, 0xFF, 0x03])
    assert hello[7:43] == ("SAP_" + sid).encode("ascii")
    assert hello[43:50] == bytes.fromhex("0101f4000003d4")
    assert hello[50:66].rstrip(b"\x00") == b"Home Assistant"
    assert hello[98:104] == b"\xff" * 6 and hello[104] == 0x02
    # a bad id length is rejected
    import pytest
    with pytest.raises(ValueError):
        af.build_sap_hello("TOO_SHORT")


def test_is_hello_announce() -> None:
    assert af.is_hello_announce(bytes([0x68, 0x14, 0x04, 0x01]))  # watch announce
    assert not af.is_hello_announce(bytes([0x68, 0x15, 0x04]))  # our own reply
    assert not af.is_hello_announce(bytes.fromhex("000503c001500b0b02f3db"))  # app frame


def test_header_checksum_matches_every_captured_length() -> None:
    # (H0,H1) -> (H2,H3) pairs seen on the wire, both directions, short and long frames
    seen = {
        (0x00, 0x05): (0x03, 0xC0), (0x00, 0x06): (0x02, 0x80), (0x00, 0x07): (0xC2, 0x41),
        (0x00, 0x08): (0xC6, 0x01), (0x00, 0x09): (0x06, 0xC0), (0x00, 0x0A): (0x07, 0x80),
        (0x00, 0x0E): (0xC4, 0x81), (0x00, 0x13): (0xCD, 0x41), (0x00, 0x22): (0x19, 0x80),
        (0x00, 0x8E): (0x64, 0x80), (0x01, 0x0E): (0x54, 0x80), (0x01, 0xEE): (0xDC, 0x81),
    }
    for (h0, h1), (h2, h3) in seen.items():
        assert af.header_checksum(h0, h1) == (h2 << 8) | h3, (h0, h1)


def test_build_frame_any_size_roundtrips_and_matches_a_long_capture() -> None:
    app = bytes(range(256)) * 2  # 512 bytes: needs the 16-bit length
    wire = af.build_frame(0x01, 0x40, af.ROUTE_WATCHFACE, app)
    assert ((wire[0] << 8) | wire[1]) + 6 == len(wire) == 522
    d = af.decode_app_frame(wire)
    assert d.app == app and d.route == af.ROUTE_WATCHFACE
    # the real 276-byte catalog frame (H0=01) has the same shape
    cat = bytes.fromhex(WF_CAT_R)
    assert ((cat[0] << 8) | cat[1]) + 6 == len(cat) == 276


def test_frame_assembler_handles_split_merged_and_hello_frames() -> None:
    a = af.build_frame(0x00, 0x40, af.ROUTE_BATTERY, b"\x42\x06\x00\x05\x30")
    b = af.build_frame(0x00, 0x40, af.ROUTE_WATCHFACE, bytes(300))  # long frame
    hello = af.build_sap_hello(af.sap_id_from_seed("x"))
    stream = a + b + hello + a
    asm = af.FrameAssembler()
    out: list[bytes] = []
    for i in range(0, len(stream), 7):  # feed in awkward 7-byte pieces
        out += asm.feed(stream[i : i + 7])
    assert out == [a, b, hello, a]
    # a merged burst in one notification
    assert af.FrameAssembler().feed(a + a) == [a, a]
    # garbage resynchronises instead of wedging the buffer
    asm2 = af.FrameAssembler()
    assert asm2.feed(b"\xff\xff\xff") == []
    assert asm2.feed(a) == [a]


def test_is_reply_rule() -> None:
    assert af.is_reply(0x02, 0x42) and af.is_reply(0x0C, 0x4C) and af.is_reply(0xA2, 0xE2)
    assert af.is_reply(0x00, 0xC0) and af.is_reply(0x01, 0xC1)  # route 0606 catalog answers
    assert not af.is_reply(0x02, 0x02) and not af.is_reply(0x02, 0x43) and not af.is_reply(0x22, 0x42)
