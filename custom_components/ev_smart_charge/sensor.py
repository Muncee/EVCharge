"""Sensor platform for EV Smart Charge."""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_POUND, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BINARY_SENSOR_CHARGE_NOW,
    CONF_BATTERY_CAPACITY,
    CONF_CHARGE_RATE,
    DOMAIN,
    REGION_CODES,
    SENSOR_CHARGE_END,
    SENSOR_CHARGE_START,
    SENSOR_CHEAPEST_TONIGHT,
    SENSOR_CURRENT_PRICE,
    SENSOR_DAILY_USAGE,
    SENSOR_ESTIMATED_COST,
    SENSOR_FORECAST_LOW,
    SENSOR_FORECAST_LOW_TIME,
    SENSOR_STRATEGY,
    STRATEGY_DESCRIPTIONS,
)
from .coordinator import EVSmartChargeCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EV Smart Charge sensors from a config entry."""
    coordinator: EVSmartChargeCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        EVStrategySensor(coordinator, entry),
        EVChargeStartSensor(coordinator, entry),
        EVChargeEndSensor(coordinator, entry),
        EVEstimatedCostSensor(coordinator, entry),
        EVCheapestTonightSensor(coordinator, entry),
        EVForecastLowSensor(coordinator, entry),
        EVForecastLowTimeSensor(coordinator, entry),
        EVCurrentPriceSensor(coordinator, entry),
        EVDailyUsageSensor(coordinator, entry),
    ]
    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    cfg = {**entry.data, **entry.options}
    region_code = cfg.get("region", "?")
    region_name = REGION_CODES.get(region_code, region_code)
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="AgilePredict / EV Smart Charge",
        model=f"UK DNO Region {region_code} — {region_name}",
        sw_version="1.0.0",
        configuration_url="https://agilepredict.com",
    )


class EVSmartChargeSensorBase(CoordinatorEntity, SensorEntity):
    """Base class for all EV Smart Charge sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EVSmartChargeCoordinator,
        entry: ConfigEntry,
        sensor_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{sensor_key}"
        self._attr_device_info = _device_info(entry)

    @property
    def _strategy(self):
        return self.coordinator.strategy


class EVStrategySensor(EVSmartChargeSensorBase):
    """Human-readable charging strategy recommendation."""

    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SENSOR_STRATEGY)
        self._attr_name = "Charging Strategy"

    @property
    def native_value(self) -> Optional[str]:
        s = self._strategy
        return STRATEGY_DESCRIPTIONS.get(s.strategy_type, s.strategy_type) if s else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._strategy
        if not s:
            return {}
        attrs: dict[str, Any] = {
            "strategy_type": s.strategy_type,
            "recommendation": s.recommendation,
            "target_soc": s.target_soc,
            "estimated_cost_gbp": round(s.estimated_cost_gbp, 4),
            "reasoning": s.reasoning,
        }
        if s.night_comparison:
            attrs["night_price_comparison"] = s.night_comparison
        if s.charge_window:
            attrs["window_start"] = s.charge_window.start.isoformat()
            attrs["window_end"] = s.charge_window.end.isoformat()
            attrs["window_avg_price_pence"] = round(s.charge_window.avg_price_pence, 2)
            attrs["window_energy_kwh"] = round(s.charge_window.energy_kwh, 2)
        return attrs


class EVChargeStartSensor(EVSmartChargeSensorBase):
    """Start time of the recommended charging window."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SENSOR_CHARGE_START)
        self._attr_name = "Charge Window Start"

    @property
    def native_value(self):
        s = self._strategy
        if s and s.charge_window:
            return s.charge_window.start
        return None


class EVChargeEndSensor(EVSmartChargeSensorBase):
    """End time of the recommended charging window."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SENSOR_CHARGE_END)
        self._attr_name = "Charge Window End"

    @property
    def native_value(self):
        s = self._strategy
        if s and s.charge_window:
            return s.charge_window.end
        return None


class EVEstimatedCostSensor(EVSmartChargeSensorBase):
    """Estimated cost of the recommended charging session."""

    _attr_native_unit_of_measurement = CURRENCY_POUND
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-gbp"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SENSOR_ESTIMATED_COST)
        self._attr_name = "Estimated Charge Cost"

    @property
    def native_value(self) -> Optional[float]:
        s = self._strategy
        return round(s.estimated_cost_gbp, 4) if s else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._strategy
        if not s:
            return {}
        return {
            "cost_pence": round(s.estimated_cost_pence, 2),
            "energy_kwh": round(s.charge_window.energy_kwh, 2) if s.charge_window else None,
            "avg_price_pence_per_kwh": round(s.charge_window.avg_price_pence, 2) if s.charge_window else None,
        }


class EVCheapestTonightSensor(EVSmartChargeSensorBase):
    """Cheapest single slot price tonight (p/kWh)."""

    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:lightning-bolt-circle"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SENSOR_CHEAPEST_TONIGHT)
        self._attr_name = "Cheapest Price Tonight"

    @property
    def native_value(self) -> Optional[float]:
        s = self._strategy
        return round(s.cheapest_tonight_pence, 2) if s and s.cheapest_tonight_pence is not None else None


class EVForecastLowSensor(EVSmartChargeSensorBase):
    """Lowest forecast price in the next 14 days (p/kWh)."""

    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:arrow-down-bold-circle"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SENSOR_FORECAST_LOW)
        self._attr_name = "Forecast Lowest Price (14 days)"

    @property
    def native_value(self) -> Optional[float]:
        s = self._strategy
        return round(s.forecast_cheapest_pence, 2) if s and s.forecast_cheapest_pence is not None else None


class EVForecastLowTimeSensor(EVSmartChargeSensorBase):
    """Timestamp of the cheapest forecast slot in the next 14 days."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SENSOR_FORECAST_LOW_TIME)
        self._attr_name = "Forecast Lowest Price Time"

    @property
    def native_value(self):
        s = self._strategy
        return s.forecast_cheapest_time if s else None


class EVCurrentPriceSensor(EVSmartChargeSensorBase):
    """Current half-hour Agile electricity price (p/kWh)."""

    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SENSOR_CURRENT_PRICE)
        self._attr_name = "Current Agile Price"

    @property
    def native_value(self) -> Optional[float]:
        price = self.coordinator.current_price
        return round(price, 2) if price is not None else None


class EVDailyUsageSensor(EVSmartChargeSensorBase):
    """Estimated daily energy usage based on SOC history (kWh)."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:car-electric"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, SENSOR_DAILY_USAGE)
        self._attr_name = "Estimated Daily Usage"

    @property
    def native_value(self) -> Optional[float]:
        return self.coordinator.estimated_daily_usage

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "soc_history_entries": self.coordinator.data.get("soc_history_len", 0)
            if self.coordinator.data
            else 0,
            "battery_capacity_kwh": self.coordinator.battery_capacity,
        }
