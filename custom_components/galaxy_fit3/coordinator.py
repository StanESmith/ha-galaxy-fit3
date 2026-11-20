"""DataUpdateCoordinator for the Galaxy Fit3 integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import ble_support, face_store, heart_rate_history, routing, store_catalog
from .sap import appframe, faceinstall
from .bluez_agent import PairingAgent
from .client import WatchStatus
from .const import (
    CONF_ADDRESS,
    CONF_IS_24H,
    DEFAULT_IS_24H,
    DEFAULT_SCAN_INTERVAL_S,
    DOMAIN,
    HOLD_CONNECTION,
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
)

_LOGGER = logging.getLogger(__name__)

INSTALL_IDLE = "idle"
INSTALL_DOWNLOADING = "downloading"
INSTALL_SENDING = "sending"
INSTALL_DONE = "installed"
INSTALL_FAILED = "failed"

_RETRY_INTERVAL_S = 30  # poll this often while disconnected, DEFAULT_SCAN_INTERVAL_S once connected
TIME_SYNC_INTERVAL_S = 3600  # re-send the time at most this often (checked every poll)
_MAX_FAILS = 3  # consecutive connect failures before a stale bond is cleared
_SEEN_WITHIN_S = 45  # only try to connect when the watch advertised this recently
_MAX_RETRY_INTERVAL_S = 300  # ceiling for the back-off after failed connection attempts
_WARN_AFTER_FAILS = 6  # single failed connects are routine at the edge of range; warn when they keep failing
HEART_RATE_STATISTIC_ID = f"{DOMAIN}:heart_rate"  # external statistic: hourly mean/min/max of the watch's records


class Fit3Coordinator(DataUpdateCoordinator[WatchStatus]):
    """Connect over BLE each poll and read battery (+ HR/steps once those are reachable).

    The plaintext SAP path (LE-SC bond + hello + app frames) is live; each poll connects, reads
    status, and disconnects. Connection failures are logged and surfaced as an empty status so the
    integration stays loaded and keeps retrying instead of erroring in a loop.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_S),
        )
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.status: str = STATUS_DISCONNECTED
        self._fail_streak = 0  # consecutive real connect failures (see _record_failure)
        self._connect_fails = 0  # consecutive failed connection attempts (drives the retry back-off)
        self._absent_logged = False
        self._releasing = False  # True while we drop the link ourselves at the end of a poll
        self._purge_old_statistics = False  # stored records with a bogus date were dropped; clear their statistics
        # every heart-rate record the watch has sent, kept across restarts for graphing and later merging
        self._hr_store: Store = Store(hass, heart_rate_history.STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.heart_rate")
        self.watch_faces: list[appframe.WatchFace] = []  # catalog, fetched once per session
        self.current_face_id: int | None = None  # last face we selected (None until set)
        self.store_faces: dict[int, store_catalog.StoreFace] = {}  # Samsung store catalogue: names and previews
        self.local_previews: set[int] = set()  # faces with a preview PNG under www/galaxy_fit3/faces
        self.store_selection: int | None = None  # face picked in the store select, installed by the button
        self.install_state: str = INSTALL_IDLE
        self.install_info: dict[str, object] = {}
        self._install_task: asyncio.Task | None = None
        self._preview_task: asyncio.Task | None = None
        self._route = routing.RouteHolder()  # the adapters that hold a bond; refreshed before each connect
        self._route_logged: str | None = None
        self._link_lock = asyncio.Lock()  # one user of the link at a time: a poll, a face change or a service call
        self.last_seen: datetime | None = None  # end of the last poll that reached the watch
        self._client = ble_support.build_client(
            hass, self.address, self._on_disconnect,
            is_24h_provider=lambda: self.entry.options.get(CONF_IS_24H, DEFAULT_IS_24H),
            route=self._route,
        )

    async def async_load_history(self) -> None:
        """Restore the stored heart-rate records (they are not re-imported as statistics)."""
        records = heart_rate_history.from_store(await self._hr_store.async_load())
        dropped = heart_rate_history.drop_implausible(records)
        if dropped:
            _LOGGER.warning("Fit3 %s: dropped %d stored heart-rate records with an impossible date", self.address, dropped)
            self._purge_old_statistics = True
        self._client.restore_heart_rate_records(records)
        if records:
            _LOGGER.info("Fit3 %s: restored %d stored heart-rate records", self.address, len(records))

    @property
    def heart_rate_info(self) -> dict[str, object]:
        """Attributes for the heart-rate sensor: how fresh the newest record is and how many are stored."""
        records = self._client.heart_rate_records()
        newest = self._client.heart_rate_time
        return {
            "last_record": datetime.fromtimestamp(newest, UTC).isoformat() if newest else None,
            "stored_records": len(records),
            "oldest_record": datetime.fromtimestamp(min(records), UTC).isoformat() if records else None,
        }

    async def _save_heart_rate_history(self) -> None:
        """Persist new records and import the hours they touch as hourly statistics (mean, min, max).

        Statistics are what Home Assistant can graph for readings that arrive hours after they were
        taken; the raw records stay in storage at full resolution.
        """
        if self._purge_old_statistics:  # the bogus-dated hours were imported before the date check existed
            get_instance(self.hass).async_clear_statistics(
                [HEART_RATE_STATISTIC_ID],
                start_time=datetime(2000, 1, 1, tzinfo=UTC),
                end_time=datetime.fromtimestamp(heart_rate_history.MIN_RECORD_TIME, UTC),
            )
            await self._hr_store.async_save(heart_rate_history.to_store(self._client.heart_rate_records()))
            self._purge_old_statistics = False
        hours = self._client.take_dirty_heart_rate_hours()
        if not hours:
            return
        records = self._client.heart_rate_records()
        await self._hr_store.async_save(heart_rate_history.to_store(records))
        rows = heart_rate_history.hourly_stats(records, hours)
        if not rows:
            return
        metadata = StatisticMetaData(
            has_sum=False,
            mean_type=StatisticMeanType.ARITHMETIC,
            name="Galaxy Fit3 heart rate",
            source=DOMAIN,
            statistic_id=HEART_RATE_STATISTIC_ID,
            unit_class=None,
            unit_of_measurement="bpm",
        )
        stats = [
            StatisticData(start=datetime.fromtimestamp(hour, UTC), mean=mean, min=low, max=high)
            for hour, mean, low, high in rows
        ]
        async_add_external_statistics(self.hass, metadata, stats)
        _LOGGER.info("Fit3 %s: imported %d hourly heart-rate statistics (%d records stored)", self.address, len(stats), len(records))

    async def async_apply_options(self) -> None:
        """Re-send the time with the current options now (called when the options flow saves)."""
        if self._client.is_connected:
            try:
                await self._client.set_time_rtc(is_24h=self.entry.options.get(CONF_IS_24H, DEFAULT_IS_24H))
            except Exception as err:  # noqa: BLE001 - a settings change must not raise into the UI
                _LOGGER.warning("Fit3 %s: applying options failed: %s", self.address, err)

    def face_label(self, face_id: int) -> str:
        """The face's store name (e.g. "Progress gauge 1"), or ``Face <id>`` until the catalogue is loaded."""
        face = self.store_faces.get(face_id)
        return face.name if face and face.name else f"Face {face_id}"

    def face_preview(self, face_id: int | None) -> str | None:
        """The face's own preview under ``/local`` when there is one, else the store's icon."""
        if face_id is None:
            return None
        if face_id in self.local_previews:
            return face_store.preview_url(face_id)
        face = self.store_faces.get(face_id)
        return face.preview if face and face.preview else None

    async def async_load_store_catalogue(self) -> None:
        """Load the Samsung store catalogue (or its cached copy); entities pick up names and pictures."""
        self.local_previews = await face_store.async_local_previews(self.hass)
        faces = await face_store.async_list_faces(self.hass)
        if faces:
            self.store_faces = faces
        self.async_update_listeners()

    async def async_select_face(self, label: str) -> None:
        """Switch the active watch face from a dropdown label (see :meth:`face_label`)."""
        face = next((f for f in self.watch_faces if self.face_label(f.face_id) == label), None)
        if face is None:
            raise ValueError(f"unknown watch face: {label}")
        await self.async_show_face(face.face_id, face.group)

    def _set_install(self, state: str, **info: object) -> None:
        self.install_state = state
        self.install_info = {k: v for k, v in info.items() if v is not None}
        self.async_update_listeners()

    def async_start_install(self, face_id: int | None = None, style: int = 0) -> None:
        """Install a face in the background: the store select's pick (the button) or ``face_id`` (the service)."""
        face_id = face_id if face_id is not None else self.store_selection
        if face_id is None:
            raise HomeAssistantError("Pick a face to install first.")
        if self._install_task is not None and not self._install_task.done():
            raise HomeAssistantError("A watch face is already being installed.")
        self._install_task = self.hass.async_create_background_task(
            self._install_in_background(face_id, style), f"{DOMAIN} install face {face_id}"
        )

    def async_start_preview_fetch(self, face_ids: list[int] | None = None) -> int:
        """Save missing preview pictures in the background, one package at a time; returns how many are queued.

        Each preview lives inside the face's store package (about 4 MB), so fetching all of them downloads
        a few hundred MB. Faces that already have a preview are skipped.
        """
        wanted = list(self.store_faces) if face_ids is None else face_ids
        missing = [face_id for face_id in wanted if face_id not in self.local_previews]
        if not missing:
            return 0
        if self._preview_task is not None and not self._preview_task.done():
            raise HomeAssistantError("Previews are already being fetched.")
        self._preview_task = self.entry.async_create_background_task(
            self.hass, self._fetch_previews(missing), f"{DOMAIN} fetch face previews"
        )
        return len(missing)

    async def _fetch_previews(self, face_ids: list[int]) -> None:
        saved = 0
        for face_id in face_ids:
            try:
                saved += await face_store.async_fetch_preview(self.hass, face_id)
            except Exception as err:  # noqa: BLE001 - one failed download must not stop the rest
                _LOGGER.debug("Fit3 %s: no preview for face %d: %s", self.address, face_id, err)
            self.local_previews = await face_store.async_local_previews(self.hass)
            self.async_update_listeners()
        _LOGGER.info("Fit3 %s: saved %d of %d face previews", self.address, saved, len(face_ids))

    async def _install_in_background(self, face_id: int, style: int = 0) -> None:
        try:
            await self.async_install_face(face_id, style)
        except HomeAssistantError as err:
            _LOGGER.warning("Fit3 %s: %s", self.address, err)


    async def async_show_face(self, face_id: int, group: int | None = None) -> None:
        """Show a face on the watch (it is put back if it had been removed) and update the dropdown."""
        async with self.watch_session() as client:
            try:
                faces = await client.set_watch_face(face_id, group)
            except (TimeoutError, asyncio.TimeoutError) as err:
                raise HomeAssistantError(f"The watch did not switch to face {face_id}; try again.") from err
        if faces:
            self.watch_faces = faces
        self.current_face_id = next((f.face_id for f in faces if f.active), face_id)
        self.async_update_listeners()

    async def async_install_face(self, face_id: int, style: int = 0, handshake_only: bool = False) -> dict:
        """Download a face from Samsung's store (cached) and install it over Bluetooth Classic.

        ``handshake_only`` opens and closes the watch's file channel without sending the file."""
        name = self.face_label(face_id)
        try:
            self._set_install(INSTALL_DOWNLOADING, face=name, face_id=face_id)
            try:
                data = await face_store.async_get_face_file(self.hass, face_id)
                self.local_previews = await face_store.async_local_previews(self.hass)
            except Exception as err:  # noqa: BLE001 - store, network or package problems all end here
                raise HomeAssistantError(f"Could not get {name} from the Samsung store: {err}") from err
            adapter = await ble_support.classic_adapter(self.address)
            if adapter is None:
                raise HomeAssistantError("No local Bluetooth adapter has a Classic bond with the watch; pair it again.")
            self._set_install(INSTALL_SENDING, face=name, face_id=face_id, progress=0)

            def _progress(block: int, total: int) -> None:  # runs in the transfer thread
                pct = round(100 * block / total)
                self.hass.loop.call_soon_threadsafe(
                    lambda: self._set_install(INSTALL_SENDING, face=name, face_id=face_id, progress=pct)
                )

            async with self.watch_session() as client:
                try:
                    faces = await client.install_watch_face(
                        face_id, data, adapter[1], style=style, handshake_only=handshake_only, progress=_progress
                    )
                except faceinstall.SecurityRefused as err:
                    raise HomeAssistantError(
                        f"Installing {name} failed: {err}. The watch's bond is on {adapter[0]}; a Bluetooth 4.1 "
                        "or newer adapter is needed for installs."
                    ) from err
                except (TimeoutError, asyncio.TimeoutError, faceinstall.TransferError, OSError) as err:
                    raise HomeAssistantError(f"Installing {name} failed: {err}") from err
        except HomeAssistantError as err:
            self._set_install(INSTALL_FAILED, face=name, face_id=face_id, error=str(err))
            raise
        if faces:
            self.watch_faces = faces
            self.current_face_id = next((f.face_id for f in faces if f.active), self.current_face_id)
        self.async_update_listeners()
        on_watch = any(f.face_id == face_id for f in faces)
        if not handshake_only and not on_watch:
            self._set_install(INSTALL_FAILED, face=name, face_id=face_id, error="not in the watch's list after sending")
            raise HomeAssistantError(f"The file was sent but {name} is not in the watch's list.")
        self._set_install(INSTALL_IDLE if handshake_only else INSTALL_DONE, face=name, face_id=face_id)
        return {
            "face_id": face_id,
            "handshake_only": handshake_only,
            "bytes": len(data),
            "adapter": adapter[0],
            "on_watch": on_watch,
            "shown": self.current_face_id,
        }

    @contextlib.asynccontextmanager
    async def watch_session(self):
        """Use the watch outside a poll (a face change or a service call); yields the client.

        Waits for a running poll to finish, brings the link up if it is down (between polls it
        usually is) and drops it again afterwards. Raises HomeAssistantError when the watch can't be
        reached.
        """
        async with self._link_lock:
            opened = not self._client.is_connected
            if not await self._open_link():
                raise HomeAssistantError("The Galaxy Fit3 is not reachable right now; try again when it is nearby.")
            try:
                yield self._client
            finally:
                if opened and not HOLD_CONNECTION:
                    await self._release_link()

    @property
    def client(self):
        """The BLE client; the service handlers in ``__init__`` call it as ``coordinator.client``."""
        return self._client

    def _on_disconnect(self, _client) -> None:
        if self._releasing:
            _LOGGER.debug("Fit3 %s: link released after the poll", self.address)
        else:
            _LOGGER.info("Fit3 %s: watch disconnected", self.address)

    async def forget_bond(self) -> None:
        """Drop the BlueZ bond (the ``forget_bond`` service); the next connect pairs fresh.

        Removing the BlueZ device records also clears any ``Blocked`` flag on them.
        """
        async with self._link_lock:
            await self._client.disconnect()
            await asyncio.wait_for(ble_support.forget_bond(self.address), timeout=8)
            self._route_logged = None  # re-check the blocks once a new bond exists

    async def reconnect(
        self, replay_frames: int | None = None, skip_time_frame: bool = False, fresh_pairing: bool = False
    ) -> None:
        """Diagnostics: drop the link and reconnect now, replaying only the first ``replay_frames`` setup frames.

        ``None`` replays everything (the normal behaviour). The limit stays in effect for later reconnects
        until it is changed again or Home Assistant restarts.
        """
        self._client.set_replay_limit(replay_frames)
        self._client.set_skip_time_frame(skip_time_frame)
        self._client.set_fresh_pairing(fresh_pairing)
        await self._client.disconnect()
        await self.async_request_refresh()

    def _record_failure(self, err: Exception) -> None:
        """Count a connect failure; clear a (probably stale) bond after repeated real failures.

        "Watch not seen / no free slot" means it is simply out of range or asleep and says nothing
        about the bond, so it must never cause a good bond to be deleted. Anything else repeated
        :data:`_MAX_FAILS` times in a row is treated as a stale bond (the watch forgot its keys).
        """
        self.status = STATUS_DISCONNECTED
        self.update_interval = timedelta(seconds=_RETRY_INTERVAL_S)  # retry sooner than normal
        text = str(err)
        if "never seen by any scanner" in text or "available connection slot" in text:
            return
        self._fail_streak += 1

    async def _release_link(self) -> None:
        """Drop the BLE link so the adapter is free until the next poll."""
        self._releasing = True
        try:
            await asyncio.wait_for(self._client.disconnect(), timeout=25)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.warning("Fit3 %s: releasing the link timed out; dropping it through BlueZ", self.address)
            await ble_support.drop_orphaned_links(self.address)
        finally:
            self._releasing = False

    async def _update_blocked_adapters(self) -> None:
        """Keep the watch off adapters it has no bond with (checked whenever the bonds change)."""
        try:
            blocked, unblocked = await asyncio.wait_for(ble_support.update_blocked_adapters(self.address), timeout=10)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.debug("Fit3 %s: updating blocked adapters timed out", self.address)
            return
        if blocked:
            _LOGGER.info("Fit3 %s: blocked the watch on %s (no bond there)", self.address, ", ".join(blocked))
        if unblocked:
            _LOGGER.info("Fit3 %s: unblocked the watch on %s", self.address, ", ".join(unblocked))

    async def _drop_orphaned_links(self) -> bool:
        """Drop a BlueZ link to the watch that no client owns (left by a restart mid-poll). True if one was dropped.

        Skipped while a config flow for this integration runs, since pairing holds its own link.
        """
        if self.hass.config_entries.flow.async_progress_by_handler(DOMAIN):
            return False
        try:
            dropped = await asyncio.wait_for(ble_support.drop_orphaned_links(self.address), timeout=10)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.warning("Fit3 %s: checking for a leftover BlueZ link timed out", self.address)
            return False
        if dropped:
            _LOGGER.warning(
                "Fit3 %s: dropped a leftover BlueZ link on %s that no client owned (the watch does not "
                "advertise while connected)", self.address, ", ".join(dropped),
            )
        return bool(dropped)

    async def _open_link(self) -> bool:
        """Connect to the watch if the link is down. False (failure recorded) when it cannot be reached."""
        if self._client.is_connected:
            return True
        # A bond belongs to one adapter, so only adapters that hold one may carry the connection (the
        # watch rejects the others), even when an unbonded adapter or proxy hears it better.
        try:
            self._route.policy = await asyncio.wait_for(ble_support.route_policy(self.address), timeout=8)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.warning("Fit3 %s: reading the BlueZ bonds timed out; keeping the previous route", self.address)
        described = self._route.policy.describe()
        if described != self._route_logged:
            _LOGGER.info("Fit3 %s: connection route: %s", self.address, described)
            self._route_logged = described
            await self._update_blocked_adapters()
        # Do not touch the adapter unless the watch is actually advertising nearby: a connection
        # attempt to an absent watch ties up the adapter for most of a minute and, repeated every
        # poll, starves every other Bluetooth device of scan time.
        seen = ble_support.seconds_since_seen(self.hass, self.address, self._route)
        if seen is None or seen > _SEEN_WITHIN_S:
            await self._drop_orphaned_links()  # the watch advertises again once it is gone; connect next poll
            if not self._absent_logged:
                _LOGGER.info(
                    "Fit3 %s: watch is not advertising to an adapter that can connect to it (%s; heard by: %s); "
                    "waiting for it (no connection attempts)",
                    self.address, described, ", ".join(ble_support.heard_by(self.hass, self.address)) or "nothing",
                )
                self._absent_logged = True
            self.status = STATUS_DISCONNECTED
            self.update_interval = timedelta(seconds=_RETRY_INTERVAL_S)
            return False
        self._absent_logged = False
        try:
            await asyncio.wait_for(ble_support.prefer_le_bearer(self.address), timeout=8)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.warning("Fit3 %s: setting PreferredBearer timed out; continuing", self.address)
        try:
            # Register a throw-away auto-confirm pairing agent so the LE-SC bond can complete.
            async with PairingAgent():
                await asyncio.wait_for(self._client.connect(), timeout=150)
        except Exception as err:  # noqa: BLE001 - resilient poll: log + retry soon
            self._connect_fails += 1
            _LOGGER.log(
                logging.WARNING if self._connect_fails == _WARN_AFTER_FAILS else logging.INFO,
                "Fit3 %s: connect failed (%d in a row): %s (BlueZ: %s)", self.address, self._connect_fails, err,
                await ble_support.bluez_device_state(self.address),
            )
            self._record_failure(err)
            # back off: 60 s, 120 s, 240 s, then every 5 minutes, until a connection succeeds
            self.update_interval = timedelta(
                seconds=min(_MAX_RETRY_INTERVAL_S, _RETRY_INTERVAL_S * 2 ** min(self._connect_fails, 4))
            )
            await self._client.disconnect()
            return False
        self._connect_fails = 0
        return True

    async def _async_update_data(self) -> WatchStatus:
        async with self._link_lock:
            return await self._poll()

    def _last_known(self) -> WatchStatus:
        """What the sensors keep showing after a failed poll: the last values read, not unknown.

        Polls fail routinely at the edge of Bluetooth range; the ``status`` and ``last_seen``
        attributes say how old the values are.
        """
        return self.data if self.data is not None else WatchStatus()

    async def _poll(self) -> WatchStatus:
        # Each poll is a short session (connect, sync, disconnect) unless HOLD_CONNECTION keeps the link up.
        # NOTE: we deliberately do NOT auto-clear the BlueZ bond on repeated failures. The watch keeps
        # its half of the bond, so clearing HA's half only creates a mismatch that forces a full re-pair,
        # and the coordinator has no pairing agent to recover. "Service Discovery has not been performed
        # yet" / connect timeouts are transient and clear on retry. Use the forget_bond service for a
        # genuinely stale bond.
        if not await self._open_link():
            return self._last_known()
        try:
            if not HOLD_CONNECTION:  # a short session: collect the records now, before the link is dropped
                await self._client.sync_health()
            status = await self._client.read_status()
            try:  # keep the watch clock current: once per connect (setup replay) and then hourly
                await self._client.sync_time_if_due(TIME_SYNC_INTERVAL_S)
            except Exception as err:  # noqa: BLE001 - a failed time push must not fail the poll
                _LOGGER.warning("Fit3 %s: time sync failed: %s", self.address, err)
            await self._read_watch_faces()
            self.status = STATUS_CONNECTED
            self.last_seen = datetime.now(UTC)
            self._fail_streak = 0
            self.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL_S)
            _LOGGER.debug("Fit3 %s: status %s", self.address, status)
            try:  # a storage or recorder problem must not fail the poll
                await self._save_heart_rate_history()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Fit3 %s: saving heart-rate history failed: %s", self.address, err)
            if not HOLD_CONNECTION:
                await self._release_link()
            return status
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Fit3 %s: read failed: %s", self.address, err)
            self._record_failure(err)
            await self._client.disconnect()
            return self._last_known()

    async def _read_watch_faces(self) -> None:
        """Refresh the watch-face catalogue and the active face (for the select entity) every poll,
        so a face changed on the watch itself shows up too."""
        try:
            faces = await self._client.list_watch_faces()
        except Exception as err:  # noqa: BLE001 - optional; the dropdown keeps its last contents
            _LOGGER.debug("Fit3 %s: watch-face catalogue read failed: %s", self.address, err)
            return
        if faces:
            self.watch_faces = faces
            active = next((f.face_id for f in faces if f.active), None)
            if active is not None:
                self.current_face_id = active
