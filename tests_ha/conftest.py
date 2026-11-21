"""Fixtures for the Home Assistant integration tests: the real core, a fake watch."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.galaxy_fit3 import coordinator as coordinator_mod
from custom_components.galaxy_fit3.client import GalaxyFit3Client, WatchStatus
from custom_components.galaxy_fit3.const import CONF_ADDRESS, CONF_NAME, DOMAIN
from custom_components.galaxy_fit3.sap.appframe import WatchFace
from custom_components.galaxy_fit3.store_catalog import StoreFace

ADDRESS = "AA:BB:CC:DD:EE:FF"
PACKAGE = "com.samsung.fit3watchface.sm_r390_{:04d}"


def watch_faces(active: int) -> list[WatchFace]:
    """The faces on the watch, with ``active`` shown."""
    return [WatchFace(face_id=i, package=PACKAGE.format(i), name=f"wf_name-{i:05d}", group=1, active=i == active)
            for i in (106, 16)]


STORE = {
    106: StoreFace(face_id=106, name="Fitness pro 3", styles=1, version=40000, preview="https://img.example/106.png"),
    16: StoreFace(face_id=16, name="Progress gauge 1", styles=1, version=40000, preview="https://img.example/16.png"),
    79: StoreFace(face_id=79, name="Step streak", styles=1, version=40000, preview="https://img.example/79.png"),
}


@pytest.fixture
def client() -> AsyncMock:
    """A stand-in for the watch client that only has methods the real one has (autospec)."""
    fake = create_autospec(GalaxyFit3Client, instance=True)
    fake.is_connected = False
    fake.read_status.return_value = WatchStatus(battery_level=64, heart_rate=75, steps=1234, calories=123.456, distance=2345.67)
    fake.set_time_rtc.return_value = True
    fake.list_watch_faces.return_value = watch_faces(active=106)
    fake.set_watch_face.return_value = watch_faces(active=16)
    fake.heart_rate_records.return_value = {}
    fake.heart_rate_time = None
    fake.take_dirty_heart_rate_hours.return_value = set()
    fake.set_time.return_value = True

    async def connect(*_args, **_kwargs) -> None:
        fake.is_connected = True

    async def disconnect(*_args, **_kwargs) -> None:
        fake.is_connected = False

    fake.connect.side_effect = connect
    fake.disconnect.side_effect = disconnect
    return fake


@pytest.fixture
def reachable() -> bool:
    """Whether the (stubbed) Bluetooth link to the watch can be opened; override per test."""
    return True


@pytest.fixture
async def integration(
    recorder_mock, enable_bluetooth, enable_custom_integrations, hass, client, reachable
) -> AsyncGenerator[MockConfigEntry]:
    """A set-up config entry running against the fake watch.

    ``recorder_mock`` must come before ``hass``: the plugin builds the recorder database first.
    ``enable_custom_integrations`` lets Home Assistant load custom_components/galaxy_fit3 from this repository.
    """

    async def open_link(self) -> bool:
        if not reachable:
            return False
        await client.connect()
        return True

    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title="Galaxy Fit3",
                            data={CONF_ADDRESS: ADDRESS, CONF_NAME: "Galaxy Fit3"})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.galaxy_fit3.ble_support.build_client", return_value=client),
        patch.object(coordinator_mod.Fit3Coordinator, "_open_link", open_link),
        patch("custom_components.galaxy_fit3.face_store.async_list_faces", AsyncMock(return_value=STORE)),
        patch("custom_components.galaxy_fit3.face_store.async_local_previews", AsyncMock(return_value=set())),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
