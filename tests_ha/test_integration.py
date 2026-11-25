"""The integration inside a real Home Assistant: setup, entities, services and translations."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service import async_get_all_descriptions

from custom_components.galaxy_fit3 import coordinator as coordinator_mod
from custom_components.galaxy_fit3.const import DOMAIN

from .conftest import ADDRESS, watch_faces

pytestmark = pytest.mark.usefixtures("integration")


def _entity_id(entity_registry, platform: str, unique_suffix: str) -> str:
    entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, f"{ADDRESS}_{unique_suffix}")
    assert entity_id is not None, f"no {platform} entity with unique id ..._{unique_suffix}"
    return entity_id


async def test_setup_loads_and_unloads(hass, integration) -> None:
    assert integration.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(integration.entry_id)
    assert integration.state is ConfigEntryState.NOT_LOADED


async def test_device_is_registered(hass, integration, device_registry) -> None:
    device = device_registry.async_get_device_by_identifier((DOMAIN, ADDRESS), integration.entry_id)
    assert device is not None
    assert device.manufacturer == "Samsung"


async def test_sensors_report_the_watch(hass, entity_registry) -> None:
    states = {
        key: hass.states.get(_entity_id(entity_registry, "sensor", key)).state
        for key in ("battery_level", "heart_rate", "steps")
    }
    assert states == {"battery_level": "64", "heart_rate": "75", "steps": "1234"}


async def test_entity_names_are_translated(hass, entity_registry) -> None:
    names = {
        key: hass.states.get(_entity_id(entity_registry, "sensor", key)).attributes["friendly_name"]
        for key in ("heart_rate", "steps")
    }
    assert names["heart_rate"].endswith("Heart rate")
    assert names["steps"].endswith("Steps")


async def test_watch_face_select_lists_the_faces_by_store_name(hass, entity_registry) -> None:
    state = hass.states.get(_entity_id(entity_registry, "select", "watch_face"))
    assert state.state == "Fitness pro 3"
    assert state.attributes["options"] == ["Fitness pro 3", "Progress gauge 1"]


async def test_selecting_a_face_switches_the_watch(hass, entity_registry, client) -> None:
    entity_id = _entity_id(entity_registry, "select", "watch_face")
    await hass.services.async_call(
        "select", "select_option", {"entity_id": entity_id, "option": "Progress gauge 1"}, blocking=True
    )
    client.set_watch_face.assert_awaited_once_with(16, 1)
    assert hass.states.get(entity_id).state == "Progress gauge 1"


async def test_store_select_offers_every_store_face(hass, entity_registry) -> None:
    state = hass.states.get(_entity_id(entity_registry, "select", "store_face"))
    assert {"Fitness pro 3", "Progress gauge 1", "Step streak"} <= set(state.attributes["options"])


async def test_services_are_registered(hass) -> None:
    for service in ("set_time", "set_distance_unit", "set_watch_face", "list_watch_faces", "install_watch_face", "reconnect",
                    "forget_bond", "request_health_sync", "fetch_face_previews"):
        assert hass.services.has_service(DOMAIN, service), service


async def test_the_diagnostic_experiments_are_no_longer_actions(hass) -> None:
    for service in ("gatt_dump", "write_current_time", "set_time_rtc"):
        assert not hass.services.has_service(DOMAIN, service), service


async def test_service_descriptions_match_the_schemas(hass) -> None:
    descriptions = (await async_get_all_descriptions(hass))[DOMAIN]
    assert {"face_id", "style", "handshake_only", "wait"} <= set(descriptions["install_watch_face"]["fields"])
    assert set(descriptions["set_time"]["fields"]) == {"is_24h", "tz_offset_seconds"}
    assert {"kind", "window_days"} <= set(descriptions["request_health_sync"]["fields"])
    assert "face_id" in descriptions["set_watch_face"]["fields"]


async def test_install_service_returns_at_once_when_not_waiting(hass) -> None:
    with patch.object(coordinator_mod.Fit3Coordinator, "async_start_install") as start:
        result = await hass.services.async_call(
            DOMAIN, "install_watch_face", {"face_id": 79, "wait": False}, blocking=True, return_response=True
        )
    start.assert_called_once_with(79, 0)
    assert result == {"started": True, "face_id": 79}


async def test_install_service_rejects_a_face_outside_the_valid_range(hass) -> None:
    with pytest.raises(Exception, match="face_id"):
        await hass.services.async_call(DOMAIN, "install_watch_face", {"face_id": 999}, blocking=True)


async def test_set_watch_face_service_reaches_the_client(hass, client) -> None:
    client.set_watch_face.return_value = watch_faces(active=16)
    await hass.services.async_call(DOMAIN, "set_watch_face", {"face_id": 16, "group": 1}, blocking=True)
    client.set_watch_face.assert_awaited_once_with(16, 1)


@pytest.mark.parametrize("reachable", [False])
async def test_a_service_fails_cleanly_when_the_watch_is_out_of_reach(hass) -> None:
    with pytest.raises(HomeAssistantError, match="not reachable"):
        await hass.services.async_call(DOMAIN, "set_watch_face", {"face_id": 16}, blocking=True)


async def test_set_time_sends_the_settings_channel_message_with_the_clock_option(hass, client) -> None:
    await hass.services.async_call(DOMAIN, "set_time", {}, blocking=True)
    client.set_time_rtc.assert_awaited_once_with(is_24h=False, tz_offset_seconds=None)  # the option's default


async def test_set_time_takes_explicit_values(hass, client) -> None:
    await hass.services.async_call(DOMAIN, "set_time", {"is_24h": True, "tz_offset_seconds": -18000}, blocking=True)
    client.set_time_rtc.assert_awaited_once_with(is_24h=True, tz_offset_seconds=-18000)


async def test_set_time_reports_a_watch_that_does_not_acknowledge(hass, client) -> None:
    client.set_time_rtc.return_value = False
    with pytest.raises(HomeAssistantError, match="did not acknowledge"):
        await hass.services.async_call(DOMAIN, "set_time", {}, blocking=True)


async def test_request_health_sync_reaches_the_client(hass, client) -> None:
    await hass.services.async_call(DOMAIN, "request_health_sync", {"kind": "measure_heart_rate"}, blocking=True)
    client.request_health_sync.assert_awaited_once_with("measure_heart_rate", 1)


async def test_the_new_readings_are_sensors(hass, entity_registry) -> None:
    calories = hass.states.get(_entity_id(entity_registry, "sensor", "calories"))
    assert calories.state == "123.5" and calories.attributes["unit_of_measurement"] == "kcal"
    distance = hass.states.get(_entity_id(entity_registry, "sensor", "distance"))
    assert distance.attributes["unit_of_measurement"] == "km"  # stored in metres, shown in kilometres
    assert float(distance.state) == pytest.approx(2.3457, abs=0.01)


async def test_the_resting_heart_rate_sensor_is_off_until_wanted(hass, entity_registry) -> None:
    entity_id = _entity_id(entity_registry, "sensor", "resting_heart_rate")
    assert entity_registry.async_get(entity_id).disabled_by is not None
    assert hass.states.get(entity_id) is None


async def test_polling_every_five_minutes_by_default_and_as_often_as_the_option_says(hass, integration) -> None:
    from datetime import timedelta

    coordinator = hass.data[DOMAIN][integration.entry_id]
    assert coordinator.update_interval == timedelta(minutes=5)
    hass.config_entries.async_update_entry(integration, options={"scan_interval": 10})
    await hass.async_block_till_done()
    assert coordinator.update_interval == timedelta(minutes=10)


async def test_set_distance_unit_sends_the_unit_to_the_watch(hass, client) -> None:
    client.set_distance_unit.return_value = True
    await hass.services.async_call(DOMAIN, "set_distance_unit", {"unit": "mi"}, blocking=True)
    client.set_distance_unit.assert_awaited_once_with("mi")
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "set_distance_unit", {"unit": "yd"}, blocking=True)


async def test_set_distance_unit_reports_a_watch_that_does_not_acknowledge(hass, client) -> None:
    client.set_distance_unit.return_value = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "set_distance_unit", {"unit": "km"}, blocking=True)


async def test_the_watch_distance_unit_follows_the_option_or_home_assistant(hass, integration) -> None:
    from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM

    coordinator = hass.data[DOMAIN][integration.entry_id]
    hass.config.units = METRIC_SYSTEM
    assert coordinator.distance_unit() == "km"  # "auto" by default
    hass.config.units = US_CUSTOMARY_SYSTEM
    assert coordinator.distance_unit() == "mi"
    hass.config_entries.async_update_entry(integration, options={"distance_unit": "km"})
    assert coordinator.distance_unit() == "km"  # an explicit choice wins over the unit system
