"""The config and options flows, driven through the real flow manager."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.galaxy_fit3.config_flow import CONF_SKIP_PAIRING
from custom_components.galaxy_fit3.const import CONF_ADDRESS, CONF_IS_24H, CONF_NAME, DOMAIN
from custom_components.galaxy_fit3.sap.constants import PIPE_SERVICE

from .conftest import ADDRESS


@pytest.fixture(autouse=True)
def _no_entry_setup(recorder_mock, enable_bluetooth, enable_custom_integrations):
    """Flow tests stop at the entry; setting it up is covered by test_integration."""
    with patch("custom_components.galaxy_fit3.async_setup_entry", return_value=True):
        yield


def _discovery() -> BluetoothServiceInfoBleak:
    uuids = [str(PIPE_SERVICE)]
    device = BLEDevice(ADDRESS, "Galaxy Fit3 (ABCD)", {})
    advertisement = AdvertisementData(
        local_name="Galaxy Fit3 (ABCD)", manufacturer_data={}, service_data={}, service_uuids=uuids,
        rssi=-60, tx_power=None, platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name="Galaxy Fit3 (ABCD)", address=ADDRESS, rssi=-60, manufacturer_data={}, service_data={},
        service_uuids=uuids, source="local", device=device, advertisement=advertisement, connectable=True,
        time=0, tx_power=None,
    )


async def test_manual_entry_can_skip_pairing(hass) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS.lower(), CONF_NAME: "My watch", CONF_SKIP_PAIRING: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My watch"
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_NAME: "My watch"}


async def test_a_configured_watch_is_not_added_twice(hass) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS, CONF_SKIP_PAIRING: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_discovery_asks_to_confirm(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=_discovery()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"name": "Galaxy Fit3 (ABCD)"}


async def test_discovery_of_a_configured_watch_aborts(hass) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=_discovery()
    )
    assert result["type"] is FlowResultType.ABORT


async def test_options_flow_sets_the_clock_format(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_IS_24H: True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_IS_24H: True}
