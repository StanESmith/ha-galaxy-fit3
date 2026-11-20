"""The integration inside a real Home Assistant: setup, entities, services and translations."""

from __future__ import annotations

from unittest.mock import patch

import pytest
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
    for service in ("set_time", "set_watch_face", "list_watch_faces", "install_watch_face", "reconnect"):
        assert hass.services.has_service(DOMAIN, service), service


async def test_service_descriptions_match_the_schemas(hass) -> None:
    descriptions = (await async_get_all_descriptions(hass))[DOMAIN]
    assert {"face_id", "style", "handshake_only", "wait"} <= set(descriptions["install_watch_face"]["fields"])
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
