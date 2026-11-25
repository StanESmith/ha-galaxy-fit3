"""GalaxyFit3Client against a fake watch (no Bluetooth, no Home Assistant).

client.py uses relative imports and bleak, so it is loaded under a throwaway package name with only
``bleak`` stubbed. The fake transport plays the watch: it ACKs every frame the client sends (route
8000, mirroring b5), answers requests, and can push unsolicited frames and its own requests.
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types

import pytest

_PKG_DIR = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "galaxy_fit3"


def _load():
    sys.modules.setdefault("bleak", types.SimpleNamespace(BleakClient=object))
    pkg = types.ModuleType("gf3")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["gf3"] = pkg
    return importlib.import_module("gf3.client"), importlib.import_module("gf3.sap.appframe"), importlib.import_module("gf3.sap.setup_sequence")


client_mod, af, ss = _load()
client_mod.SKIP_SETUP_TIME_FRAME = False  # tests exercise the full replay unless they opt in to skipping
client_mod.FRESH_PAIRING_REPLAY = False  # ... and the reconnect sequence unless they opt in to the fresh-pairing one

CAP_MAP = bytes.fromhex(
    "00533d4000400f0f0001130317020103150202031c02030302020403080205030602060319020703180208030102"
    "ff030302ff030402ff030702ff030902ff031402ff031602ff031a02ff031b02ff031d02ff031e02ffa8b7"
)
CATALOG_R = bytes.fromhex(
    "010e548000400606c10307180a04fe05053130303030060e77665f6e616d652d3030323534"
    "00080009000a000b010c000d011e011808041605053130303034060e77665f6e616d652d30"
    "3030323200080009010a000b000c001808044f05053130303032060e77665f6e616d652d30"
    "3030373900080409020a040b000c001808041c05053130303034060e77665f6e616d652d30"
    "3030323800080009030a000b000c001808046c05053130303033060e77665f6e616d652d30"
    "3031303800080109040a010b000c001808046a05053130303032060e77665f6e616d652d30"
    "3031303600080109050a010b000c001808042e05053130303038060e77665f6e616d652d30"
    "3030343600080209060a020b000c00d0bf"
)


class FakeWatch:
    """Stands in for BleTransport and plays the watch."""

    def __init__(self, *, ack: bool = True, chunk: int | None = None) -> None:
        self.ack = ack
        self.chunk = chunk
        self.is_connected = False
        self.written: list[bytes] = []  # frames the client sent (hello + app frames)
        self.hello_seen = False
        self._cb = None
        self.on_request = {}  # (route_hex, op) -> callable(app) -> list of (route, app) pushes

    async def connect(self, on_notify) -> None:
        self._cb = on_notify
        self.is_connected = True
        loop = asyncio.get_running_loop()
        loop.call_soon(self.push, bytes([0x68, 0x14]) + bytes(103))  # the watch's hello announce
        loop.call_soon(self.push, CAP_MAP)

    async def disconnect(self) -> None:
        self.is_connected = False

    def push(self, data: bytes) -> None:
        if self.chunk is None:
            self._cb(data)
        else:
            for i in range(0, len(data), self.chunk):
                self._cb(data[i : i + self.chunk])

    def push_app(self, route: bytes, app: bytes, b5: int = 0x40) -> None:
        self.push(af.build_frame(0x00, b5, route, app))

    async def write_frame(self, data: bytes) -> None:
        self.written.append(data)
        if data[0] == 0x68:
            self.hello_seen = True
            return
        fr = af.decode_app_frame(data)
        loop = asyncio.get_running_loop()
        if self.ack:
            loop.call_soon(self.push, af.build_frame(0x88, fr.b5, b"\x80\x00", b""))
        handler = self.on_request.get((fr.route.hex(), fr.app[0]))
        if handler is not None:
            for route, app in handler(fr.app):
                loop.call_later(0.01, self.push_app, route, app)

    def app_frames(self):
        return [af.decode_app_frame(w) for w in self.written if w[0] != 0x68]


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=20))


def test_connect_sends_hello_learns_caps_and_replays_setup_with_all_acks() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        return watch, cl

    watch, cl = _run(scenario())
    assert watch.hello_seen
    assert cl.capabilities is not None and cl.capabilities.channel_for(6) == 5
    sent = watch.app_frames()
    # every scripted frame once, plus the reactive acknowledgement of the capability map (route 0f0f)
    assert len(sent) == len(ss.SETUP_FRAMES) + 1
    assert [f.app for f in sent if f.route.hex() == "0f0f"] == [b"\x00"]
    # the time frame carries a plausible live epoch
    t = [f for f in sent if f.route.hex() == "0101" and f.app.startswith(bytes.fromhex("830164000200030a"))]
    assert len(t) == 1 and t[0].app[8:18].isdigit() and int(t[0].app[8:18]) > 1_700_000_000
    # window bit toggles per class: consecutive 0x4_ frames alternate 0x40/0x50
    w40 = [f.b5 for f in sent if f.b5 & 0xE0 == 0x40]
    assert w40[:4] == [0x40, 0x50, 0x40, 0x50]


def test_unacked_frames_do_not_hang_setup() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        watch.ack = False  # the watch stops ACKing
        return await cl.run_setup(ack_timeout=0.01)

    assert _run(scenario()) == 0  # nothing ACKed, but it returns instead of blocking forever


def test_battery_reply_is_matched_by_opcode_not_by_first_frame_on_the_route() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        # on a battery query the watch first pushes an unsolicited charge message, then the reply
        watch.on_request[("0b0b", 0x02)] = lambda app: [
            (af.ROUTE_BATTERY, bytes.fromhex("622101")),
            (af.ROUTE_BATTERY, bytes.fromhex("420600051d")),
        ]
        return await cl.read_battery()

    assert _run(scenario()) == 0x1D


def test_watch_face_catalog_arrives_in_small_pieces_and_show_uses_the_group() -> None:
    async def scenario():
        watch = FakeWatch(chunk=20)  # an MTU-23 link: every notification is 20 bytes
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        watch.on_request[("0606", 0x01)] = lambda app: [(af.ROUTE_WATCHFACE, af.decode_app_frame(CATALOG_R).app)]
        watch.on_request[("0606", 0x03)] = lambda app: [(af.ROUTE_WATCHFACE, bytes([0x43]) + app[1:])]
        watch.on_request[("0606", 0x0A)] = lambda app: [(af.ROUTE_WATCHFACE, bytes([0x4A]) + app[1:])]
        faces = await cl.list_watch_faces()
        await cl.set_watch_face(0x4F)
        return faces, watch

    faces, watch = _run(scenario())
    assert {f.name for f in faces} >= {"wf_name-00079", "wf_name-00046"}
    sent = [f.app for f in watch.app_frames() if f.route == af.ROUTE_WATCHFACE]
    assert not any(a[:1] == b"\x05" for a in sent)  # never the remove command
    show = [a for a in sent if a[:1] == b"\x03"]
    assert show == [bytes.fromhex("03044f1d04")]  # group 4 came from the catalog
    order = [a for a in sent if a[:1] == b"\x0a"][-1]
    assert order[:4] == bytes([0x0A, 0x19, 0x01, len(faces)])
    assert order[4:8] == bytes.fromhex("044f1d04")  # the shown face goes first
    assert len(order) == 4 + 4 * len(faces)


def test_set_watch_face_raises_when_the_watch_does_not_confirm() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        watch.on_request[("0606", 0x01)] = lambda app: [(af.ROUTE_WATCHFACE, af.decode_app_frame(CATALOG_R).app)]
        try:
            await cl.set_watch_face(0x4F, 4, timeout=0.5)
        except TimeoutError:
            return True
        return False

    assert _run(scenario())


def test_install_watch_face_runs_the_le_steps_around_the_file_transfer() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        sent_rfcomm = []
        cl._rfcomm_transfer = lambda local, name, data, hs, progress=None: sent_rfcomm.append((local, name, len(data), hs))
        watch.on_request[("0606", 0x04)] = lambda app: [(af.ROUTE_WATCHFACE, bytes([0x44, 0x16, 0x01]) + app[1:])]
        watch.on_request[("1e1e", 0x01)] = lambda app: [(b"\x1e\x1e", bytes.fromhex("410101020003060610000000"))]
        watch.on_request[("1e1e", 0x02)] = lambda app: [(b"\x1e\x1e", bytes.fromhex("4201010200"))]
        watch.on_request[("1e1e", 0x03)] = lambda app: [(b"\x1e\x1e", bytes.fromhex("4301010200"))]
        watch.on_request[("0606", 0x01)] = lambda app: [(af.ROUTE_WATCHFACE, af.decode_app_frame(CATALOG_R).app)]
        watch.on_request[("0606", 0x0A)] = lambda app: [(af.ROUTE_WATCHFACE, bytes([0x4A]) + app[1:])]
        before = len(watch.written)
        await cl.install_watch_face(0x4F, b"x" * 1000, "00:00:00:00:00:02", style=4)
        return watch, sent_rfcomm, before

    watch, sent_rfcomm, before = _run(scenario())
    assert sent_rfcomm == [("00:00:00:00:00:02", "SM-R390_00079_256x402.bin", 1000, False)]
    later = [af.decode_app_frame(w) for w in watch.written[before:]]
    steps = [(f.route.hex(), f.app[0]) for f in later if f.route in (af.ROUTE_WATCHFACE, b"\x1e\x1e")]
    assert steps == [("0606", 0x04), ("1e1e", 0x01), ("1e1e", 0x02), ("1e1e", 0x03), ("0606", 0x01), ("0606", 0x0A)]
    install = [f.app for f in watch.app_frames() if f.route == af.ROUTE_WATCHFACE and f.app[0] == 0x04][0]
    assert install == bytes.fromhex("04044f1d04")


def test_install_handshake_only_skips_the_install_request_and_still_closes_the_session() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()

        def boom(*_args):
            raise OSError("Host is down")

        cl._rfcomm_transfer = boom
        watch.on_request[("1e1e", 0x01)] = lambda app: [(b"\x1e\x1e", bytes.fromhex("410101020003060610000000"))]
        watch.on_request[("1e1e", 0x02)] = lambda app: [(b"\x1e\x1e", bytes.fromhex("4201010200"))]
        watch.on_request[("1e1e", 0x03)] = lambda app: [(b"\x1e\x1e", bytes.fromhex("4301010200"))]
        before = len(watch.written)
        try:
            await cl.install_watch_face(16, b"x" * 10, "00:00:00:00:00:02", handshake_only=True)
        except OSError:
            pass
        return watch, before

    watch, before = _run(scenario())
    later = [af.decode_app_frame(w) for w in watch.written[before:]]
    steps = [(f.route.hex(), f.app[0]) for f in later if f.route in (af.ROUTE_WATCHFACE, b"\x1e\x1e")]
    assert steps == [("1e1e", 0x01), ("1e1e", 0x02), ("1e1e", 0x03)]  # no 0606 04; session closed after the failure


def test_watch_requests_are_answered_reactively() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        before = len(watch.written)
        watch.push_app(bytes.fromhex("0a0a"), bytes.fromhex("a202e01927e12d01"))  # counter 0x19
        watch.push_app(bytes.fromhex("0808"), bytes.fromhex("0d"))
        await asyncio.sleep(0.2)
        return [af.decode_app_frame(w) for w in watch.written[before:]]

    replies = {f.route.hex(): f.app for f in _run(scenario())}
    reactive = importlib.import_module("gf3.sap.reactive")
    expected_0a0a = reactive.reply_for(  # matches whatever the S-Health-installed experiment flag is set to
        bytes.fromhex("0a0a"), bytes.fromhex("a202e01927e12d01"),
        claim_health_installed=reactive.CLAIM_HEALTH_APP_INSTALLED,
    )
    assert replies["0a0a"] == expected_0a0a  # echoes the watch's counter
    assert replies["0808"] == bytes.fromhex("cd2400000000")


def test_timezone_from_the_host_reaches_the_time_frame_and_the_reply() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client(
            "AA:BB:CC:DD:EE:FF", transport=watch, tz_provider=lambda: (32400, "Japan Standard Time")
        )
        await cl.connect()
        before = len(watch.written)
        watch.push_app(bytes.fromhex("0808"), bytes.fromhex("0c"))
        await asyncio.sleep(0.2)
        replies = [af.decode_app_frame(w) for w in watch.written[before:]]
        return watch, replies

    watch, replies = _run(scenario())
    time_frame = [f for f in watch.app_frames() if f.route.hex() == "0101" and f.app.startswith(bytes.fromhex("830164000200030a"))][0]
    assert time_frame.app.endswith(bytes.fromhex("04009 07e0500".replace(" ", "")))  # UTC+9, sign 0
    tz_reply = [f for f in replies if f.route.hex() == "0808"][0]
    assert tz_reply.app == bytes.fromhex("4c27010900907e2813") + b"Japan Standard Time"


def test_capability_map_repeats_are_answered_at_most_twice_in_the_right_class() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        before = len(watch.written)
        for _ in range(10):  # the watch keeps resending the map
            watch.push(CAP_MAP)
        await asyncio.sleep(0.3)
        return watch, [af.decode_app_frame(w) for w in watch.written[before:]]

    watch, later = _run(scenario())
    acks = [f for f in watch.app_frames() if f.route.hex() == "0f0f"]
    assert len(acks) == 2  # one during connect + one more, never a flood
    assert all(f.b5 & 0xE0 == 0x20 for f in acks)  # 0x20 class like the real phone


def test_acks_are_matched_even_when_the_watch_reports_the_opposite_window() -> None:
    """Live, the watch's ACK carries the other window value than the frame it acknowledges."""

    class InvertingWatch(FakeWatch):
        async def write_frame(self, data: bytes) -> None:
            self.written.append(data)
            if data[0] == 0x68:
                self.hello_seen = True
                return
            fr = af.decode_app_frame(data)
            inverted = fr.b5 ^ 0x10
            asyncio.get_running_loop().call_soon(self.push, af.build_frame(0x88, inverted, b"\x80\x00", b""))

    async def scenario():
        watch = InvertingWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        return await cl.run_setup(ack_timeout=1.0)

    assert _run(scenario()) == len(ss.SETUP_FRAMES)  # every frame counted as ACKed, none timed out


