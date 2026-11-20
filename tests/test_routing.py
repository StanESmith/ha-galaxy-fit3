"""Tests for routing.py: which adapters may carry a connection to the watch."""

from __future__ import annotations

from types import SimpleNamespace

import routing

ADDR = "AA:BB:CC:DD:EE:03"


def _objects():
    return {
        "/org/bluez": {"org.bluez.AgentManager1": {}},
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": "AA:BB:CC:DD:EE:02"}},
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_03": {"org.bluez.Device1": {"Address": ADDR, "Bonded": True, "Paired": True}},
        "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_03": {"org.bluez.Device1": {"Address": ADDR, "Bonded": False, "Paired": False}},
        "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_01": {"org.bluez.Device1": {"Address": "AA:BB:CC:DD:EE:01", "Bonded": True}},
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_03/service0001": {"org.bluez.GattService1": {}},
    }


def test_devices_for_address_finds_each_adapter_and_ignores_other_objects():
    found = routing.devices_for_address(_objects(), ADDR.lower())
    assert set(found) == {"hci0", "hci1"}
    assert found["hci0"]["Bonded"] is True and found["hci1"]["Bonded"] is False


def test_policy_from_devices_collects_bonded_adapters():
    policy = routing.policy_from_devices(routing.devices_for_address(_objects(), ADDR))
    assert policy.bonded == frozenset({"hci0"})
    assert policy.allows("hci0") and not policy.allows("hci1")
    assert not policy.allows("AA:BB:CC:DD:EE:04")  # a remote proxy has no bond


def test_no_bond_anywhere_allows_local_adapters_only():
    policy = routing.policy_from_devices({})
    assert policy.allows("hci0") and policy.allows("hci1")
    assert not policy.allows("AA:BB:CC:DD:EE:04") and not policy.allows(None)


def test_unreadable_bond_state_restricts_nothing():
    policy = routing.policy_from_devices(None)
    assert policy.allows("hci1") and policy.allows("AA:BB:CC:DD:EE:04") and policy.allows(None)


def test_two_bonded_adapters_are_both_allowed():
    devices = {"hci0": {"Bonded": True}, "hci1": {"Bonded": True}}
    policy = routing.policy_from_devices(devices)
    assert policy.allows("hci0") and policy.allows("hci1") and not policy.allows("hci2")


def test_describe_names_the_reason():
    assert "hci0" in routing.policy_from_devices({"hci0": {"Bonded": True}}).describe()
    assert "no bond" in routing.policy_from_devices({}).describe()
    assert "unknown" in routing.policy_from_devices(None).describe()


def _scanner(adapter, stamps=None):
    return SimpleNamespace(adapter=adapter, discovered_device_timestamps=stamps or {})


def test_filtered_manager_only_hides_disallowed_scanners():
    hci0, hci1, proxy = _scanner("hci0"), _scanner("hci1"), _scanner("apollo")
    devices = [SimpleNamespace(scanner=s) for s in (hci1, proxy, hci0)]
    calls = []

    class Manager:
        extra = "passed through"

        def async_scanner_devices_by_address(self, address, connectable):
            calls.append((address, connectable))
            return devices

    holder = routing.RouteHolder(routing.RoutePolicy(frozenset({"hci0"})))
    filtered = routing.FilteredManager(Manager(), holder.allows_scanner)
    assert [d.scanner.adapter for d in filtered.async_scanner_devices_by_address(ADDR, True)] == ["hci0"]
    assert calls == [(ADDR, True)]
    assert filtered.extra == "passed through"


def test_filtered_manager_follows_a_policy_change():
    hci0, hci1 = _scanner("hci0"), _scanner("hci1")
    devices = [SimpleNamespace(scanner=s) for s in (hci0, hci1)]
    manager = SimpleNamespace(async_scanner_devices_by_address=lambda a, c: devices)
    holder = routing.RouteHolder(routing.RoutePolicy(frozenset({"hci0"})))
    filtered = routing.FilteredManager(manager, holder.allows_scanner)
    assert len(filtered.async_scanner_devices_by_address(ADDR, True)) == 1
    holder.policy = routing.RoutePolicy(frozenset({"hci0", "hci1"}))
    assert len(filtered.async_scanner_devices_by_address(ADDR, True)) == 2


def test_freshest_age_only_counts_allowed_scanners():
    near_but_unbonded = _scanner("hci1", {ADDR: 99.0})
    bonded = _scanner("hci0", {ADDR: 90.0})
    proxy = _scanner("apollo", {ADDR: 98.0})
    items = [SimpleNamespace(scanner=s) for s in (near_but_unbonded, bonded, proxy)]
    holder = routing.RouteHolder(routing.RoutePolicy(frozenset({"hci0"})))
    assert routing.freshest_age(items, ADDR, 100.0, holder.allows_scanner) == 10.0


