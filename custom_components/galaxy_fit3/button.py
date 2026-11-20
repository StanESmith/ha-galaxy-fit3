"""Button: install the watch face picked in the store select."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import Fit3Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: Fit3Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([Fit3InstallFaceButton(coordinator)])


class Fit3InstallFaceButton(CoordinatorEntity[Fit3Coordinator], ButtonEntity):
    """Downloads the selected store face and installs it on the watch (about a minute; runs in the background)."""

    _attr_has_entity_name = True
    _attr_translation_key = "install_face"
    _attr_icon = "mdi:watch-import"

    def __init__(self, coordinator: Fit3Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_install_face"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.address)})

    @property
    def available(self) -> bool:
        return True  # the install opens the link itself

    async def async_press(self) -> None:
        self.coordinator.async_start_install()
