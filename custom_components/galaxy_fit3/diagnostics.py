"""Diagnostics to attach to a bug report. The watch's Bluetooth address is left out of everything."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CONF_ADDRESS, DOMAIN

TO_REDACT = {CONF_ADDRESS, "address", "title", "name"}
_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}[:_-]){5}[0-9A-Fa-f]{2}")


def _scrub(value: Any) -> Any:
    """Blank out anything shaped like a Bluetooth address, wherever it turns up (an error text, say)."""
    if isinstance(value, str):
        return _MAC.sub("**REDACTED**", value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_scrub(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The integration's state: link, readings, faces and install, with no Bluetooth address."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    status = coordinator.data
    integration = await async_get_integration(hass, DOMAIN)
    report = {
        "integration_version": integration.version,
        "entry": async_redact_data(
            {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT
        ),
        "link": {
            "status": coordinator.status,
            "last_seen": coordinator.last_seen.isoformat() if coordinator.last_seen else None,
            "update_interval_s": coordinator.update_interval.total_seconds() if coordinator.update_interval else None,
            "connect_failures_in_a_row": coordinator._connect_fails,  # noqa: SLF001 - diagnostics read the counters
            "route": coordinator._route.policy.describe(),  # noqa: SLF001
            "connected": coordinator.client.is_connected,
        },
        "readings": {
            "battery_level": getattr(status, "battery_level", None),
            "heart_rate": getattr(status, "heart_rate", None),
            "steps": getattr(status, "steps", None),
            "calories": getattr(status, "calories", None),
            "distance": getattr(status, "distance", None),
            "resting_heart_rate": getattr(status, "resting_heart_rate", None),
        },
        "heart_rate_history": coordinator.heart_rate_info,
        "watch_faces": [
            {"id": face.face_id, "name": coordinator.face_label(face.face_id), "active": face.active}
            for face in coordinator.watch_faces
        ],
        "store": {"faces": len(coordinator.store_faces), "previews_saved": len(coordinator.local_previews)},
        "install": {"state": coordinator.install_state, "info": coordinator.install_info},
    }
    return _scrub(report)