_TIME_PREFIX = bytes.fromhex("830164000200030a")


def _time_frames(watch):
    return [f for f in watch.app_frames() if f.route.hex() == "0101" and f.app.startswith(_TIME_PREFIX)]


def test_set_time_sends_live_epoch_and_host_offset_and_logs_the_reply() -> None:
    async def scenario():
        watch = FakeWatch()
        watch.on_request[("0101", 0x83)] = lambda app: [(b"\x01\x01", bytes.fromhex("43000000000000"))]
        cl = client_mod.GalaxyFit3Client(
            "AA:BB:CC:DD:EE:FF", transport=watch, tz_provider=lambda: (-14400, "Eastern Daylight Time")
        )
        await cl.connect()
        assert len(_time_frames(watch)) == 1
        cl._last_time_sync = None
        ok = await cl.set_time()
        return watch, cl, ok

    watch, cl, ok = _run(scenario())
    frames = _time_frames(watch)
    assert ok and len(frames) == 2  # once in the setup replay, once on demand
    assert frames[1].app[8:18].isdigit() and int(frames[1].app[8:18]) > 1_700_000_000
    assert frames[1].app.endswith(bytes.fromhex("0140380500"))  # UTC-4 (DST): sign 1, 14400 s LE
    assert cl.last_time_sync is not None


