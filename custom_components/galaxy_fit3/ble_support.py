"""Home Assistant / BlueZ glue shared by the coordinator and the config flow."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import bleak_retry_connector
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import routing
from .client import GalaxyFit3Client
from .sap import tzinfo

_LOGGER = logging.getLogger(__name__)


_FALLBACK_ADAPTER = "hci0"  # used when BlueZ cannot tell us which adapters know the watch


def device_path(address: str, adapter: str = _FALLBACK_ADAPTER) -> str:
    return f"/org/bluez/{adapter}/dev_" + address.replace(":", "_").upper()


async def _bluez_devices(bus, address: str) -> dict[str, dict]:
    """``{adapter: Device1 properties}`` for every local adapter that has a record of ``address``."""
    intro = await bus.introspect("org.bluez", "/")
    manager = bus.get_proxy_object("org.bluez", "/", intro).get_interface("org.freedesktop.DBus.ObjectManager")
    managed = await manager.call_get_managed_objects()
    plain = {
        path: {iface: {k: v.value for k, v in props.items()} for iface, props in ifaces.items()}
        for path, ifaces in managed.items()
    }
    return routing.devices_for_address(plain, address)


async def route_policy(address: str) -> routing.RoutePolicy:
    """Which adapters hold a bond for the watch (all of them allowed if BlueZ cannot be read)."""
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            return routing.policy_from_devices(await _bluez_devices(bus, address))
        finally:
            bus.disconnect()
    except Exception as err:  # noqa: BLE001 - without BlueZ we simply do not restrict the route
        _LOGGER.debug("Fit3 %s: could not read the BlueZ bonds: %s", address, err)
        return routing.RoutePolicy(None)


def _pinned_client_class(route: routing.RouteHolder):
    """Home Assistant's retry-connector client, limited to the adapters in ``route`` when it picks a path.

    Home Assistant replaces ``bleak_retry_connector.BleakClientWithServiceCache`` with its own
    subclass once its Bluetooth integration is set up, so it is looked up here, not at import time.
    """
    base = bleak_retry_connector.BleakClientWithServiceCache
    if not routing.can_pin(base):
        _LOGGER.warning("Home Assistant's Bluetooth client cannot be limited to bonded adapters; using its own choice")
    return routing.pin_client_class(base, route)


def _tz_provider(hass: HomeAssistant):
    """``() -> (current UTC offset in seconds incl. DST, display name)`` for HA's configured zone."""

    def provider() -> tuple[int, str]:
        tz_id = hass.config.time_zone
        try:
            off = int(datetime.now(ZoneInfo(tz_id)).utcoffset().total_seconds())
        except Exception:  # noqa: BLE001 - unknown zone: fall back to UTC rather than break pairing
            tz_id, off = "UTC", 0
        return off, tzinfo.timezone_name(tz_id, off)

    return provider


def build_client(
    hass: HomeAssistant,
    address: str,
    on_disconnect=None,
    is_24h_provider=None,
    route: routing.RouteHolder | None = None,
) -> GalaxyFit3Client:
    """A client that connects through HA's retry connector with the device HA discovered, which
    forces the LE bearer (a bare MAC lets BlueZ pick classic Bluetooth and fail).

    ``route`` limits which adapters may carry the connection (default: any local adapter)."""
    client_class = _pinned_client_class(route or routing.RouteHolder())

    async def _connect(device, name):
        return await establish_connection(
            client_class,
            device,
            name,
            disconnected_callback=on_disconnect,
            max_attempts=2,
        )

    return GalaxyFit3Client(
        address,
        resolve_device=lambda: bluetooth.async_ble_device_from_address(hass, address, connectable=True),
        connector=_connect,
        tz_provider=_tz_provider(hass),
        is_24h_provider=is_24h_provider,
    )


def seconds_since_seen(hass: HomeAssistant, address: str, route: routing.RouteHolder | None = None) -> float | None:
    """How long ago a scanner that may carry the connection last heard the watch (``None`` = none has).

    Used to avoid connection attempts while the watch is out of range: on a single adapter every
    attempt takes time away from scanning, which starves other Bluetooth devices of their
    advertisements. Scanners without a bond are ignored, since a connection could not use them.
    """
    holder = route or routing.RouteHolder()
    by_address = getattr(bluetooth, "async_scanner_devices_by_address", None)
    if by_address is None:  # an HA without per-scanner lookup: use whichever scanner heard it last
        info = bluetooth.async_last_service_info(hass, address, connectable=True)
        return None if info is None else max(0.0, time.monotonic() - info.time)
    return routing.freshest_age(
        by_address(hass, address, connectable=True), address, time.monotonic(), holder.allows_scanner
    )


def heard_by(hass: HomeAssistant, address: str) -> list[str]:
    """Names of the scanners that currently hear the watch (for the log)."""
    by_address = getattr(bluetooth, "async_scanner_devices_by_address", None)
    if by_address is None:
        return []
    return sorted(
        str(getattr(d.scanner, "adapter", None) or d.scanner.name) for d in by_address(hass, address, connectable=True)
    )


