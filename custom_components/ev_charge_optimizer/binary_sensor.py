"""Binary sensor platform for EV Charge Optimizer."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DATA_CHARGE_NOW_BINARY,
    DATA_IS_CHEAP_NOW,
    DATA_IS_EXPENSIVE_NOW,
)
from .coordinator import EVChargeCoordinator


@dataclass(frozen=True, kw_only=True)
class EVBinarySensorDescription(BinarySensorEntityDescription):
    data_key: str = ""


BINARY_SENSOR_DESCRIPTIONS: tuple[EVBinarySensorDescription, ...] = (
    EVBinarySensorDescription(
        key="charge_now",
        name="Charge EV Now",
        icon="mdi:car-electric",
        device_class=BinarySensorDeviceClass.RUNNING,
        data_key=DATA_CHARGE_NOW_BINARY,
    ),
    EVBinarySensorDescription(
        key="cheap_period",
        name="Cheap Electricity Period",
        icon="mdi:lightning-bolt-circle",
        data_key=DATA_IS_CHEAP_NOW,
    ),
    EVBinarySensorDescription(
        key="expensive_period",
        name="Expensive Electricity Period",
        icon="mdi:alert-circle",
        data_key=DATA_IS_EXPENSIVE_NOW,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EVChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EVChargeBinarySensor(coordinator, entry, desc)
        for desc in BINARY_SENSOR_DESCRIPTIONS
    )


class EVChargeBinarySensor(CoordinatorEntity[EVChargeCoordinator], BinarySensorEntity):
    """Binary sensor that reads a boolean from the EV Charge coordinator."""

    entity_description: EVBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EVChargeCoordinator,
        entry: ConfigEntry,
        description: EVBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EV Charge Optimizer",
            "manufacturer": "AgilePredict",
            "model": "EV Charge Optimizer",
            "entry_type": "service",
        }

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return bool(self.coordinator.data.get(self.entity_description.data_key, False))