def test_set_time_offset_override_and_not_connected() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        try:
            await cl.set_time()
        except Exception as err:  # noqa: BLE001
            first = type(err).__name__
        await cl.connect()
        await cl.set_time(tz_offset_seconds=32400)
        return watch, first

    watch, first = _run(scenario())
    assert first == "NotConnected"
    assert _time_frames(watch)[-1].app.endswith(bytes.fromhex("04009 07e0500".replace(" ", "")))


def test_set_time_reports_unacked_frame() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        watch.ack = False
        return await cl.set_time()

    assert _run(scenario()) is False


def test_sync_time_if_due_only_resends_after_the_interval() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()  # the setup replay counts as a sync
        fresh = await cl.sync_time_if_due(3600)
        cl._last_time_sync -= 3601
        stale = await cl.sync_time_if_due(3600)
        return watch, fresh, stale

    watch, fresh, stale = _run(scenario())
    assert (fresh, stale) == (False, True)
    assert len(_time_frames(watch)) == 1  # the replay's 0101 frame; the later sync uses the 0b0b message
    assert len(_rtc_frames(watch)) == 1


def _rtc_frames(watch):
    """App frames on the settings route (0b0b) that look like the phone's set-time message (id 0, 4 params)."""
    return [f for f in watch.app_frames() if f.route.hex() == "0b0b" and f.app[:2] == b"\x80\x04"]


