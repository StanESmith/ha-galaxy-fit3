"""High-level Galaxy Fit3 client: the integration's capability surface.

Composes the SAP stack over BLE into the product capabilities: battery, heart rate and steps, clock
sync, watch-face switching and face installs.

Over BLE the Fit3 secures the link with an **LE Secure Connections bond** (the OS/BlueZ handles it) and
then runs **SAP in plaintext** on the data pipe. The live path is:

    bleak connect (the OS performs the LE-SC bond)
      -> open the 0x1A1A pipe, enable notifications      [ble.BleTransport]
      -> plaintext app frames per feature, selected by a 2-byte route  [sap.appframe]

Each frame is ``[H0][H1][H2][H3][B4][B5][route 2B][app TLV][CRC-16/ARC 2B]`` (see ``sap/appframe.py``).
Features multiplex over a 2-byte **route**, not the capability map's channels: battery and the clock on
route 0b0b, watch faces on 0606, health sync on 0a0a, and face installs over a Bluetooth Classic RFCOMM
link opened through route 1e1e. Response reassembly is one GATT notification at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from . import heart_rate_history
from .sap import fresh_pairing, app_message, appframe, capability, faceinstall, healthsync, reactive, setup_sequence, tzinfo
from .sap.ble import BleTransport
from .sap.errors import NotConnected
from .sap.transport import MessageChannel

_LOGGER = logging.getLogger(__name__)

# The route-0101 time frame from the phone's setup exchange drives this watch to 2106-02-07 and a reboot
# whatever epoch it carries; the phone's own route-0b0b message (``set_time_rtc``) sets the clock correctly
# (checked against a full phone capture, 2026-11-15), so the setup replay leaves the 0101 frame out.
SKIP_SETUP_TIME_FRAME = True

# Replay the phone's captured first-pairing connection (sap/fresh_pairing.py) instead of the reconnect
# sequence. Only a factory-fresh watch needs this; once setup is complete the normal SETUP_FRAMES
# sequence (which includes the 0101 84 01 01 setup-completion frame) is what the watch expects.
FRESH_PAIRING_REPLAY = False

# Identity strings in our SAP hello. Using the phone's own made no difference to how the watch treats the
# 0b0b time message, so we keep our own.
HELLO_NAME, HELLO_MANUFACTURER, HELLO_MODEL = "Home Assistant", "Home Assistant", "Home Assistant"



HEALTH_SYNC_INTERVAL = 240.0  # seconds between sync requests we send (the status poll is 5 minutes)


@dataclass(slots=True)
class HealthState:
    """Latest health values the watch has pushed in SYNC_DATA messages (route 0a0a)."""

    last_sync: int = 0  # protocol seconds of the last SYNC_DATA received from the watch (0 = never)
    heart_rate: int | None = None
    heart_rate_time: int | None = None  # Unix seconds of the newest heart-rate record
    # step-count records keyed by their start time (Unix seconds); re-sent records overwrite
    step_bins: dict[int, int] = field(default_factory=dict)
    # the same records' calories (kcal) and distance (metres), by start time
    step_calories: dict[int, float] = field(default_factory=dict)
    step_distance: dict[int, float] = field(default_factory=dict)
    resting_heart_rate: int | None = None  # newest heart-rate record tagged as a resting heart rate
    resting_time: int | None = None
    # every heart-rate record the watch has sent, keyed by start time: (bpm, min, max, tag). Kept
    # for good (the integration persists them) so they can be graphed; re-sent records overwrite.
    hr_records: dict[int, tuple[int, int | None, int | None, int | None]] = field(default_factory=dict)
    dirty_hours: set[int] = field(default_factory=set)  # hours whose records changed since last taken
    sync_messages: int = 0

    def apply(self, data: healthsync.SyncData) -> None:
        self.sync_messages += 1
        if data.op == healthsync.OP_SYNC_DATA:
            self.last_sync = healthsync.to_protocol_time(time.time())
        for hr in data.heart_rates:
            if hr.bpm and hr.tag == healthsync.TAG_RESTING_HEART_RATE and (self.resting_time is None or (hr.start or 0) >= self.resting_time):
                self.resting_heart_rate, self.resting_time = hr.bpm, hr.start or 0
            if hr.bpm and (self.heart_rate_time is None or (hr.start or 0) >= self.heart_rate_time):
                self.heart_rate, self.heart_rate_time = hr.bpm, hr.start or 0
            if hr.bpm and hr.start is not None and heart_rate_history.is_plausible(hr.start):
                rec = (hr.bpm, hr.bpm_min, hr.bpm_max, hr.tag)
                if self.hr_records.get(hr.start) != rec:
                    self.hr_records[hr.start] = rec
                    self.dirty_hours.add(hr.start - hr.start % 3600)
        for st in data.steps:
            if st.count is not None and st.start is not None:
                self.step_bins[st.start] = st.count
            if st.start is not None and st.calorie is not None:
                self.step_calories[st.start] = st.calorie
            if st.start is not None and st.distance is not None:
                self.step_distance[st.start] = st.distance
        if len(self.step_bins) > 4096:  # keep the newest records only
            oldest = sorted(self.step_bins)[-2048]
            for records in (self.step_bins, self.step_calories, self.step_distance):
                for key in [key for key in records if key < oldest]:
                    del records[key]

    def steps_since(self, unix_seconds: float) -> int | None:
        """Sum of the step records starting at or after ``unix_seconds``; ``None`` before any sync."""
        if not self.step_bins:
            return None
        return sum(n for start, n in self.step_bins.items() if start >= unix_seconds)

    def calories_since(self, unix_seconds: float) -> float | None:
        """Kilocalories of the step records starting at or after ``unix_seconds``; ``None`` if none carried any."""
        if not self.step_calories:
            return None
        return sum(kcal for start, kcal in self.step_calories.items() if start >= unix_seconds)

    def distance_since(self, unix_seconds: float) -> float | None:
        """Metres of the step records starting at or after ``unix_seconds``; ``None`` if none carried any."""
        if not self.step_distance:
            return None
        return sum(metres for start, metres in self.step_distance.items() if start >= unix_seconds)


@dataclass(slots=True)
class WatchStatus:
    battery_level: int | None = None
    heart_rate: int | None = None
    steps: int | None = None  # today
    calories: float | None = None  # kcal, today
    distance: float | None = None  # metres, today
    resting_heart_rate: int | None = None


class GalaxyFit3Client:
    """Orchestrates the plaintext SAP stack over a single BLE connection to the watch."""

    def __init__(
        self,
        address: str,
        *,
        local_address: str = "",
        transport: BleTransport | None = None,
        resolve_device=None,
        connector=None,
        tz_provider=None,
        is_24h_provider=None,
        distance_unit_provider=None,
        id_a: bytes | None = None,
        id_b: bytes | None = None,
    ) -> None:
        self._address = address
        # () -> (utc_offset_seconds, display_name): evaluated whenever the timezone is sent, so a DST
        # change is picked up on the next connection. Defaults to the captured US Eastern sample.
        self._tz_provider = tz_provider
        # () -> bool: the configured clock format, read whenever the time is sent (options flow).
        self._is_24h_provider = is_24h_provider
        # () -> "km" | "mi" | None: the distance unit the watch should show (None = leave it alone).
        self._distance_unit_provider = distance_unit_provider
        self._profile_unit_sent: str | None = None  # the unit sent on this connection
        self._transport = transport or BleTransport(
            address, resolve_device=resolve_device, connector=connector
        )
        # id_a = remote(watch) BT address, id_b = local(HA) BT address (hello/connection identity).
        self._id_a = id_a if id_a is not None else address.encode("ascii", "ignore")
        self._id_b = id_b if id_b is not None else local_address.encode("ascii", "ignore")
        self._channel: MessageChannel | None = None
        self._caps: capability.ServiceChannelMap | None = None
        self._faces: list[appframe.WatchFace] = []
        # request/reply plumbing: route -> (request opcode, future for the matching reply frame)
        self._pending: dict[bytes, tuple[int, asyncio.Future[appframe.AppFrame]]] = {}
        # frames are serialized (one in flight, ACKed by the watch before the next); the b5 window
        # bit toggles per class (0x40 for most routes, 0x20 for the route-0606 / capability ones)
        self._tx_lock = asyncio.Lock()
        self._fresh_pairing = FRESH_PAIRING_REPLAY  # diagnostics: replay the phone's first-pairing connection
        self._skip_time_frame = SKIP_SETUP_TIME_FRAME  # leave the setup replay's 0101 time frame out
        self._replay_limit: int | None = None  # None = the whole setup replay; N = only the first N frames (diagnostics)
        self._win: dict[int, int] = {}
        self._asm = appframe.FrameAssembler()
        self._reasm = appframe.MessageReassembler()  # joins SAP-fragmented messages
        self._health = HealthState()
        self._large = healthsync.LargeDataReceiver()
        self._last_raw: tuple[bytes, float] = (b"", 0.0)  # drops a notification delivered twice
        self._health_seq = 0  # sequence number for health messages we originate
        self._health_ready = False  # the watch has sent its capability request on this connection
        self._health_requested = float("-inf")  # time.monotonic() of our last sync request (never, yet)
        self._health_synced = asyncio.Event()  # set when the watch has sent SYNC_DATA (the first one is empty)
        self._health_last_rx = 0.0  # time.monotonic() of the last message on the health route
        self._tasks: set[asyncio.Task] = set()
        self._replies_sent: Counter[tuple[str, int]] = Counter()  # reactive replies per (route, op)
        self._hello_announce: asyncio.Future[bytes] | None = None
        # The watch ACKs each phone frame on route 8000 (b4=0x88) within ~100 ms. Live, the ACK's b5
        # carries the OPPOSITE window value to the frame it acknowledges (the recorded capture mirrored
        # it), so ACKs are not matched on b5: frames are sent one at a time, so the next ACK is the one.
        self._ack_waiter: asyncio.Future[None] | None = None
        self._rx_routes: Counter[str] = Counter()  # watch->host frames seen, by route (diagnostics)
        self._ack_ok = False  # whether the most recent _request frame was ACKed
        self._last_time_sync: float | None = None  # time.monotonic() of the last time frame the watch ACKed

    @property
    def address(self) -> str:
        return self._address

    def _tz(self) -> tuple[int, str]:
        return self._tz_provider() if self._tz_provider else (tzinfo.DEFAULT_OFFSET_S, tzinfo.DEFAULT_NAME)

    def _is_24h(self) -> bool:
        return bool(self._is_24h_provider()) if self._is_24h_provider else False

    async def _mtu_ok(self) -> bool:
        """True when the ATT MTU is large enough that a clock frame fits one BLE packet (see
        :attr:`sap.ble.BleTransport.mtu_ok`), re-reading it first if it still looks like the default.
        Defaults to True for transports that don't report it (the in-memory test fake), which never split."""
        refresh = getattr(self._transport, "refresh_mtu", None)
        if refresh is not None:
            return bool(await refresh())
        return bool(getattr(self._transport, "mtu_ok", True))

    @property
    def last_time_sync(self) -> float | None:
        """``time.monotonic()`` of the last time frame the watch ACKed on this connection, or None."""
        return self._last_time_sync

    @property
    def is_connected(self) -> bool:
        return self._transport.is_connected

    @property
    def capabilities(self) -> capability.ServiceChannelMap | None:
        """The service->channel map learned from the watch, once it has advertised it."""
        return self._caps

    # -- lifecycle -------------------------------------------------------------------------
    async def connect(self, *, hello_timeout: float = 15.0) -> None:
        """BLE bond + open the SAP pipe + do the SAP hello handshake.

        bleak's ``connect`` triggers the OS LE Secure Connections pairing/bond (BlueZ agent handles
        Just-Works or a passkey) -- no handshake of our own on top. We open the 0x1A1A pipe, enable notifications,
        wait for the watch's hello announce, and reply with our ``SAP_<id>`` hello
        (both 105-byte 0x68 frames). The watch then advertises its capability map, which
        :meth:`_on_frame` parses.
        """
        self._channel = MessageChannel(self._transport.write_frame)
        self._caps = None
        self._ack_waiter = None
        self._rx_routes.clear()
        self._win.clear()
        self._last_time_sync = None
        self._asm.reset()
        self._reasm.reset()
        self._large.reset()
        self._health_ready = False
        self._profile_unit_sent = None
        self._health_synced.clear()
        self._replies_sent.clear()
        loop = asyncio.get_running_loop()
        self._hello_announce = loop.create_future()
        await self._transport.connect(self._on_frame)
        _LOGGER.debug("Fit3 %s: BLE connected + bonded; awaiting SAP announce", self._address)

        # Wait for the watch's hello announce (best effort), then reply with our hello.
        try:
            await asyncio.wait_for(self._hello_announce, timeout=hello_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.debug("no SAP announce from %s within %.0fs; sending hello anyway",
                            self._address, hello_timeout)
        seed = (self._id_b.decode("ascii", "ignore") or self._address) or "homeassistant"
        hello = appframe.build_sap_hello(
            appframe.sap_id_from_seed(seed), name=HELLO_NAME, manufacturer=HELLO_MANUFACTURER, model=HELLO_MODEL
        )
        await self._transport.write_frame(hello)
        _LOGGER.debug("Fit3 %s: sent SAP hello", self._address)

        # Wait briefly for the watch's capability map, then replay the phone's setup exchange so the
        # watch's pairing screen completes (see sap/setup_sequence.py).
        for _ in range(50):
            if self._caps is not None:
                break
            await asyncio.sleep(0.1)
        await self.run_setup()

    async def run_setup(self, ack_timeout: float = 2.0) -> int:
        """Send the phone-initiated setup frames, waiting for the watch's ACK after each.

        The watch's own requests during this time (timezone, config, ...) are answered by
        :meth:`_on_frame` via :mod:`.reactive`. Returns the number of frames ACKed.
        """
        if self._fresh_pairing:
            return await self._run_fresh_pairing(ack_timeout)
        acked = 0
        missing: list[str] = []
        frames = setup_sequence.SETUP_FRAMES if self._replay_limit is None else setup_sequence.SETUP_FRAMES[: self._replay_limit]
        if self._replay_limit is not None:
            _LOGGER.warning("Fit3 %s: setup replay limited to the first %d of %d frames (diagnostic)",
                            self._address, len(frames), len(setup_sequence.SETUP_FRAMES))
        for _h2, _h3, _b4, b5, route_hex, app_hex in frames:
            if app_hex is None and self._skip_time_frame:
                _LOGGER.debug("Fit3 %s: skipping the setup replay's 0101 time frame", self._address)
                continue
            app = setup_sequence.build_time_app(utc_offset_s=self._tz()[0]) if app_hex is None else bytes.fromhex(app_hex)
            if not await self._send(bytes.fromhex(route_hex), app, b5 & 0xE0, ack_timeout=ack_timeout):
                missing.append(route_hex)
            else:
                acked += 1
                if app_hex is None:  # the time frame
                    self._last_time_sync = time.monotonic()
                    _LOGGER.debug("Fit3 %s: time frame ACKed (%s)", self._address, self._time_desc())
        _LOGGER.debug(
            "Fit3 setup replay: %d/%d frames ACKed; no ACK for routes %s; watch->host frames by route %s",
            acked, len(frames), missing or "none", dict(self._rx_routes),
        )
        return acked

    async def _run_fresh_pairing(self, ack_timeout: float) -> int:
        """Diagnostics: replay the phone's first connection after a fresh pairing (see :mod:`.sap.fresh_pairing`)."""
        acked = 0
        for cls, route_hex, app_hex in fresh_pairing.FRESH_FRAMES:
            if await self._send(bytes.fromhex(route_hex), bytes.fromhex(app_hex), cls, ack_timeout=ack_timeout):
                acked += 1
        _LOGGER.warning(
            "Fit3 fresh-pairing replay: %d/%d frames ACKed; watch->host frames by route %s",
            acked, len(fresh_pairing.FRESH_FRAMES), dict(self._rx_routes),
        )
        return acked

    def set_fresh_pairing(self, enabled: bool) -> None:
        """Diagnostics: from the next connect, replay the phone's first-pairing connection."""
        self._fresh_pairing = bool(enabled)

    async def disconnect(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for _op, fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        await self._transport.disconnect()

    # -- capabilities ----------------------------------------------------------------------
    def _time_desc(self, utc_offset_s: int | None = None) -> str:
        off = self._tz()[0] if utc_offset_s is None else utc_offset_s
        return f"epoch {int(time.time())}, UTC{off / 3600:+g}h"

    async def set_time(
        self, is_24h: bool = False, tz_offset_seconds: int | None = None, variant: str = "standard", epoch_seconds: int | None = None,
    ) -> bool:
        """Push the current time and UTC offset to the watch (route 0101, the phone's time frame).

        This is the same ``83 01 64 00 02 00 03 0a <epoch ASCII> 04 <sign> <uint16 LE s> 05 00`` frame
        the phone sends in its setup exchange (:func:`setup_sequence.build_time_app`), now sendable at
        any time. The epoch is UTC seconds taken at send time; the offset is the host's current
        one including DST (``tz_offset_seconds`` overrides it). The watch ACKs on route 8000 and
        answers with a ``43`` frame on route 0101 whose contents are logged (the reply is not decoded:
        its meaning is unknown). ``is_24h`` is accepted for the service schema but the frame has no
        such field. Returns whether the watch ACKed the frame.
        """
        if not self._transport.is_connected:
            raise NotConnected("not connected: call connect() first")
        if not await self._mtu_ok():
            _LOGGER.warning("Fit3 %s: skipping set_time at low ATT MTU (a split time frame corrupts the clock)", self._address)
            return False
        offset = self._tz()[0] if tz_offset_seconds is None else int(tz_offset_seconds)
        app = setup_sequence.build_time_app_variant(variant, epoch_seconds, offset, is_24h)
        if variant != "standard" or epoch_seconds is not None:
            _LOGGER.warning("Fit3 %s: time experiment variant=%s epoch=%s frame=%s", self._address, variant, epoch_seconds, app.hex())
        resp = await self._request(appframe.ROUTE_TIME, app, 0x40, timeout=5.0)
        ok = self._ack_ok
        if ok:
            self._last_time_sync = time.monotonic()
        _LOGGER.log(
            logging.DEBUG if ok else logging.WARNING,
            "Fit3 %s: set_time sent (%s); ACKed=%s; watch reply=%s",
            self._address, self._time_desc(offset), ok, resp.app.hex() if resp is not None else None,
        )
        return ok

    def set_skip_time_frame(self, skip: bool) -> None:
        """Diagnostics: from the next connect, leave the time frame out of the setup replay."""
        self._skip_time_frame = bool(skip)

    def set_replay_limit(self, frames: int | None) -> None:
        """Diagnostics: from the next connect, replay only the first ``frames`` setup frames (None = all)."""
        self._replay_limit = None if frames is None else max(0, int(frames))

    async def set_time_rtc(
        self,
        is_24h: bool = False,
        tz_offset_seconds: int | None = None,
        byte_order: str = "little",
        offset_unit: str = "seconds",
    ) -> bool:
        """Set the watch clock with the phone's runtime message (service 11 / route 0b0b, msg id 0).

        Layout confirmed byte for byte against a phone capture (the message it sends on every timezone
        change): ``80 04 | 00 00 | 01 <epoch int64 LE> | 04 <24h> | 02 <sign> <offset uint16 LE seconds>``.
        The endianness and offset unit stay selectable for experiments. Returns whether the watch ACKed.
        """
        if not self._transport.is_connected:
            raise NotConnected("not connected: call connect() first")
        if not await self._mtu_ok():
            _LOGGER.warning("Fit3 %s: skipping set_time_rtc at low ATT MTU (a split frame corrupts the clock)", self._address)
            return False
        offset = self._tz()[0] if tz_offset_seconds is None else int(tz_offset_seconds)
        app = app_message.build_set_time(
            is_24h=is_24h, tz_offset_seconds=offset, byte_order=byte_order, offset_unit=offset_unit
        )
        ok = await self._send(appframe.ROUTE_SETTINGS, app, 0x40, ack_timeout=3.0)
        if ok:
            self._last_time_sync = time.monotonic()
        _LOGGER.log(
            logging.DEBUG if ok else logging.WARNING,
            "Fit3 %s: set_time_rtc sent (%s, %s-endian, offset in %s) frame=%s; ACKed=%s",
            self._address, self._time_desc(offset), byte_order, offset_unit, app.hex(), ok,
        )
        await asyncio.sleep(1.5)  # let the watch's reply frames arrive (they are logged at DEBUG)
        return ok

    async def sync_time_if_due(self, max_age_s: float = 3600.0) -> bool:
        """Re-send the time when none was ACKed in the last ``max_age_s`` seconds. True if sent."""
        if not self._transport.is_connected:
            return False
        if self._last_time_sync is not None and time.monotonic() - self._last_time_sync < max_age_s:
            return False
        await self.set_time_rtc(is_24h=self._is_24h())
        return True

    async def read_battery(self) -> int | None:
        """Read battery level over SAP (route 0b0b; reply ``42 06 00 05 <pct>``)."""
        resp = await self._request(appframe.ROUTE_BATTERY, b"\x02", 0x40)
        return appframe.parse_battery_response(resp.app) if resp is not None else None

    def heart_rate_records(self) -> dict[int, tuple[int, int | None, int | None, int | None]]:
        """Every heart-rate record received so far (and any restored), keyed by start time."""
        return dict(self._health.hr_records)

    def restore_heart_rate_records(self, records: dict[int, tuple[int, int | None, int | None, int | None]]) -> None:
        """Seed the history from storage (not marked changed, so it is not re-imported)."""
        self._health.hr_records.update(records)
        if records and self._health.heart_rate_time is None:
            newest = max(records)
            self._health.heart_rate, self._health.heart_rate_time = records[newest][0], newest

    def take_dirty_heart_rate_hours(self) -> set[int]:
        """The hours (Unix seconds) with new or changed records since the last call; clears the set."""
        hours, self._health.dirty_hours = self._health.dirty_hours, set()
        return hours

    @property
    def heart_rate_time(self) -> int | None:
        """Unix seconds of the newest heart-rate record the watch has sent."""
        return self._health.heart_rate_time

    async def read_heart_rate(self) -> int | None:
        """Newest heart rate the watch has pushed in a health sync (route 0a0a), if any."""
        return self._health.heart_rate

    def _local_midnight(self) -> float:
        now = time.time()
        return now - (now + self._tz()[0]) % 86400

    async def read_steps(self) -> int | None:
        """Steps since local midnight, summed from the step-count records the watch has pushed."""
        return self._health.steps_since(self._local_midnight())

    async def read_calories(self) -> float | None:
        """Kilocalories since local midnight, from the same records."""
        return self._health.calories_since(self._local_midnight())

    async def read_distance(self) -> float | None:
        """Metres walked since local midnight, from the same records."""
        return self._health.distance_since(self._local_midnight())

    async def read_resting_heart_rate(self) -> int | None:
        """The newest resting heart rate the watch has sent (it has not sent one yet in a week of data)."""
        return self._health.resting_heart_rate

    def _wanted_distance_unit(self) -> str | None:
        unit = self._distance_unit_provider() if self._distance_unit_provider else None
        return unit if unit in healthsync.DISTANCE_UNITS else None

    def _profile_block(self) -> bytes:
        """The user-profile block for this sync: the distance unit, once per connection and whenever it
        changes (empty when no unit is configured or the watch already has it)."""
        unit = self._wanted_distance_unit()
        if unit is None or unit == self._profile_unit_sent:
            return b""
        self._profile_unit_sent = unit
        _LOGGER.debug("Fit3 %s: sending distance unit %s in the user profile", self._address, unit)
        return healthsync.build_user_profile(unit, healthsync.to_protocol_time(time.time()))

    async def set_distance_unit(self, unit: str) -> bool:
        """Send the watch its distance unit (``km`` or ``mi``) now. Returns whether the watch ACKed the frame
        at the transport level; whether it then shows the unit is checked on the watch."""
        if unit not in healthsync.DISTANCE_UNITS:
            raise ValueError(f"distance unit must be km or mi, not {unit!r}")
        if not self._transport.is_connected:
            raise NotConnected("not connected: call connect() first")
        self._health_seq = (self._health_seq % 0x7FFF) + 1
        profile = healthsync.build_user_profile(unit, healthsync.to_protocol_time(time.time()))
        app = healthsync.build_mobile_sync_data(self._health_seq, self._health.last_sync, profile)
        _LOGGER.debug("Fit3 %s: TX HEALTH distance unit %s app=%s", self._address, unit, app.hex())
        acked = await self._send(healthsync.ROUTE_HEALTH, app, reactive.REPLY_CLASS)
        if acked:
            self._profile_unit_sent = unit
        return acked

    async def request_health_sync(self, kind: str = "request_data", window_days: int = 1) -> bool:
        """Ask the watch to sync its health records (experimental; the trigger is unverified).

        ``kind`` is ``request_data`` (a REQUEST_DATA for the last ``window_days`` days),
        ``check_status``, or ``measure_heart_rate`` (ask the watch to take a reading now). Returns whether the watch ACKed the frame at the transport level; any
        records arrive later as SYNC_DATA pushes.
        """
        if not self._transport.is_connected:
            raise NotConnected("not connected: call connect() first")
        self._health_seq = (self._health_seq % 0x7FFF) + 1
        if kind == "check_status":
            app = healthsync.build_check_status(self._health_seq)
        elif kind == "measure_heart_rate":
            app = healthsync.build_on_demand_measure(self._health_seq)
        else:
            app = healthsync.build_request_data(self._health_seq, self._health.last_sync)
        _LOGGER.debug("Fit3 %s: TX HEALTH %s app=%s", self._address, kind, app.hex())
        return await self._send(healthsync.ROUTE_HEALTH, app, reactive.REPLY_CLASS)

    async def sync_health(self, timeout: float = 20.0, ready_timeout: float = 5.0, quiet: float = 1.5) -> bool:
        """Run one health sync and wait for the watch's records, so the link can be dropped afterwards.

        Asks the watch for anything new (REQUEST_DATA) and waits until it has answered and then gone
        quiet: the watch first sends an empty SYNC_DATA and only then the records, as large-data chunks
        about 250 ms apart, so the first SYNC_DATA is not the end. Our acknowledgements are flushed
        before returning. Returns whether the watch answered within ``timeout``; a watch that never
        opens the health service or never answers just returns False.
        """
        deadline = time.monotonic() + ready_timeout
        while not self._health_ready and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not self._health_ready:
            _LOGGER.debug("Fit3 %s: watch has not opened the health service; no sync", self._address)
            return False
        self._health_synced.clear()
        self._health_requested = time.monotonic()
        end = self._health_requested + timeout
        try:
            await self.request_health_sync("request_data")
            await asyncio.wait_for(self._health_synced.wait(), timeout=max(0.0, end - time.monotonic()))
        except (NotConnected, TimeoutError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Fit3 %s: health sync did not complete: %s", self._address, err)
            return False
        while time.monotonic() - self._health_last_rx < quiet and time.monotonic() < end:
            await asyncio.sleep(0.1)  # records are still arriving
        pending = list(self._tasks)  # our acknowledgements are queued as tasks
        if pending:
            await asyncio.wait(pending, timeout=3.0)
        return True

    async def list_watch_faces(self) -> list[appframe.WatchFace]:
        """Request the watch-face catalog (route 0606) and return the installed faces."""
        resp = await self._request(appframe.ROUTE_WATCHFACE, b"\x01", 0x20)
        if resp is not None:
            faces = appframe.parse_watchface_catalog(resp.app)
            if faces:
                self._faces = faces
        return list(self._faces)

    async def set_watch_face(
        self, face_id: int, group: int | None = None, *, timeout: float = 15.0
    ) -> list[appframe.WatchFace]:
        """Show a face on the watch (route 0606 ``03``) the way the phone's app does, and return the
        catalogue read afterwards. ``group`` comes from the catalogue when omitted.

        Raises TimeoutError if the watch does not confirm the switch."""
        faces = await self.list_watch_faces()
        if group is None:
            match = next((f for f in faces if f.face_id == face_id), None)
            group = match.group if match else 0
        resp = await self._request(
            appframe.ROUTE_WATCHFACE, appframe.watchface_show_app(face_id, group), 0x20, timeout=timeout
        )
        if resp is None:
            raise TimeoutError(f"the watch did not confirm showing face {face_id}")
        # Wearable then sends its face list with the shown face moved to the front.
        order = [(face_id, group)] + [(f.face_id, f.group) for f in faces if f.face_id != face_id]
        await self._request(appframe.ROUTE_WATCHFACE, appframe.watchface_order_app(order), 0x20)
        return await self.list_watch_faces()

    async def install_watch_face(
        self,
        face_id: int,
        data: bytes,
        local_address: str,
        *,
        style: int = 0,
        handshake_only: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[appframe.WatchFace]:
        """Install a face file on the watch (see :mod:`.sap.faceinstall`) and return the catalogue afterwards.

        ``local_address`` is the Bluetooth address of the adapter holding the bond: the file goes over a
        Bluetooth Classic RFCOMM connection from it. ``handshake_only`` announces the file and opens and
        closes the file channel without sending it (a test that writes nothing to the watch).
        ``progress(block, total)`` is called from the transfer thread after each acknowledged block.
        Raises TimeoutError or :class:`faceinstall.TransferError` on failure."""
        filename = faceinstall.face_filename(face_id)
        if not handshake_only:
            install = bytes([0x04]) + appframe.watchface_item(face_id, style)
            if await self._request(appframe.ROUTE_WATCHFACE, install, 0x20) is None:
                raise TimeoutError(f"the watch did not answer the install request for face {face_id}")
        route = faceinstall.ROUTE_INSTALL
        if await self._request(route, faceinstall.announce_app(filename, len(data)), 0x40) is None:
            raise TimeoutError("the watch did not answer the file announce")
        try:
            if await self._request(route, bytes([faceinstall.OP_OPEN]), 0x40) is None:
                raise TimeoutError("the watch did not open the file transfer")
            started = time.monotonic()
            await asyncio.get_running_loop().run_in_executor(
                None, self._rfcomm_transfer, local_address, filename, data, handshake_only, progress
            )
            _LOGGER.info(
                "Fit3 %s: %s %s (%d bytes) in %.0f s", self._address,
                "file channel opened and closed for" if handshake_only else "sent", filename, len(data),
                time.monotonic() - started,
            )
        finally:
            if await self._request(route, bytes([faceinstall.OP_CLOSE]), 0x40, timeout=5.0) is None:
                _LOGGER.debug("Fit3 %s: no reply to closing the install session", self._address)
        faces = await self.list_watch_faces()
        if not handshake_only and any(f.face_id == face_id for f in faces):
            order = [(face_id, style)] + [(f.face_id, f.group) for f in faces if f.face_id != face_id]
            await self._request(appframe.ROUTE_WATCHFACE, appframe.watchface_order_app(order), 0x20)
        return faces

    def _rfcomm_transfer(
        self, local_address: str, filename: str, data: bytes, handshake_only: bool,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """The Classic part of an install; runs in an executor thread."""

        def _block_done(n: int, total: int) -> None:
            _LOGGER.debug("Fit3 %s: face block %d/%d acknowledged", self._address, n, total)
            if progress:
                progress(n, total)

        session = faceinstall.RfcommSession.connect(local_address, self._address)
        try:
            session.command(b"30", b"300")
            if not handshake_only:
                session.send_file(filename, data, _block_done)
            session.command(b"34", b"340")
        finally:
            session.close()

    async def read_status(self) -> WatchStatus:
        """Aggregate the readings the coordinator publishes (battery, heart rate, the day's activity)."""
        status = WatchStatus()
        # Ask the watch for anything new since the last sync; the records arrive after this poll
        # returns and show up on the next one. A phone health app opens its syncs the same way.
        if self._health_ready and time.monotonic() - self._health_requested >= HEALTH_SYNC_INTERVAL:
            self._health_requested = time.monotonic()
            try:
                await self.request_health_sync("request_data")
            except (NotConnected, TimeoutError, asyncio.TimeoutError) as err:
                _LOGGER.debug("health sync request not sent: %s", err)
        for name, reader in (
            ("battery_level", self.read_battery),
            ("heart_rate", self.read_heart_rate),
            ("steps", self.read_steps),
            ("calories", self.read_calories),
            ("distance", self.read_distance),
            ("resting_heart_rate", self.read_resting_heart_rate),
        ):
            try:
                setattr(status, name, await reader())
            except (NotConnected, TimeoutError, asyncio.TimeoutError) as err:
                _LOGGER.debug("%s unavailable: %s", name, err)
        return status

    # -- transport -------------------------------------------------------------------------
    async def _send(
        self, route: bytes, app: bytes, cls: int, *, ack_timeout: float = 2.0
    ) -> bool:
        """Send one app frame and wait for the watch's ACK. Returns whether it was ACKed.

        One frame is in flight at a time, so the next ACK (route 8000) from the watch is the one for it
        (see ``_ack_waiter``); the b5 window bit just toggles per class.
        """
        loop = asyncio.get_running_loop()
        async with self._tx_lock:
            toggle = self._win.get(cls, 0)
            self._win[cls] = toggle ^ 1
            b5 = cls | (toggle << 4)
            fut: asyncio.Future[None] = loop.create_future()
            self._ack_waiter = fut
            try:
                _LOGGER.debug("TX route=%s b5=%#04x op=%02x len=%d", route.hex(), b5, app[0] if app else 0, len(app))
                await self._transport.write_frame(appframe.build_frame(0x01, b5, route, app))
                await asyncio.wait_for(fut, ack_timeout)
                return True
            except (TimeoutError, asyncio.TimeoutError):
                _LOGGER.debug("no ACK for route=%s b5=%#04x", route.hex(), b5)
                return False
            finally:
                if self._ack_waiter is fut:
                    self._ack_waiter = None

    async def _request(
        self, route: bytes, app: bytes, cls: int, *, timeout: float = 15.0
    ) -> appframe.AppFrame | None:
        """Send a request and await the watch's reply on the same route.

        The reply is matched by opcode (:func:`appframe.is_reply`), so unsolicited frames the watch
        pushes on the same route (e.g. charge-state messages on the battery route) are not mistaken
        for it. Returns ``None`` on timeout.
        """
        if not self._transport.is_connected:
            raise NotConnected("not connected: call connect() first")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[appframe.AppFrame] = loop.create_future()
        self._pending[route] = (app[0], fut)
        try:
            self._ack_ok = await self._send(route, app, cls)
            if not self._ack_ok:
                _LOGGER.debug("request on route %s was not ACKed", route.hex())
            return await asyncio.wait_for(fut, timeout)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.debug("no reply on route %s within %.0fs", route.hex(), timeout)
            return None
        finally:
            if self._pending.get(route, (None, None))[1] is fut:
                self._pending.pop(route, None)

    def _health_send(self, app: bytes) -> None:
        """Queue one message on the health route (frames are serialized by ``_send``)."""
        task = asyncio.ensure_future(self._send(healthsync.ROUTE_HEALTH, app, reactive.REPLY_CLASS))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_health(self, app: bytes) -> None:
        """Log a health-route message and keep any heart-rate / step records it carries."""
        op = app[0]
        self._health_last_rx = time.monotonic()
        if op == healthsync.OP_LARGE_DATA_CHUNK:
            _LOGGER.debug("RX HEALTH chunk flags=%02x len=%d", app[1] if len(app) > 1 else 0, len(app))
            done = self._large.feed(app)
            if done is None:
                return
            whole, ack = done
            self._health_send(ack)
            self._on_health(whole)
            reply = reactive.reply_for(
                healthsync.ROUTE_HEALTH, whole, self._tz(), reactive.CLAIM_HEALTH_APP_INSTALLED,
                self._health.last_sync,
            )
            if reply is not None:
                self._health_send(reply)
            return
        # The capability request repeats and is long; everything else is logged whole.
        shown = app.hex() if op != healthsync.OP_CAPABILITY_REQUEST and len(app) <= 400 else app[:48].hex() + "..."
        _LOGGER.debug("RX HEALTH 0a0a op=%02x len=%d app=%s", op, len(app), shown)
        if op == healthsync.OP_CAPABILITY_REQUEST:
            self._health_ready = True
        if op == healthsync.OP_REQUEST_DATA_RESPONSE:
            # stage two of a phone-started sync: our (empty) SYNC_DATA, after which the watch sends its own
            self._health_seq = (self._health_seq % 0x7FFF) + 1
            self._health_send(
                healthsync.build_mobile_sync_data(self._health_seq, self._health.last_sync, self._profile_block())
            )
        if (op & 0x3F) not in (2, 3):
            return
        try:
            data = healthsync.parse_sync_message(app)
        except ValueError as err:
            _LOGGER.warning("Fit3 %s: health message did not parse: %s; app=%s", self._address, err, app.hex())
            return
        self._health.apply(data)
        if op == healthsync.OP_SYNC_DATA:
            self._health_synced.set()
        for label, recs in (("heart-rate", data.heart_rates), ("step", data.steps)):
            starts = [r.start for r in recs if r.start is not None]
            if starts:
                _LOGGER.debug(
                    "Fit3 %s: %d %s records in this message, from %s to %s UTC (%d heart-rate records stored in all)",
                    self._address, len(starts), label,
                    time.strftime("%Y-%m-%d %H:%M", time.gmtime(min(starts))),
                    time.strftime("%Y-%m-%d %H:%M", time.gmtime(max(starts))),
                    len(self._health.hr_records),
                )
        _LOGGER.debug(
            "Fit3 %s: health sync seq=%s steps=%d records heart_rate=%d records%s",
            self._address, data.seq, len(data.steps), len(data.heart_rates),
            "" if data.stopped_at_tag is None
            else f" (stopped at unknown data type {data.stopped_at_tag:#04x}, offset {data.stopped_at_offset}; rest={app[data.stopped_at_offset:data.stopped_at_offset + 64].hex()})",
        )

    def _on_frame(self, data: bytes) -> None:
        """Notification sink: reassemble frames from the byte stream and handle each one."""
        for frame_bytes in self._asm.feed(data):
            self._handle_frame(frame_bytes)

    def _handle_frame(self, data: bytes) -> None:
        if appframe.is_hello_announce(data):
            if self._hello_announce is not None and not self._hello_announce.done():
                self._hello_announce.set_result(data)
            return
        if self._caps is None and capability.CAP_MAP_MARKER in data:
            try:
                self._caps = capability.parse_service_channel_map(data)
                _LOGGER.debug("Fit3: learned SAP capability map (%d bound services)", len(self._caps.bound))
            except ValueError:
                pass
        now = time.monotonic()
        if data == self._last_raw[0] and now - self._last_raw[1] < 0.05 and data[6:8] != b"\x80\x00":
            return  # the same notification delivered twice
        self._last_raw = (data, now)
        try:
            part = appframe.decode_app_frame(data)
        except ValueError:
            return  # not a valid app frame -- ignore
        fr = self._reasm.feed(part)
        if fr is None:
            return  # a fragment of a larger message; wait for the rest
        self._rx_routes[fr.route.hex()] += 1
        _LOGGER.debug("RX route=%s b5=%#04x op=%s len=%d", fr.route.hex(), fr.b5,
                      f"{fr.app[0]:02x}" if fr.app else "--", len(fr.app))
        if fr.route in (appframe.ROUTE_TIME, b"\x08\x08", appframe.ROUTE_WATCHFACE):  # log these exchanges whole
            _LOGGER.debug("RX route=%s app=%s", fr.route.hex(), fr.app.hex())
        if fr.route == healthsync.ROUTE_HEALTH and fr.app:
            self._on_health(fr.app)
        if fr.route == b"\x80\x00":  # transport ACK of one of our frames
            if self._ack_waiter is not None and not self._ack_waiter.done():
                self._ack_waiter.set_result(None)
            return
        # a reply to our outstanding request on this route?
        waiting = self._pending.get(fr.route)
        if waiting is not None and fr.app and appframe.is_reply(waiting[0], fr.app[0]):
            if not waiting[1].done():
                waiting[1].set_result(fr)
            return
        # otherwise the watch may be asking us something
        answer = reactive.reply_for(
            fr.route, fr.app, self._tz(), reactive.CLAIM_HEALTH_APP_INSTALLED, self._health.last_sync
        )
        if answer is not None:
            key = (fr.route.hex(), fr.app[0])
            limit = reactive.MAX_REPLIES.get((key[0], key[1]))
            if limit is not None and self._replies_sent[key] >= limit:
                _LOGGER.debug("not answering repeat of %s/%02x (already sent %d)", key[0], key[1], limit)
                return
            self._replies_sent[key] += 1
            task = asyncio.ensure_future(self._send(fr.route, answer, reactive.reply_class(fr.route)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
