"""Number platform for EV Charge Optimizer - runtime adjustable parameters."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    CONF_TARGET_CHARGE_PCT,
    CONF_DAILY_USAGE,
)
from .coordinator import EVChargeCoordinator

_NUMBER_DEFS = (
    {
        "key": "target_charge_pct",
        "name": "Target Charge Level",
        "icon": "mdi:battery-high",
        "unit": PERCENTAGE,
        "min": 50.0,
        "max": 100.0,
        "step": 1.0,
        "config_key": CONF_TARGET_CHARGE_PCT,
        "default": 80.0,
    },
    {
        "key": "daily_usage_kwh",
        "name": "Estimated Daily Usage",
        "icon": "mdi:lightning-bolt",
        "unit": "kWh",
        "min": 1.0,
        "max": 100.0,
        "step": 0.5,
        "config_key": CONF_DAILY_USAGE,
        "default": 15.0,
    },
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EVChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EVChargeNumber(coordinator, entry, defn) for defn in _NUMBER_DEFS
    )


class EVChargeNumber(RestoreEntity, NumberEntity):
    """Adjustable number entity that overrides coordinator config at runtime."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: EVChargeCoordinator,
        entry: ConfigEntry,
        defn: dict,
    ) -> None:
        self._coordinator = coordinator
        self._defn = defn
        self._attr_unique_id = f"{entry.entry_id}_{defn['key']}"
        self._attr_name = defn["name"]
        self._attr_icon = defn["icon"]
        self._attr_native_unit_of_measurement = defn["unit"]
        self._attr_native_min_value = defn["min"]
        self._attr_native_max_value = defn["max"]
        self._attr_native_step = defn["step"]
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EV Charge Optimizer",
            "manufacturer": "AgilePredict",
            "model": "EV Charge Optimizer",
            "entry_type": "service",
        }
        # Initialise from merged config+options; will be overwritten by restore
        config = {**entry.data, **entry.options}
        self._attr_native_value = float(config.get(defn["config_key"], defn["default"]))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable", None):
            try:
                self._attr_native_value = float(last.state)
            except (ValueError, TypeError):
                pass
        # Push initial value into coordinator overrides
        self._coordinator.overrides[self._defn["config_key"]] = self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._coordinator.overrides[self._defn["config_key"]] = value
        self.async_write_ha_state()
        await self._coordinator.async_request_refresh()