def test_set_time_rtc_sends_the_service_11_message_with_selectable_layout() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client(
            "AA:BB:CC:DD:EE:FF", transport=watch, tz_provider=lambda: (-18000, "Eastern Standard Time")
        )
        await cl.connect()
        ok_little = await cl.set_time_rtc()  # defaults: little-endian, offset in seconds (the phone's layout)
        ok_big = await cl.set_time_rtc(byte_order="big", offset_unit="minutes", tz_offset_seconds=-18000)
        return watch, ok_little, ok_big

    watch, ok_little, ok_big = _run(scenario())
    frames = _rtc_frames(watch)
    assert ok_big and ok_little and len(frames) == 2
    little, big = frames[0].app, frames[1].app
    # header 0x80 (variable, request, id 0), 4 params, then [id][value] entries and a bare offset short
    assert little[0] == 0x80 and little[1] == 4
    assert little[-3:] == b"\x01" + (18000).to_bytes(2, "little")  # tz sign param 2 = 1 (west), 18000 s LE: 01 50 46
    assert big[-3:] == b"\x01" + (300).to_bytes(2, "big")  # sign 1, 300 minutes big-endian
    assert int.from_bytes(little[5:13], "little") > 1_700_000_000  # param 1: epoch, 8 bytes


def test_set_time_rtc_requires_a_connection() -> None:
    async def scenario():
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=FakeWatch())
        try:
            await cl.set_time_rtc()
        except Exception as err:  # noqa: BLE001
            return type(err).__name__

    assert _run(scenario()) == "NotConnected"


