"""Sensor platform for EV Charge Optimizer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfTime,
)

CURRENCY_POUND = "GBP"
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DATA_RECOMMENDATION,
    DATA_REASON,
    DATA_CURRENT_PRICE,
    DATA_AVG_PRICE,
    DATA_CHEAP_THRESHOLD,
    DATA_EXPENSIVE_THRESHOLD,
    DATA_NEXT_CHEAP_WINDOW,
    DATA_NEXT_CHEAP_PRICE,
    DATA_HOURS_UNTIL_CHEAP,
    DATA_CURRENT_BATTERY_PCT,
    DATA_KWH_NEEDED,
    DATA_HOURS_TO_CHARGE,
    DATA_COST_NOW,
    DATA_COST_CHEAPEST,
    DATA_ESTIMATED_SAVINGS,
    DATA_DAYS_OF_RANGE,
    DATA_PCT_EXPENSIVE_7DAYS,
    DATA_CHEAPEST_TODAY_PRICE,
    DATA_CHEAPEST_TODAY_START,
    DATA_CHEAPEST_UPCOMING_PRICE,
    DATA_CHEAPEST_UPCOMING_START,
    DATA_LONG_EXPENSIVE_DAYS,
)
from .coordinator import EVChargeCoordinator

# Unit for electricity price in pence per kWh
PENCE_PER_KWH = "p/kWh"


@dataclass(frozen=True, kw_only=True)
class EVSensorDescription(SensorEntityDescription):
    """Extended description for EV Charge sensors."""
    data_key: str = ""


SENSOR_DESCRIPTIONS: tuple[EVSensorDescription, ...] = (
    EVSensorDescription(
        key="recommendation",
        name="Charging Recommendation",
        icon="mdi:car-electric",
        data_key=DATA_RECOMMENDATION,
    ),
    EVSensorDescription(
        key="recommendation_reason",
        name="Recommendation Reason",
        icon="mdi:text-box-outline",
        data_key=DATA_REASON,
    ),
    EVSensorDescription(
        key="current_price",
        name="Current Electricity Price",
        native_unit_of_measurement=PENCE_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:currency-gbp",
        data_key=DATA_CURRENT_PRICE,
    ),
    EVSensorDescription(
        key="average_price_14day",
        name="14-Day Average Price",
        native_unit_of_measurement=PENCE_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-line",
        data_key=DATA_AVG_PRICE,
    ),
    EVSensorDescription(
        key="cheap_threshold",
        name="Cheap Price Threshold",
        native_unit_of_measurement=PENCE_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:arrow-down-circle",
        data_key=DATA_CHEAP_THRESHOLD,
    ),
    EVSensorDescription(
        key="expensive_threshold",
        name="Expensive Price Threshold",
        native_unit_of_measurement=PENCE_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:arrow-up-circle",
        data_key=DATA_EXPENSIVE_THRESHOLD,
    ),
    EVSensorDescription(
        key="next_cheap_window",
        name="Next Cheap Window",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-fast",
        data_key=DATA_NEXT_CHEAP_WINDOW,
    ),
    EVSensorDescription(
        key="next_cheap_price",
        name="Next Cheap Window Price",
        native_unit_of_measurement=PENCE_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        data_key=DATA_NEXT_CHEAP_PRICE,
    ),
    EVSensorDescription(
        key="hours_until_cheap",
        name="Hours Until Cheap Window",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-sand",
        data_key=DATA_HOURS_UNTIL_CHEAP,
    ),
    EVSensorDescription(
        key="battery_pct",
        name="EV Battery Level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.BATTERY,
        icon="mdi:car-battery",
        data_key=DATA_CURRENT_BATTERY_PCT,
    ),
    EVSensorDescription(
        key="kwh_needed",
        name="Energy Needed to Charge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.ENERGY,
        icon="mdi:battery-charging",
        data_key=DATA_KWH_NEEDED,
    ),
    EVSensorDescription(
        key="hours_to_charge",
        name="Hours to Charge",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        data_key=DATA_HOURS_TO_CHARGE,
    ),
    EVSensorDescription(
        key="cost_to_charge_now",
        name="Cost to Charge Now",
        native_unit_of_measurement=CURRENCY_POUND,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:cash",
        data_key=DATA_COST_NOW,
    ),
    EVSensorDescription(
        key="cost_at_cheapest_today",
        name="Cost at Cheapest Today",
        native_unit_of_measurement=CURRENCY_POUND,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:cash-minus",
        data_key=DATA_COST_CHEAPEST,
    ),
    EVSensorDescription(
        key="estimated_savings",
        name="Estimated Savings (Wait vs Now)",
        native_unit_of_measurement=CURRENCY_POUND,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:piggy-bank",
        data_key=DATA_ESTIMATED_SAVINGS,
    ),
    EVSensorDescription(
        key="days_of_battery_range",
        name="Days of Battery Range",
        native_unit_of_measurement="days",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:road-variant",
        data_key=DATA_DAYS_OF_RANGE,
    ),
    EVSensorDescription(
        key="pct_expensive_next_7days",
        name="Expensive Period (Next 7 Days)",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-alert",
        data_key=DATA_PCT_EXPENSIVE_7DAYS,
    ),
    EVSensorDescription(
        key="cheapest_price_today",
        name="Cheapest Price Today",
        native_unit_of_measurement=PENCE_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:sale",
        data_key=DATA_CHEAPEST_TODAY_PRICE,
    ),
    EVSensorDescription(
        key="cheapest_window_today",
        name="Cheapest Window Today",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-check",
        data_key=DATA_CHEAPEST_TODAY_START,
    ),
    EVSensorDescription(
        key="cheapest_upcoming_price",
        name="Cheapest Upcoming Price",
        native_unit_of_measurement=PENCE_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:sale-outline",
        data_key=DATA_CHEAPEST_UPCOMING_PRICE,
    ),
    EVSensorDescription(
        key="cheapest_upcoming_window",
        name="Cheapest Upcoming Window",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-star-four-points",
        data_key=DATA_CHEAPEST_UPCOMING_START,
    ),
    EVSensorDescription(
        key="long_expensive_days",
        name="Longest Upcoming Expensive Period",
        native_unit_of_measurement="days",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-sunny-alert",
        data_key=DATA_LONG_EXPENSIVE_DAYS,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EVChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EVChargeSensor(coordinator, entry, desc)
        for desc in SENSOR_DESCRIPTIONS
    )


class EVChargeSensor(CoordinatorEntity[EVChargeCoordinator], SensorEntity):
    """A sensor entity that reads from the EV Charge coordinator."""

    entity_description: EVSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EVChargeCoordinator,
        entry: ConfigEntry,
        description: EVSensorDescription,
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
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.entity_description.data_key)
        # datetime values are returned as-is (HA handles them for TIMESTAMP devices)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose extra attributes on the recommendation sensor."""
        if self.entity_description.key != "recommendation":
            return {}
        data = self.coordinator.data or {}
        return {
            "reason": data.get(DATA_REASON),
            "current_price_pence": data.get(DATA_CURRENT_PRICE),
            "cheap_threshold_pence": data.get(DATA_CHEAP_THRESHOLD),
            "expensive_threshold_pence": data.get(DATA_EXPENSIVE_THRESHOLD),
            "pct_expensive_7days": data.get(DATA_PCT_EXPENSIVE_7DAYS),
            "longest_expensive_run_days": data.get(DATA_LONG_EXPENSIVE_DAYS),
            "battery_range_days": data.get(DATA_DAYS_OF_RANGE),
        }
