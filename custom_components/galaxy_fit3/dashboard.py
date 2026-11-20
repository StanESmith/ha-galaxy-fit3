"""The dashboard side of the integration: a view strategy and the data it builds its cards from.

``frontend/galaxy-fit3.js`` is a Lovelace *view strategy*: a dashboard view written as

    - title: Galaxy Fit3
      path: galaxy-fit3
      strategy:
        type: custom:galaxy-fit3

is generated when the dashboard loads, so the grid of watch faces follows the Samsung store and what is
on the watch with nothing to maintain. It asks for its data with the ``galaxy_fit3/faces`` WebSocket
command below, which also names the entities (so it never has to guess an entity id).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STATIC_URL = f"/{DOMAIN}_static"
JS_FILE = "galaxy-fit3.js"
WS_FACES = f"{DOMAIN}/faces"

# (key in the answer, platform, suffix of the entity's unique id after the watch address)
_ENTITIES = (
    ("battery", "sensor", "battery_level"),
    ("heart_rate", "sensor", "heart_rate"),
    ("steps", "sensor", "steps"),
    ("watch_face", "select", "watch_face"),
    ("store_face", "select", "store_face"),
    ("install", "sensor", "face_install"),
    ("install_button", "button", "install_face"),
)


async def async_setup_frontend(hass: HomeAssistant) -> None:
    """Serve the strategy's JavaScript, load it into the frontend and answer its data requests."""
    websocket_api.async_register_command(hass, ws_faces)
    http = getattr(hass, "http", None)
    if http is None:
        _LOGGER.debug("no http component: the dashboard strategy is not served")
        return
    url = f"{STATIC_URL}/{JS_FILE}"
    await http.async_register_static_paths(
        [StaticPathConfig(url, str(Path(__file__).parent / "frontend" / JS_FILE), cache_headers=False)]
    )
    if "frontend" not in hass.config.components:
        return  # nothing to load it into (a headless setup)
    from homeassistant.components import frontend  # noqa: PLC0415 - only importable where the frontend is installed

    version = (await async_get_integration(hass, DOMAIN)).version
    frontend.add_extra_js_url(hass, f"{url}?v={version}")  # the version in the URL refreshes the browser's copy


def faces_payload(hass: HomeAssistant, coordinator: Any) -> dict[str, Any]:
    """What the strategy needs for one watch: entity ids and every face it can show, by name."""
    registry = er.async_get(hass)
    entities = {
        key: registry.async_get_entity_id(platform, DOMAIN, f"{coordinator.address}_{suffix}")
        for key, platform, suffix in _ENTITIES
    }
    names = {face_id: face.name for face_id, face in coordinator.store_faces.items() if face.name}
    for face in coordinator.watch_faces:  # a face the store does not list (or the catalogue is not loaded yet)
        names.setdefault(face.face_id, coordinator.face_label(face.face_id))
    faces = [
        {"id": face_id, "name": name, "preview": coordinator.face_preview(face_id)}
        for face_id, name in sorted(names.items(), key=lambda item: item[1].lower())
    ]
    return {"entry_id": coordinator.entry.entry_id, "title": coordinator.entry.title, "entities": entities, "faces": faces}


@websocket_api.websocket_command({vol.Required("type"): WS_FACES, vol.Optional("entry_id"): str})
@callback
def ws_faces(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The faces and entities of a watch (the first set-up one unless ``entry_id`` names it)."""
    coordinators: dict[str, Any] = hass.data.get(DOMAIN, {})
    wanted = msg.get("entry_id")
    if wanted is not None and wanted not in coordinators:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"no Galaxy Fit3 entry {wanted}")
        return
    if not coordinators:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "no Galaxy Fit3 is set up")
        return
    coordinator = coordinators[wanted] if wanted else next(iter(coordinators.values()))
    connection.send_result(msg["id"], faces_payload(hass, coordinator))