def test_replay_limit_sends_only_the_first_frames() -> None:
    async def scenario(limit):
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        cl.set_replay_limit(limit)
        await cl.connect()
        return watch

    full = _run(scenario(None))
    none = _run(scenario(0))
    first7 = _run(scenario(7))
    assert len(_time_frames(full)) == 1 and not _time_frames(none)
    assert len(_time_frames(first7)) == 1  # the time frame is the 7th scripted frame
    assert len(first7.app_frames()) < len(full.app_frames())


def test_skip_time_frame_leaves_out_only_the_time_frame_and_the_sync_uses_the_rtc_message() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        cl.set_skip_time_frame(True)
        await cl.connect()
        return watch, cl, await cl.sync_time_if_due()

    watch, cl, resent = _run(scenario())
    assert len(_time_frames(watch)) == 0
    assert len(watch.app_frames()) == len(ss.SETUP_FRAMES) + 1  # hello + all frames but the time frame + the RTC message
    assert resent is True and len(_rtc_frames(watch)) == 1


def test_time_variants_have_the_expected_shape() -> None:
    std = ss.build_time_app_variant("standard", 1700000000, -18000)
    assert std == ss.build_time_app(1700000000, -18000)
    assert ss.build_time_app_variant("tz_zero", 1700000000, -18000).endswith(b"1700000000\x04\x00\x00\x00\x05\x00")
    assert ss.build_time_app_variant("local_epoch", 1700000000, -18000).find(b"1699982000") > 0
    ms = ss.build_time_app_variant("millis", 1700000000, -18000)
    assert ms[7] == 0x0D and b"1700000000000" in ms
    assert b"\x04" not in ss.build_time_app_variant("no_tz", 1700000000, -18000)[8 + 10 :-2]


