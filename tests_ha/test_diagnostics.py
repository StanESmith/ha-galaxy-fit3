"""The diagnostics download: useful, and never the watch's Bluetooth address."""

from __future__ import annotations

import json

import pytest

from custom_components.galaxy_fit3.const import DOMAIN
from custom_components.galaxy_fit3.diagnostics import async_get_config_entry_diagnostics

from .conftest import ADDRESS

pytestmark = pytest.mark.usefixtures("integration")


async def test_the_report_describes_the_integration_without_the_address(hass, integration) -> None:
    report = await async_get_config_entry_diagnostics(hass, integration)
    assert report["integration_version"]
    assert report["link"]["status"] == "connected"
    assert report["readings"]["battery_level"] == 64 and report["readings"]["calories"] == 123.456
    assert [face["name"] for face in report["watch_faces"]] == ["Fitness pro 3", "Progress gauge 1"]
    assert report["store"] == {"faces": 3, "previews_saved": 0}
    assert report["entry"]["data"]["address"] == "**REDACTED**"
    assert ADDRESS not in json.dumps(report) and ADDRESS.replace(":", "_") not in json.dumps(report)


async def test_an_address_inside_an_error_text_is_scrubbed_too(hass, integration) -> None:
    coordinator = hass.data[DOMAIN][integration.entry_id]
    coordinator.install_info = {"error": f"Device {ADDRESS} not found (dev_{ADDRESS.replace(':', '_')})"}
    report = await async_get_config_entry_diagnostics(hass, integration)
    assert report["install"]["info"]["error"] == "Device **REDACTED** not found (dev_**REDACTED**)"
