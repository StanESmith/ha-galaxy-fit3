"""Select entity: pick the active watch face from the catalogue on the watch."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
    async_add_entities([Fit3WatchFaceSelect(coordinator), Fit3StoreFaceSelect(coordinator)])


class Fit3WatchFaceSelect(CoordinatorEntity[Fit3Coordinator], SelectEntity):
    """A dropdown of the watch's installed faces; selecting one switches the watch."""

    _attr_has_entity_name = True
    _attr_translation_key = "watch_face"
    _attr_icon = "mdi:watch-variant"

    def __init__(self, coordinator: Fit3Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_watch_face"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={("bluetooth", coordinator.address)},
            manufacturer="Samsung",
            model="Galaxy Fit3",
            name=coordinator.entry.title,
        )

    @property
    def options(self) -> list[str]:
        return [self.coordinator.face_label(face.face_id) for face in self.coordinator.watch_faces]

    @property
    def current_option(self) -> str | None:
        for face in self.coordinator.watch_faces:
            if face.face_id == self.coordinator.current_face_id:
                return self.coordinator.face_label(face.face_id)
        return None

    @property
    def entity_picture(self) -> str | None:
        return self.coordinator.face_preview(self.coordinator.current_face_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "face_id": self.coordinator.current_face_id,
            "face_ids": [face.face_id for face in self.coordinator.watch_faces],  # the dashboard shows these
        }

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_select_face(option)


class Fit3StoreFaceSelect(CoordinatorEntity[Fit3Coordinator], SelectEntity):
    """Every face in Samsung's store; the "Install watch face" button installs the one picked here."""

    _attr_has_entity_name = True
    _attr_translation_key = "store_face"
    _attr_icon = "mdi:storefront-outline"

    def __init__(self, coordinator: Fit3Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_store_face"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.address)})

    def _sorted(self) -> list:
        return sorted(self.coordinator.store_faces.values(), key=lambda f: f.name.lower())

    @property
    def available(self) -> bool:
        return bool(self.coordinator.store_faces)  # the store catalogue, not the watch link

    @property
    def options(self) -> list[str]:
        return [f.name for f in self._sorted()]

    @property
    def current_option(self) -> str | None:
        sel = self.coordinator.store_selection
        return self.coordinator.face_label(sel) if sel in self.coordinator.store_faces else None

    @property
    def entity_picture(self) -> str | None:
        return self.coordinator.face_preview(self.coordinator.store_selection)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        sel = self.coordinator.store_selection
        return {
            "face_id": sel,
            "on_watch": any(f.face_id == sel for f in self.coordinator.watch_faces),
            "store_faces": len(self.coordinator.store_faces),
        }

    async def async_select_option(self, option: str) -> None:
        face = next((f for f in self._sorted() if f.name == option), None)
        if face is None:
            raise ValueError(f"unknown store face: {option}")
        self.coordinator.store_selection = face.face_id
        self.async_write_ha_state()