def test_set_time_rtc_matches_the_frame_the_phone_sent() -> None:
    """The 13:22:10 timezone-change message from the 2026-11-15 phone capture, rebuilt byte for byte."""
    am = importlib.import_module("gf3.sap.app_message")
    phone = bytes.fromhex("800400000102b2f96a00000000040002015046")
    assert am.build_set_time(0x6AF9B202, is_24h=False, tz_offset_seconds=-18000, byte_order="little", offset_unit="seconds") == phone


def test_fresh_pairing_replay_sends_the_captured_first_connection_without_the_time_frame() -> None:
    fp = importlib.import_module("gf3.sap.fresh_pairing")

    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        cl.set_fresh_pairing(True)
        await cl.connect()
        return watch

    watch = _run(scenario())
    sent = [(f.route.hex(), f.app.hex()) for f in watch.app_frames()]
    it = iter(sent)  # the capture's frames appear in order (reactive replies may be interleaved)
    assert all(any(frame == (r, a) for frame in it) for _, r, a in fp.FRESH_FRAMES)
    assert not any(r == "0101" and a.startswith("830164") for r, a in sent)  # never the 0101 time frame
    assert ("0606", "01") in sent  # the fresh-pairing flag the reconnect sequence lacks


def test_watchface_label_roundtrip() -> None:
    af = importlib.import_module("gf3.sap.appframe")
    faces = [af.WatchFace(face_id=46, package="10008", name="wf_name-00046", group=2),
             af.WatchFace(face_id=22, package="10004", name="wf_name-00022", group=0)]
    assert af.watchface_label(faces[0]) == "Face 46"
    assert af.watchface_by_label(faces, "Face 22").group == 0
    assert af.watchface_by_label(faces, "Face 999") is None


def test_health_sync_capability_is_answered_and_records_reach_the_status() -> None:
    hs = importlib.import_module("gf3.sap.healthsync")
    reactive = importlib.import_module("gf3.sap.reactive")
    cap = bytes.fromhex("a017e01527a001a10433") + bytes(range(256)) * 3  # > one 500-byte frame
    now = int(__import__("time").time())
    t = hs.to_protocol_time(now).to_bytes(4, "little")
    step = (b"\x01" + t + b"\x02" + t + b"\x03" + bytes(4) + b"\x05" + t + b"\x10" + (321).to_bytes(4, "little")
            + b"\x11" + bytes(4) + b"\x12" + bytes(4) + b"\x13" + bytes(4) + b"\x14" + bytes(4)
            + b"\x15" + bytes(4) + b"\x16" + bytes(4) + b"\x17\x01")
    hr = b"\x01" + t + b"\x02" + t + b"\x20\x4d"
    sync = bytes.fromhex("8203e00900") + b"\x07\x00" + step + b"\x0a\x00" + len(hr).to_bytes(4, "little") + hr

    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch, tz_provider=lambda: (0, "UTC"))
        await cl.connect()
        before = len(watch.written)
        # the capability request arrives as two SAP fragments; the second has no route
        watch.push(af.build_frame(0x02, 0x40, hs.ROUTE_HEALTH, cap[:490]))
        watch.push(af.build_frame(0x06, 0x41, cap[490:492], cap[492:]))
        watch.push_app(hs.ROUTE_HEALTH, sync)
        await asyncio.sleep(0.3)
        status = await cl.read_status()
        return [af.decode_app_frame(w) for w in watch.written[before:]], status

    frames, status = _run(scenario())
    sent = [f.app for f in frames if f.route == hs.ROUTE_HEALTH]
    assert any(a[:5] == hs.build_sync_ack(9)[:5] for a in sent)
    assert (hs.build_capability_response(reactive.HEALTH_APP_VERSION_CODE) in sent) == reactive.CLAIM_HEALTH_APP_INSTALLED
    assert status.heart_rate == 77
    assert status.steps in (321, 0)  # 0 only if the test straddles UTC midnight


