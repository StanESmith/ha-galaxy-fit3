"""The Samsung Galaxy Fit3 integration.

SAP client for the watch. Over BLE the Fit3 bonds with LE Secure Connections (made by
BlueZ) and runs SAP in plaintext. The integration reads battery, heart rate and steps, keeps the watch's
clock in sync, switches watch faces and installs faces from Samsung's store.
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import CONF_IS_24H, DEFAULT_IS_24H, DOMAIN
from .coordinator import Fit3Coordinator
from .dashboard import async_setup_frontend
from .sap.errors import NotConnected

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SELECT, Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SET_TIME = "set_time"
SET_TIME_SCHEMA = vol.Schema(
    {
        vol.Optional("is_24h"): cv.boolean,  # default: the 24-hour clock option
        vol.Optional("tz_offset_seconds"): vol.Coerce(int),  # default: Home Assistant's zone, DST included
    }
)

SERVICE_SET_DISTANCE_UNIT = "set_distance_unit"
SET_DISTANCE_UNIT_SCHEMA = vol.Schema({vol.Required("unit"): vol.In(["km", "mi"])})

SERVICE_RECONNECT = "reconnect"
RECONNECT_SCHEMA = vol.Schema({vol.Optional("replay_frames"): vol.All(vol.Coerce(int), vol.Range(min=0, max=27)), vol.Optional("skip_time_frame", default=False): cv.boolean, vol.Optional("fresh_pairing", default=False): cv.boolean})

SERVICE_REQUEST_HEALTH_SYNC = "request_health_sync"
REQUEST_HEALTH_SYNC_SCHEMA = vol.Schema(
    {
        vol.Optional("kind", default="request_data"): vol.In(["request_data", "check_status", "measure_heart_rate"]),
        vol.Optional("window_days", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
    }
)

SERVICE_SET_WATCH_FACE = "set_watch_face"
SET_WATCH_FACE_SCHEMA = vol.Schema(
    {
        vol.Required("face_id"): vol.Coerce(int),
        vol.Optional("group"): vol.Coerce(int),
    }
)

SERVICE_LIST_WATCH_FACES = "list_watch_faces"
SERVICE_INSTALL_WATCH_FACE = "install_watch_face"
INSTALL_WATCH_FACE_SCHEMA = vol.Schema(
    {
        vol.Required("face_id"): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
        vol.Optional("style", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=15)),
        vol.Optional("handshake_only", default=False): cv.boolean,
        vol.Optional("wait", default=True): cv.boolean,
    }
)
SERVICE_FORGET_BOND = "forget_bond"
SERVICE_FETCH_FACE_PREVIEWS = "fetch_face_previews"
FETCH_FACE_PREVIEWS_SCHEMA = vol.Schema(
    {vol.Optional("face_ids"): vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1, max=255))])}
)

_NOT_READY = "The Galaxy Fit3 is not connected; try again when it is nearby."


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Once per Home Assistant: serve the dashboard strategy (config entries are set up separately)."""
    await async_setup_frontend(hass)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply an options change (e.g. the 24-hour toggle) to the watch without a reload."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_apply_options()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Galaxy Fit3 from a config entry."""
    coordinator = Fit3Coordinator(hass, entry)
    await coordinator.async_load_history()
    # names and pictures for the face selects; never blocks setup (falls back to the cached copy)
    entry.async_create_background_task(hass, coordinator.async_load_store_catalogue(), f"{DOMAIN} store catalogue")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async def _release_on_stop(_event) -> None:
        # A restart used to cut the link without telling the watch, which then refused new connections
        # for minutes to an hour. Disconnect properly while Home Assistant is still shutting down.
        try:
            await asyncio.wait_for(coordinator.client.disconnect(), timeout=5)
        except Exception as err:  # noqa: BLE001 - shutting down; nothing more to do
            _LOGGER.debug("Fit3: disconnect at shutdown failed: %s", err)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _release_on_stop))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Never block HA startup on the watch: the first connection runs in the background and the
    # sensors read "unknown" until it completes (connect can take a while or fail if out of range).
    entry.async_create_background_task(hass, coordinator.async_refresh(), "galaxy_fit3_first_refresh")

    async def _handle_set_time(call: ServiceCall) -> None:
        # The settings-channel message the integration itself sends on every connect and hourly.
        is_24h = call.data.get("is_24h", entry.options.get(CONF_IS_24H, DEFAULT_IS_24H))
        async with coordinator.watch_session() as client:
            try:
                acked = await client.set_time_rtc(is_24h=is_24h, tz_offset_seconds=call.data.get("tz_offset_seconds"))
            except (NotConnected, NotImplementedError) as err:
                raise HomeAssistantError(_NOT_READY) from err
        if not acked:
            raise HomeAssistantError("The watch did not acknowledge the time message.")

    async def _handle_set_distance_unit(call: ServiceCall) -> None:
        async with coordinator.watch_session() as client:
            try:
                acked = await client.set_distance_unit(call.data["unit"])
            except (NotConnected, NotImplementedError) as err:
                raise HomeAssistantError(_NOT_READY) from err
        if not acked:
            raise HomeAssistantError("The watch did not acknowledge the distance unit.")

    async def _handle_reconnect(call: ServiceCall) -> None:
        await coordinator.reconnect(call.data.get("replay_frames"), call.data["skip_time_frame"], call.data["fresh_pairing"])

    async def _handle_request_health_sync(call: ServiceCall) -> None:
        async with coordinator.watch_session() as client:
            try:
                await client.request_health_sync(call.data["kind"], call.data["window_days"])
            except (NotConnected, NotImplementedError) as err:
                raise HomeAssistantError(_NOT_READY) from err

    async def _handle_set_watch_face(call: ServiceCall) -> None:
        try:
            await coordinator.async_show_face(call.data["face_id"], call.data.get("group"))
        except (NotConnected, NotImplementedError) as err:
            raise HomeAssistantError(_NOT_READY) from err

    async def _handle_list_watch_faces(call: ServiceCall) -> ServiceResponse:
        async with coordinator.watch_session() as client:
            try:
                faces = await client.list_watch_faces()
            except (NotConnected, NotImplementedError) as err:
                raise HomeAssistantError(_NOT_READY) from err
        return {
            "watch_faces": [
                {"face_id": f.face_id, "group": f.group, "package": f.package, "name": f.name, "active": f.active}
                for f in faces
            ]
        }

    async def _handle_forget_bond(call: ServiceCall) -> None:
        await coordinator.forget_bond()

    hass.services.async_register(DOMAIN, SERVICE_FORGET_BOND, _handle_forget_bond)
    hass.services.async_register(DOMAIN, SERVICE_SET_TIME, _handle_set_time, schema=SET_TIME_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_DISTANCE_UNIT, _handle_set_distance_unit, schema=SET_DISTANCE_UNIT_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_RECONNECT, _handle_reconnect, schema=RECONNECT_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_REQUEST_HEALTH_SYNC, _handle_request_health_sync, schema=REQUEST_HEALTH_SYNC_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_WATCH_FACE, _handle_set_watch_face, schema=SET_WATCH_FACE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_WATCH_FACES,
        _handle_list_watch_faces,
        supports_response=SupportsResponse.ONLY,
    )

    async def _handle_install_watch_face(call: ServiceCall) -> ServiceResponse:
        if not call.data["wait"] and not call.data["handshake_only"]:
            # dashboards: return at once; the face-install sensor shows the progress
            coordinator.async_start_install(call.data["face_id"], call.data["style"])
            return {"started": True, "face_id": call.data["face_id"]}
        return await coordinator.async_install_face(call.data["face_id"], call.data["style"], call.data["handshake_only"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_INSTALL_WATCH_FACE,
        _handle_install_watch_face,
        schema=INSTALL_WATCH_FACE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _handle_fetch_face_previews(call: ServiceCall) -> ServiceResponse:
        return {"queued": coordinator.async_start_preview_fetch(call.data.get("face_ids"))}

    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_FACE_PREVIEWS,
        _handle_fetch_face_previews,
        schema=FETCH_FACE_PREVIEWS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].get(entry.entry_id)
        if coordinator is not None:
            await coordinator.client.disconnect()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SET_TIME)
            hass.services.async_remove(DOMAIN, SERVICE_RECONNECT)
            hass.services.async_remove(DOMAIN, SERVICE_REQUEST_HEALTH_SYNC)
            hass.services.async_remove(DOMAIN, SERVICE_SET_WATCH_FACE)
            hass.services.async_remove(DOMAIN, SERVICE_LIST_WATCH_FACES)
            hass.services.async_remove(DOMAIN, SERVICE_INSTALL_WATCH_FACE)
            hass.services.async_remove(DOMAIN, SERVICE_FORGET_BOND)
            hass.services.async_remove(DOMAIN, SERVICE_FETCH_FACE_PREVIEWS)
    return unload_ok
