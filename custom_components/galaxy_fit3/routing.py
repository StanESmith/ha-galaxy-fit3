"""Which Bluetooth adapters may carry a connection to the watch.

A Bluetooth bond belongs to one host identity: the watch only accepts an encrypted link from an
adapter it has bonded with, and every local adapter (and every ESPHome proxy) is a different host.
Home Assistant picks the strongest scanner that has a free slot, so a stronger but unbonded adapter
wins and the watch answers "Insufficient authentication". :class:`RoutePolicy` limits the choice to
adapters that hold a bond; :class:`FilteredManager` applies it to Home Assistant's choice.

Pure Python, no Home Assistant or bleak imports, so it is tested directly.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_LOCAL_ADAPTER = re.compile(r"^hci\d+$")
_DEVICE_PATH = re.compile(r"^/org/bluez/(hci\d+)/dev_[0-9A-F_]+$")

Device1 = Mapping[str, Any]


def is_local_adapter(adapter: str | None) -> bool:
    """``hci0``-style names are BlueZ adapters in this host; anything else is a remote scanner."""
    return bool(adapter) and _LOCAL_ADAPTER.match(adapter) is not None


def devices_for_address(managed_objects: Mapping[str, Mapping[str, Mapping[str, Any]]], address: str) -> dict[str, dict]:
    """``{adapter: Device1 properties}`` for every BlueZ adapter that knows ``address``.

    ``managed_objects`` is BlueZ's ``GetManagedObjects`` result with the D-Bus variants already
    unwrapped to plain values.
    """
    wanted = address.upper()
    found: dict[str, dict] = {}
    for path, interfaces in managed_objects.items():
        match = _DEVICE_PATH.match(path)
        props = interfaces.get("org.bluez.Device1")
        if match and props and str(props.get("Address", "")).upper() == wanted:
            found[match.group(1)] = dict(props)
    return found


def classic_adapters(managed_objects: Mapping[str, Mapping[str, Mapping[str, Any]]], address: str) -> list[tuple[str, str]]:
    """``(adapter, adapter address)`` for each local adapter holding a Bluetooth Classic bond with ``address``.

    BlueZ 5.81+ reports each transport on ``org.bluez.Bearer.BREDR1`` / ``Bearer.LE1``; an LE pairing with
    cross-transport key derivation leaves a Classic bond there too. Older BlueZ without the bearer
    interfaces: the device's overall ``Bonded`` flag is used instead.
    """
    wanted = address.upper()
    found: list[tuple[str, str]] = []
    for path, interfaces in managed_objects.items():
        match = _DEVICE_PATH.match(path)
        device = interfaces.get("org.bluez.Device1")
        if not (match and device and str(device.get("Address", "")).upper() == wanted):
            continue
        bredr = interfaces.get("org.bluez.Bearer.BREDR1")
        bonded = bredr.get("Bonded") if bredr is not None else device.get("Bonded")
        adapter = match.group(1)
        adapter_address = managed_objects.get(f"/org/bluez/{adapter}", {}).get("org.bluez.Adapter1", {}).get("Address")
        if bonded and adapter_address and is_local_adapter(adapter):
            found.append((adapter, str(adapter_address)))
    return sorted(found)


def connected_adapters(devices: Mapping[str, Device1] | None) -> list[str]:
    """Adapters on which BlueZ reports the watch as connected (from :func:`devices_for_address` output)."""
    return sorted(adapter for adapter, props in (devices or {}).items() if props.get("Connected"))


def block_changes(devices: Mapping[str, Device1] | None) -> tuple[list[str], list[str]]:
    """``(block, unblock)``: local adapters on which to set BlueZ ``Blocked`` for the watch, and to clear it.

    While some adapter holds a bond, every other local adapter is blocked. The watch was seen
    connecting to an unbonded adapter on its own (probably one it was bonded with before); BlueZ
    accepted the link with no client on it, and the watch then stopped advertising. With no bond anywhere nothing stays
    blocked, so pairing can use any adapter.
    """
    devices = devices or {}
    bonded = {adapter for adapter, props in devices.items() if props.get("Bonded")}
    block = sorted(
        adapter for adapter, props in devices.items()
        if bonded and adapter not in bonded and is_local_adapter(adapter) and not props.get("Blocked")
    )
    unblock = sorted(
        adapter for adapter, props in devices.items() if props.get("Blocked") and (not bonded or adapter in bonded)
    )
    return block, unblock


@dataclass(frozen=True)
class RoutePolicy:
    """The adapters a connection may use.

    ``bonded`` is the set of local adapters that hold a bond for the watch. ``None`` means the
    bonds could not be read, so nothing is restricted. An empty set (no bond anywhere, e.g. first
    pairing) allows every local adapter, since any of them can pair, but never a remote proxy,
    which cannot answer BlueZ's pairing request.
    """

    bonded: frozenset[str] | None = frozenset()

    def allows(self, adapter: str | None) -> bool:
        if self.bonded is None:
            return True
        if self.bonded:
            return adapter in self.bonded
        return is_local_adapter(adapter)

    def describe(self) -> str:
        if self.bonded is None:
            return "bond state unknown, any adapter"
        if not self.bonded:
            return "no bond yet, any local adapter"
        return "bonded on " + ", ".join(sorted(self.bonded))


def policy_from_devices(devices: Mapping[str, Device1] | None) -> RoutePolicy:
    """Build the policy from :func:`devices_for_address` output (``None`` = BlueZ unreachable)."""
    if devices is None:
        return RoutePolicy(None)
    return RoutePolicy(frozenset(adapter for adapter, props in devices.items() if props.get("Bonded")))


class FilteredManager:
    """Stands in for Home Assistant's Bluetooth manager while it picks the connection path.

    Only the list of scanners that can reach the device is filtered; every other attribute is
    passed through to the real manager.
    """

    def __init__(self, manager: Any, allows: Callable[[Any], bool]) -> None:
        self._manager = manager
        self._allows = allows

    def async_scanner_devices_by_address(self, address: str, connectable: bool) -> list:
        return [d for d in self._manager.async_scanner_devices_by_address(address, connectable) if self._allows(d.scanner)]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)


@dataclass
class RouteHolder:
    """The current policy, refreshed before each connect and read when Home Assistant picks a path."""

    policy: RoutePolicy = RoutePolicy()

    def allows_scanner(self, scanner: Any) -> bool:
        return self.policy.allows(getattr(scanner, "adapter", None))


def freshest_age(
    scanner_devices: Iterable[Any], address: str, now: float, allows: Callable[[Any], bool]
) -> float | None:
    """Seconds since an allowed scanner last heard ``address`` (``None`` = none of them has)."""
    ages = []
    for item in scanner_devices:
        scanner = item.scanner
        if not allows(scanner):
            continue
        stamp = scanner.discovered_device_timestamps.get(address)
        if stamp is not None:
            ages.append(max(0.0, now - stamp))
    return min(ages) if ages else None


_PICK_PATH = "_async_get_best_available_backend_and_device"


def pin_client_class(base: type, route: RouteHolder) -> type:
    """``base`` (Home Assistant's retry-connector client) limited to the adapters ``route`` allows.

    Home Assistant ranks every scanner that hears the device by signal and free slots, in a method
    of its Bluetooth client. The returned subclass runs that same method on a manager that hides the
    scanners without a bond. If ``base`` has no such method (a Home Assistant that no longer picks
    paths this way) it is returned unchanged.
    """
    if not hasattr(base, _PICK_PATH):
        return base

    class PinnedClient(base):  # type: ignore[valid-type, misc]
        def _async_get_best_available_backend_and_device(self, manager):
            return getattr(super(), _PICK_PATH)(FilteredManager(manager, route.allows_scanner))

    return PinnedClient


def can_pin(base: type) -> bool:
    return hasattr(base, _PICK_PATH)