def test_status_poll_requests_a_health_sync_once_the_watch_has_asked_for_capability() -> None:
    hs = importlib.import_module("gf3.sap.healthsync")

    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        watch.on_request[("0b0b", 0x02)] = lambda app: [(af.ROUTE_BATTERY, bytes.fromhex("4206000542"))]
        await cl.read_status()
        quiet = [f for f in watch.app_frames() if f.route == hs.ROUTE_HEALTH and f.app[:1] == b"\x03"]
        watch.push_app(hs.ROUTE_HEALTH, bytes.fromhex("a017e01527a001a10433") + bytes(20))
        await asyncio.sleep(0.1)
        await cl.read_status()
        await cl.read_status()  # inside the interval: no second request
        asked = [f for f in watch.app_frames() if f.route == hs.ROUTE_HEALTH and f.app[:1] == b"\x03"]
        return quiet, asked

    quiet, asked = _run(scenario())
    assert quiet == [] and len(asked) == 1
    assert asked[0].app == hs.build_request_data(1, 0)


def test_the_distance_unit_goes_in_the_phone_sync_once_per_connection() -> None:
    hs = importlib.import_module("gf3.sap.healthsync")

    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch, distance_unit_provider=lambda: "mi")
        await cl.connect()
        watch.push_app(hs.ROUTE_HEALTH, bytes.fromhex("a017e01527a001a10433") + bytes(20))
        await asyncio.sleep(0.1)
        for _ in range(2):  # two syncs: the watch answers each REQUEST_DATA
            watch.push_app(hs.ROUTE_HEALTH, bytes.fromhex("43e0010004020101") + bytes(30))
            await asyncio.sleep(0.1)
        return [f.app for f in watch.app_frames() if f.route == hs.ROUTE_HEALTH and f.app[:1] == b"\x82"]

    sent = _run(scenario())
    assert len(sent) == 2
    assert sent[0][1] == 0x03
    assert sent[0].endswith(b"\x07\x02") and sent[1][1] == 0x02  # the second sync carries no profile


def test_set_distance_unit_sends_a_profile_and_rejects_other_units() -> None:
    hs = importlib.import_module("gf3.sap.healthsync")

    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        ok = await cl.set_distance_unit("km")
        with pytest.raises(ValueError):
            await cl.set_distance_unit("yd")
        return ok, [f.app for f in watch.app_frames() if f.route == hs.ROUTE_HEALTH]

    ok, frames = _run(scenario())
    assert ok and len(frames) == 1
    assert frames[0][:2] == b"\x82\x03" and frames[0].endswith(b"\x07\x01")


def test_sync_health_returns_once_the_watch_records_arrive_and_are_acknowledged() -> None:
    hs = importlib.import_module("gf3.sap.healthsync")
    now = int(__import__("time").time())
    t = hs.to_protocol_time(now).to_bytes(4, "little")
    hr = b"\x01" + t + b"\x02" + t + b"\x20\x58"
    sync = bytes.fromhex("8203e00900") + b"\x0a\x00" + len(hr).to_bytes(4, "little") + hr

    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        watch.push_app(hs.ROUTE_HEALTH, bytes.fromhex("a017e01527a001a10433") + bytes(20))  # the watch opens the service
        await asyncio.sleep(0.1)
        # the watch answers the request with its records
        watch.on_request[(hs.ROUTE_HEALTH.hex(), hs.OP_REQUEST_DATA)] = lambda app: [(hs.ROUTE_HEALTH, sync)]
        before = len(watch.written)
        ok = await cl.sync_health(timeout=3.0, quiet=0.2)
        sent = [f.app for f in watch.app_frames() if f.route == hs.ROUTE_HEALTH]
        return ok, await cl.read_heart_rate(), sent, before

    ok, heart_rate, sent, _before = _run(scenario())
    assert ok is True and heart_rate == 0x58
    assert any(a[:1] == bytes([hs.OP_SYNC_DATA_RESPONSE]) for a in sent)  # our acknowledgement went out


def test_sync_health_gives_up_when_the_watch_never_opens_the_health_service() -> None:
    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        return await cl.sync_health(timeout=0.5, ready_timeout=0.3, quiet=0.1)

    assert _run(scenario()) is False


