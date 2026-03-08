"""Binary sensor platform for EV Smart Charge."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BINARY_SENSOR_CHARGE_NOW,
    DOMAIN,
    REGION_CODES,
)
from .coordinator import EVSmartChargeCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the EV Smart Charge binary sensor."""
    coordinator: EVSmartChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EVChargeNowBinarySensor(coordinator, entry)])


class EVChargeNowBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """
    Binary sensor: ON when we are inside the recommended charging window.

    Use this entity to trigger your EV charger switch in automations.
    Example automation:
      trigger: state of binary_sensor.ev_smart_charge_charge_now → 'on'
      action: switch.turn_on <charger_switch>
    """

    _attr_has_entity_name = True
    _attr_name = "Charge Now"
    _attr_device_class = BinarySensorDeviceClass.PLUG
    _attr_icon = "mdi:ev-plug-type2"

    def __init__(
        self,
        coordinator: EVSmartChargeCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{BINARY_SENSOR_CHARGE_NOW}"
        region_code = entry.data.get("region", "?")
        region_name = REGION_CODES.get(region_code, region_code)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="AgilePredict / EV Smart Charge",
            model=f"UK DNO Region {region_code} — {region_name}",
            sw_version="1.0.0",
            configuration_url="https://agilepredict.com",
        )

    @property
    def is_on(self) -> bool:
        """Return True when we are inside the optimal charging window."""
        return self.coordinator.should_charge_now

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.coordinator.strategy
        attrs: dict[str, Any] = {}
        if s:
            attrs["strategy"] = s.strategy_type
            attrs["recommendation"] = s.recommendation
            if s.charge_window:
                attrs["window_start"] = s.charge_window.start.isoformat()
                attrs["window_end"] = s.charge_window.end.isoformat()
                attrs["window_avg_price_pence"] = round(s.charge_window.avg_price_pence, 2)
        return attrs
