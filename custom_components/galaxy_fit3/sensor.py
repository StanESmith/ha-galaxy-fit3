"""Sensor entities: battery level, heart rate, step count.

Values populate once the capture-gated SAP session is operational; until then they report
``None`` (unavailable) and the device carries an ``status`` attribute explaining why.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import WatchStatus
from .const import DOMAIN
from .coordinator import Fit3Coordinator


@dataclass(frozen=True, kw_only=True)
class Fit3SensorDescription(SensorEntityDescription):
    """Describes a Fit3 sensor and how to read it from WatchStatus."""

    value_fn: Callable[[WatchStatus], int | float | None]


SENSORS: tuple[Fit3SensorDescription, ...] = (
    Fit3SensorDescription(
        key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery_level,
    ),
    Fit3SensorDescription(
        key="heart_rate",
        translation_key="heart_rate",
        native_unit_of_measurement="bpm",
        icon="mdi:heart-pulse",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.heart_rate,
    ),
    Fit3SensorDescription(
        key="steps",
        translation_key="steps",
        native_unit_of_measurement="steps",
        icon="mdi:shoe-print",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: s.steps,
    ),
    Fit3SensorDescription(
        key="calories",
        translation_key="calories",
        native_unit_of_measurement="kcal",
        icon="mdi:fire",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_fn=lambda s: None if s.calories is None else round(s.calories, 1),
    ),
    Fit3SensorDescription(
        key="distance",
        translation_key="distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: None if s.distance is None else round(s.distance, 1),
    ),
    # The watch has not sent a resting heart rate in a week of data, so this stays off until you want it.
    Fit3SensorDescription(
        key="resting_heart_rate",
        translation_key="resting_heart_rate",
        native_unit_of_measurement="bpm",
        icon="mdi:heart-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.resting_heart_rate,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: Fit3Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([*(Fit3Sensor(coordinator, desc) for desc in SENSORS), Fit3FaceInstallSensor(coordinator)])


class Fit3Sensor(CoordinatorEntity[Fit3Coordinator], SensorEntity):
    """A single Fit3 sensor backed by the coordinator."""

    entity_description: Fit3SensorDescription
    _attr_has_entity_name = True

    def __init__(self, coordinator: Fit3Coordinator, description: Fit3SensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={("bluetooth", coordinator.address)},
            manufacturer="Samsung",
            model="Galaxy Fit3",
            name=coordinator.entry.title,
        )

    @property
    def suggested_unit_of_measurement(self) -> str | None:
        """The distance is stored in metres; a new sensor is first shown in the unit the watch uses (km or miles).

        Home Assistant only applies this when the entity is first registered, so a unit chosen on the entity
        afterwards is kept.
        """
        if self.entity_description.key == "distance":
            return UnitOfLength.MILES if self.coordinator.distance_unit() == "mi" else UnitOfLength.KILOMETERS
        return super().suggested_unit_of_measurement

    @property
    def native_value(self) -> int | None:
        # The first watch connection runs in the background, so there is no data yet at setup.
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        last_seen = self.coordinator.last_seen
        attrs: dict[str, object] = {
            "status": self.coordinator.status,
            "last_seen": last_seen.isoformat() if last_seen else None,  # values are from this poll
        }
        if self.entity_description.key == "heart_rate":
            attrs.update(self.coordinator.heart_rate_info)
        return attrs


class Fit3FaceInstallSensor(CoordinatorEntity[Fit3Coordinator], SensorEntity):
    """Where the last watch-face install got to: idle, downloading, sending (with progress), installed or failed."""

    _attr_has_entity_name = True
    _attr_translation_key = "face_install"
    _attr_icon = "mdi:progress-download"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "downloading", "sending", "installed", "failed"]

    def __init__(self, coordinator: Fit3Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_face_install"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.address)})

    @property
    def available(self) -> bool:
        return True  # reports the install, not the watch link

    @property
    def native_value(self) -> str:
        return self.coordinator.install_state

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return dict(self.coordinator.install_info)