def test_sync_health_waits_past_the_watchs_empty_first_sync_data_for_the_records() -> None:
    hs = importlib.import_module("gf3.sap.healthsync")
    now = int(__import__("time").time())
    t = hs.to_protocol_time(now).to_bytes(4, "little")
    hr = b"\x01" + t + b"\x02" + t + b"\x20\x59"
    empty = bytes.fromhex("8203e00900")
    records = bytes.fromhex("8204e00900") + b"\x0a\x00" + len(hr).to_bytes(4, "little") + hr

    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        watch.push_app(hs.ROUTE_HEALTH, bytes.fromhex("a017e01527a001a10433") + bytes(20))
        await asyncio.sleep(0.1)
        loop = asyncio.get_running_loop()

        def answer(_app):  # the watch sends an empty SYNC_DATA at once and the records a moment later
            loop.call_later(0.4, watch.push_app, hs.ROUTE_HEALTH, records)
            return [(hs.ROUTE_HEALTH, empty)]

        watch.on_request[(hs.ROUTE_HEALTH.hex(), hs.OP_REQUEST_DATA)] = answer
        ok = await cl.sync_health(timeout=5.0, quiet=0.8)
        return ok, await cl.read_heart_rate()

    ok, heart_rate = _run(scenario())
    assert ok is True and heart_rate == 0x59


def test_every_heart_rate_record_is_kept_and_only_new_ones_mark_their_hour_dirty() -> None:
    hs = importlib.import_module("gf3.sap.healthsync")
    t0 = int(__import__("time").time()) // 3600 * 3600 - 7200  # the top of an hour two hours back

    def sync(starts_and_bpm):
        body = b""
        for start, bpm in starts_and_bpm:
            t = hs.to_protocol_time(start).to_bytes(4, "little")
            rec = b"\x01" + t + b"\x02" + t + b"\x20" + bytes([bpm])
            body += b"\x0a\x00" + len(rec).to_bytes(4, "little") + rec
        return bytes.fromhex("8203e00900") + body

    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        watch.push_app(hs.ROUTE_HEALTH, sync([(t0 + 60, 60), (t0 + 3600 + 60, 90)]))
        await asyncio.sleep(0.1)
        first = cl.take_dirty_heart_rate_hours()
        watch.push_app(hs.ROUTE_HEALTH, sync([(t0 + 60, 60), (t0 + 3600 + 60, 90)]))  # the watch re-sends them
        await asyncio.sleep(0.1)
        again = cl.take_dirty_heart_rate_hours()
        watch.push_app(hs.ROUTE_HEALTH, sync([(t0 + 120, 64)]))
        await asyncio.sleep(0.1)
        later = cl.take_dirty_heart_rate_hours()
        return first, again, later, cl.heart_rate_records(), t0

    first, again, later, records, t0 = _run(scenario())
    assert first == {t0, t0 + 3600}
    assert again == set()  # identical records are not changes
    assert later == {t0}
    assert sorted(records) == [t0 + 60, t0 + 120, t0 + 3600 + 60]


def test_restored_heart_rate_records_are_not_marked_dirty_and_seed_the_newest_value() -> None:
    cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=FakeWatch())
    cl.restore_heart_rate_records({1000: (61, None, None, None), 5000: (88, None, None, None)})
    assert cl.take_dirty_heart_rate_hours() == set()
    assert cl.heart_rate_time == 5000
    assert _run(cl.read_heart_rate()) == 88


def test_a_heart_rate_record_with_the_watchs_pre_clock_date_is_ignored() -> None:
    hs = importlib.import_module("gf3.sap.healthsync")
    bogus = 1_638_332_408  # 2021-12-01, seen live
    t = hs.to_protocol_time(bogus).to_bytes(4, "little")
    rec = b"\x01" + t + b"\x02" + t + b"\x20\x42"
    sync = bytes.fromhex("8203e00900") + b"\x0a\x00" + len(rec).to_bytes(4, "little") + rec

    async def scenario():
        watch = FakeWatch()
        cl = client_mod.GalaxyFit3Client("AA:BB:CC:DD:EE:FF", transport=watch)
        await cl.connect()
        watch.push_app(hs.ROUTE_HEALTH, sync)
        await asyncio.sleep(0.1)
        return cl.heart_rate_records(), cl.take_dirty_heart_rate_hours()

    records, dirty = _run(scenario())
    assert records == {} and dirty == set()