async def prefer_le_bearer(address: str) -> None:
    """Tell BlueZ to use the LE bearer for this dual-mode watch (best effort), on every adapter that knows it.

    With PreferredBearer=last-used and no history, BlueZ guessed BR/EDR and failed with
    ``org.bluez.Error.BREDR.ProfileUnavailable``. Silently skipped if D-Bus/BlueZ isn't reachable.
    """
    try:
        from dbus_fast import BusType, Variant
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            for adapter in await _bluez_devices(bus, address) or {_FALLBACK_ADAPTER: {}}:
                path = device_path(address, adapter)
                try:
                    intro = await bus.introspect("org.bluez", path)
                    props = bus.get_proxy_object("org.bluez", path, intro).get_interface(
                        "org.freedesktop.DBus.Properties"
                    )
                    await props.call_set("org.bluez.Device1", "PreferredBearer", Variant("s", "le"))
                    _LOGGER.debug("Fit3 %s: set BlueZ PreferredBearer=le on %s", address, adapter)
                except Exception as err:  # noqa: BLE001 - one adapter failing must not stop the others
                    _LOGGER.debug("Fit3 %s: could not set PreferredBearer on %s: %s", address, adapter, err)
        finally:
            bus.disconnect()
    except Exception as err:  # noqa: BLE001 - optional optimisation, never fatal
        _LOGGER.debug("Fit3 %s: could not set PreferredBearer: %s", address, err)


async def bluez_device_state(address: str) -> dict[str, dict[str, object]] | None:
    """BlueZ's view of the watch on each adapter (paired, bonded, connected, signal), for diagnosing failed connects.

    ``None`` if D-Bus/BlueZ isn't reachable.
    """
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            devices = await _bluez_devices(bus, address)
        finally:
            bus.disconnect()
        wanted = ("Paired", "Bonded", "Trusted", "Connected", "ServicesResolved", "RSSI", "AddressType")
        return {adapter: {k: props[k] for k in wanted if k in props} for adapter, props in devices.items()}
    except Exception as err:  # noqa: BLE001 - diagnostics only
        _LOGGER.debug("Fit3 %s: could not read the BlueZ device state: %s", address, err)
        return None


async def update_blocked_adapters(address: str) -> tuple[list[str], list[str]]:
    """Block the watch on local adapters without a bond (see :func:`routing.block_changes`); returns what changed."""
    try:
        from dbus_fast import BusType, Variant
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            block, unblock = routing.block_changes(await _bluez_devices(bus, address))
            done: tuple[list[str], list[str]] = ([], [])
            for adapter, value, changed in [(a, True, done[0]) for a in block] + [(a, False, done[1]) for a in unblock]:
                path = device_path(address, adapter)
                try:
                    intro = await bus.introspect("org.bluez", path)
                    props = bus.get_proxy_object("org.bluez", path, intro).get_interface("org.freedesktop.DBus.Properties")
                    await props.call_set("org.bluez.Device1", "Blocked", Variant("b", value))
                    changed.append(adapter)
                except Exception as err:  # noqa: BLE001 - one adapter failing must not stop the others
                    _LOGGER.debug("Fit3 %s: could not set Blocked=%s on %s: %s", address, value, adapter, err)
            return done
        finally:
            bus.disconnect()
    except Exception as err:  # noqa: BLE001 - best effort, never fatal
        _LOGGER.debug("Fit3 %s: could not update blocked adapters: %s", address, err)
        return [], []


async def drop_orphaned_links(address: str) -> list[str]:
    """Disconnect the watch on every adapter where BlueZ still holds a link; returns those adapters.

    Call only while the integration has no connection of its own. A Home Assistant restart in the
    middle of a poll leaves BlueZ connected with no client on the link, and a connected watch stops
    advertising, so without this the coordinator waits for an advertisement that never comes.
    """
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            dropped = []
            for adapter in routing.connected_adapters(await _bluez_devices(bus, address)):
                path = device_path(address, adapter)
                try:
                    intro = await bus.introspect("org.bluez", path)
                    await bus.get_proxy_object("org.bluez", path, intro).get_interface("org.bluez.Device1").call_disconnect()
                    dropped.append(adapter)
                except Exception as err:  # noqa: BLE001 - one adapter failing must not stop the others
                    _LOGGER.debug("Fit3 %s: could not drop the link on %s: %s", address, adapter, err)
            return dropped
        finally:
            bus.disconnect()
    except Exception as err:  # noqa: BLE001 - best effort, never fatal
        _LOGGER.debug("Fit3 %s: could not check for leftover links: %s", address, err)
        return []


async def classic_adapter(address: str) -> tuple[str, str] | None:
    """``(adapter, adapter address)`` of a local adapter with a Bluetooth Classic bond to the watch, or None."""
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            intro = await bus.introspect("org.bluez", "/")
            manager = bus.get_proxy_object("org.bluez", "/", intro).get_interface("org.freedesktop.DBus.ObjectManager")
            managed = await manager.call_get_managed_objects()
        finally:
            bus.disconnect()
    except Exception as err:  # noqa: BLE001 - reported to the caller as "no adapter"
        _LOGGER.debug("Fit3 %s: could not read the BlueZ objects: %s", address, err)
        return None
    plain = {
        path: {iface: {k: v.value for k, v in props.items()} for iface, props in ifaces.items()}
        for path, ifaces in managed.items()
    }
    adapters = routing.classic_adapters(plain, address)
    return adapters[0] if adapters else None


async def forget_bond(address: str) -> None:
    """Remove the watch's BlueZ device (and its bond keys) from every adapter so the next connect pairs fresh."""
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            for adapter in await _bluez_devices(bus, address):
                adapter_path = f"/org/bluez/{adapter}"
                intro = await bus.introspect("org.bluez", adapter_path)
                iface = bus.get_proxy_object("org.bluez", adapter_path, intro).get_interface("org.bluez.Adapter1")
                await iface.call_remove_device(device_path(address, adapter))
                _LOGGER.debug("Fit3 %s: removed BlueZ bond on %s", address, adapter)
        finally:
            bus.disconnect()
    except Exception as err:  # noqa: BLE001 - optional, never fatal
        _LOGGER.debug("Fit3 %s: no bond removed: %s", address, err)