def test_freshest_age_is_none_when_no_allowed_scanner_has_heard_it():
    items = [SimpleNamespace(scanner=_scanner("hci0", {})), SimpleNamespace(scanner=_scanner("hci1", {ADDR: 99.0}))]
    holder = routing.RouteHolder(routing.RoutePolicy(frozenset({"hci0"})))
    assert routing.freshest_age(items, ADDR, 100.0, holder.allows_scanner) is None


class _FakeHaClient:
    """Stands in for Home Assistant's client: it picks the strongest scanner the manager lists."""

    def _async_get_best_available_backend_and_device(self, manager):
        devices = sorted(manager.async_scanner_devices_by_address("AA", True), key=lambda d: d.rssi, reverse=True)
        if not devices:
            raise RuntimeError("no backend")
        return devices[0].scanner.adapter


def _manager(*pairs):
    items = [SimpleNamespace(scanner=_scanner(adapter), rssi=rssi) for adapter, rssi in pairs]
    return SimpleNamespace(async_scanner_devices_by_address=lambda a, c: items)


def test_pinned_client_skips_a_stronger_unbonded_adapter():
    route = routing.RouteHolder(routing.RoutePolicy(frozenset({"hci0"})))
    manager = _manager(("hci1", -60), ("hci0", -85), ("apollo", -70))
    assert _FakeHaClient()._async_get_best_available_backend_and_device(manager) == "hci1"
    pinned = routing.pin_client_class(_FakeHaClient, route)()
    assert pinned._async_get_best_available_backend_and_device(manager) == "hci0"
    route.policy = routing.RoutePolicy(frozenset({"hci0", "hci1"}))
    assert pinned._async_get_best_available_backend_and_device(manager) == "hci1"


def test_pinned_client_fails_when_no_bonded_adapter_hears_the_device():
    route = routing.RouteHolder(routing.RoutePolicy(frozenset({"hci0"})))
    pinned = routing.pin_client_class(_FakeHaClient, route)()
    try:
        pinned._async_get_best_available_backend_and_device(_manager(("hci1", -60)))
    except RuntimeError:
        return
    raise AssertionError("expected no backend")


def test_client_without_the_hook_is_left_alone():
    class Other:
        pass

    assert not routing.can_pin(Other)
    assert routing.pin_client_class(Other, routing.RouteHolder()) is Other


def test_connected_adapters_lists_only_adapters_holding_a_link():
    devices = {
        "hci0": {"Address": ADDR, "Connected": False},
        "hci1": {"Address": ADDR, "Connected": True},
        "hci2": {"Address": ADDR},
    }
    assert routing.connected_adapters(devices) == ["hci1"]
    assert routing.connected_adapters({}) == []
    assert routing.connected_adapters(None) == []


def test_block_changes_blocks_unbonded_local_adapters_while_a_bond_exists():
    devices = {
        "hci0": {"Address": ADDR, "Bonded": False},
        "hci1": {"Address": ADDR, "Bonded": True},
        "hci2": {"Address": ADDR, "Bonded": False, "Blocked": True},  # already blocked: no change
    }
    assert routing.block_changes(devices) == (["hci0"], [])


def test_block_changes_unblocks_everything_without_a_bond_and_never_blocks_the_bonded_adapter():
    devices = {
        "hci0": {"Address": ADDR, "Bonded": False, "Blocked": True},
        "hci1": {"Address": ADDR, "Bonded": False},
    }
    assert routing.block_changes(devices) == ([], ["hci0"])
    assert routing.block_changes({"hci1": {"Bonded": True, "Blocked": True}}) == ([], ["hci1"])
    assert routing.block_changes(None) == ([], [])


def test_classic_adapters_uses_the_bredr_bearer_and_the_adapter_address():
    w = "AA:BB:CC:DD:EE:FF"
    objects = {
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": "00:00:00:00:00:01"}},
        "/org/bluez/hci1": {"org.bluez.Adapter1": {"Address": "00:00:00:00:00:02"}},
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF": {
            "org.bluez.Device1": {"Address": w, "Bonded": False},
            "org.bluez.Bearer.BREDR1": {"Bonded": False},
        },
        "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF": {
            "org.bluez.Device1": {"Address": w, "Bonded": True},
            "org.bluez.Bearer.LE1": {"Bonded": True},
            "org.bluez.Bearer.BREDR1": {"Bonded": True},
        },
    }
    assert routing.classic_adapters(objects, w.lower()) == [("hci1", "00:00:00:00:00:02")]
    # an LE-only bond (no Classic key) does not count
    objects["/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"]["org.bluez.Bearer.BREDR1"]["Bonded"] = False
    assert routing.classic_adapters(objects, w) == []
    # BlueZ without bearer interfaces: fall back to the device's Bonded flag
    del objects["/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"]["org.bluez.Bearer.BREDR1"]
    assert routing.classic_adapters(objects, w) == [("hci1", "00:00:00:00:00:02")]
