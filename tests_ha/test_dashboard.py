"""The dashboard side: the strategy's data (WebSocket), its served script, and the preview service."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.galaxy_fit3 import dashboard
from custom_components.galaxy_fit3.const import DOMAIN

from .conftest import ADDRESS

pytestmark = pytest.mark.usefixtures("integration")

ROOT = Path(__file__).resolve().parents[1]


def _entity_id(entity_registry, platform: str, unique_suffix: str) -> str:
    return entity_registry.async_get_entity_id(platform, DOMAIN, f"{ADDRESS}_{unique_suffix}")


async def _faces(hass, hass_ws_client, **extra) -> dict:
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "galaxy_fit3/faces", **extra})
    return await ws.receive_json()


async def test_the_strategy_gets_entity_ids_and_every_face_by_name(hass, hass_ws_client, entity_registry) -> None:
    msg = await _faces(hass, hass_ws_client)
    assert msg["success"]
    result = msg["result"]
    assert result["entities"] == {
        "battery": _entity_id(entity_registry, "sensor", "battery_level"),
        "heart_rate": _entity_id(entity_registry, "sensor", "heart_rate"),
        "steps": _entity_id(entity_registry, "sensor", "steps"),
        "watch_face": _entity_id(entity_registry, "select", "watch_face"),
        "store_face": _entity_id(entity_registry, "select", "store_face"),
        "install": _entity_id(entity_registry, "sensor", "face_install"),
        "install_button": _entity_id(entity_registry, "button", "install_face"),
    }
    assert all(result["entities"].values())
    assert result["faces"] == [
        {"id": 106, "name": "Fitness pro 3", "preview": "https://img.example/106.png"},
        {"id": 16, "name": "Progress gauge 1", "preview": "https://img.example/16.png"},
        {"id": 79, "name": "Step streak", "preview": "https://img.example/79.png"},
    ]
    assert ADDRESS not in json.dumps(result)  # the watch's Bluetooth address never leaves the integration


async def test_a_face_with_a_saved_preview_uses_it(hass, hass_ws_client, integration) -> None:
    hass.data[DOMAIN][integration.entry_id].local_previews = {16}
    faces = (await _faces(hass, hass_ws_client))["result"]["faces"]
    assert {f["id"]: f["preview"] for f in faces}[16] == "/local/galaxy_fit3/faces/face_16.png"


async def test_a_face_the_store_does_not_list_still_appears_by_its_label(hass, hass_ws_client, integration) -> None:
    coordinator = hass.data[DOMAIN][integration.entry_id]
    coordinator.store_faces = {k: v for k, v in coordinator.store_faces.items() if k != 106}
    faces = (await _faces(hass, hass_ws_client))["result"]["faces"]
    assert "Face 106" in [f["name"] for f in faces]


async def test_an_entry_can_be_named(hass, hass_ws_client, integration) -> None:
    msg = await _faces(hass, hass_ws_client, entry_id=integration.entry_id)
    assert msg["success"] and msg["result"]["entry_id"] == integration.entry_id


async def test_an_unknown_entry_is_an_error(hass, hass_ws_client) -> None:
    msg = await _faces(hass, hass_ws_client, entry_id="nope")
    assert not msg["success"] and msg["error"]["code"] == "not_found"


async def test_without_a_set_up_watch_the_strategy_gets_an_error_it_can_show(hass, hass_ws_client, integration) -> None:
    await hass.config_entries.async_unload(integration.entry_id)
    msg = await _faces(hass, hass_ws_client)
    assert not msg["success"]
    assert msg["error"]["message"] == "no Galaxy Fit3 is set up"


async def test_the_select_publishes_which_faces_are_on_the_watch(hass, entity_registry) -> None:
    state = hass.states.get(_entity_id(entity_registry, "select", "watch_face"))
    assert state.attributes["face_ids"] == [106, 16]
    assert state.attributes["face_id"] == 106


async def test_the_strategy_script_is_served(hass, hass_client) -> None:
    resp = await (await hass_client()).get(f"{dashboard.STATIC_URL}/{dashboard.JS_FILE}")
    assert resp.status == 200
    assert "ll-strategy-view-galaxy-fit3" in await resp.text()


async def test_the_script_is_loaded_into_the_frontend_with_the_version_in_its_url(hass) -> None:
    hass.config.components.add("frontend")
    with (
        patch("homeassistant.components.frontend.add_extra_js_url") as add_url,
        patch.object(hass.http, "async_register_static_paths", AsyncMock()),  # already registered by the entry's setup
    ):
        await dashboard.async_setup_frontend(hass)
    version = json.loads((ROOT / "custom_components/galaxy_fit3/manifest.json").read_text())["version"]
    add_url.assert_called_once_with(hass, f"/galaxy_fit3_static/galaxy-fit3.js?v={version}")


async def test_a_headless_setup_skips_the_frontend_but_keeps_the_data_command(hass) -> None:
    assert "frontend" not in hass.config.components  # the test core runs without it
    with (
        patch("homeassistant.components.frontend.add_extra_js_url") as add_url,
        patch.object(hass.http, "async_register_static_paths", AsyncMock()),
    ):
        await dashboard.async_setup_frontend(hass)
    add_url.assert_not_called()


async def test_fetching_previews_saves_the_missing_ones_in_the_background(hass, integration) -> None:
    coordinator = hass.data[DOMAIN][integration.entry_id]
    coordinator.local_previews = {79}  # already has one
    with patch("custom_components.galaxy_fit3.face_store.async_fetch_preview", AsyncMock(return_value=True)) as fetch:
        result = await hass.services.async_call(DOMAIN, "fetch_face_previews", {}, blocking=True, return_response=True)
        await coordinator._preview_task
    assert result == {"queued": 2}
    assert sorted(call.args[1] for call in fetch.await_args_list) == [16, 106]


async def test_fetching_named_previews_that_exist_queues_nothing(hass, integration) -> None:
    hass.data[DOMAIN][integration.entry_id].local_previews = {16}
    result = await hass.services.async_call(
        DOMAIN, "fetch_face_previews", {"face_ids": [16]}, blocking=True, return_response=True
    )
    assert result == {"queued": 0}


async def test_a_failed_download_does_not_stop_the_rest(hass, integration) -> None:
    coordinator = hass.data[DOMAIN][integration.entry_id]
    fetch = AsyncMock(side_effect=[RuntimeError("store down"), True])
    with patch("custom_components.galaxy_fit3.face_store.async_fetch_preview", fetch):
        await hass.services.async_call(DOMAIN, "fetch_face_previews", {"face_ids": [16, 106]}, blocking=True)
        await coordinator._preview_task
    assert fetch.await_count == 2


async def test_two_preview_fetches_at_once_are_refused(hass, integration) -> None:
    coordinator = hass.data[DOMAIN][integration.entry_id]
    gate = asyncio.Event()

    async def slow(_hass, _face_id) -> bool:
        await gate.wait()
        return True

    with patch("custom_components.galaxy_fit3.face_store.async_fetch_preview", slow):
        await hass.services.async_call(DOMAIN, "fetch_face_previews", {"face_ids": [16]}, blocking=True)
        with pytest.raises(HomeAssistantError, match="already being fetched"):
            await hass.services.async_call(DOMAIN, "fetch_face_previews", {"face_ids": [106]}, blocking=True)
        gate.set()
        await coordinator._preview_task
