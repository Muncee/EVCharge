"""Sensor platform for EV Charge Optimizer."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    DATA_CURRENT_PRICE,
    DATA_SCHEDULE,
    DATA_NEXT_START,
    DATA_NEXT_END,
    DATA_SUMMARY,
    DATA_SCHEDULE_ACTIVE,
)
from .coordinator import EVChargeCoordinator

PENCE_PER_KWH = "p/kWh"

_DEVICE_INFO = {
    "identifiers": None,  # filled per-instance
    "name": "EV Charge Optimizer",
    "manufacturer": "AgilePredict",
    "model": "EV Charge Optimizer",
    "entry_type": "service",
}


def _device(entry_id: str) -> dict:
    return {
        "identifiers": {(DOMAIN, entry_id)},
        "name": "EV Charge Optimizer",
        "manufacturer": "AgilePredict",
        "model": "EV Charge Optimizer",
        "entry_type": "service",
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EVChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        CurrentPriceSensor(coordinator, entry),
        WeeklyScheduleSensor(coordinator, entry),
        NextChargeStartSensor(coordinator, entry),
        NextChargeEndSensor(coordinator, entry),
    ])


# ---------------------------------------------------------------------------

class CurrentPriceSensor(CoordinatorEntity[EVChargeCoordinator], SensorEntity):
    """Current Agile electricity price."""

    _attr_has_entity_name = True
    _attr_name = "Current Price"
    _attr_icon = "mdi:lightning-bolt"
    _attr_native_unit_of_measurement = PENCE_PER_KWH
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EVChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_current_price"
        self._attr_device_info = _device(entry.entry_id)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data and self.coordinator.data.get(DATA_CURRENT_PRICE)


class WeeklyScheduleSensor(CoordinatorEntity[EVChargeCoordinator], SensorEntity):
    """Human-readable weekly charge schedule.

    State: short summary of next session.
    Attributes: full list of planned sessions.
    """

    _attr_has_entity_name = True
    _attr_name = "Weekly Charge Schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: EVChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_weekly_schedule"
        self._attr_device_info = _device(entry.entry_id)

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data and self.coordinator.data.get(DATA_SUMMARY)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        schedule = self.coordinator.data.get(DATA_SCHEDULE, [])
        now = dt_util.utcnow()
        sessions = []
        for s in schedule:
            start_local = dt_util.as_local(s["start"])
            end_local = dt_util.as_local(s["end"])
            diff_days = (start_local.date() - dt_util.as_local(now).date()).days
            if diff_days == 0:
                day_label = "Today"
            elif diff_days == 1:
                day_label = "Tomorrow"
            else:
                day_label = start_local.strftime("%A %-d %b")
            sessions.append({
                "day": day_label,
                "start": start_local.strftime("%H:%M"),
                "end": end_local.strftime("%H:%M"),
                "avg_price_p_kwh": s["avg_price"],
                "duration_hours": s["duration_hours"],
                "target_pct": s["target_pct"],
                "prices_predicted": s.get("predicted", True),
                "status": "active" if s["start"] <= now < s["end"] else "upcoming",
            })
        return {
            "sessions": sessions,
            "charging_now": self.coordinator.data.get(DATA_SCHEDULE_ACTIVE, False),
        }


class NextChargeStartSensor(CoordinatorEntity[EVChargeCoordinator], SensorEntity):
    """Timestamp of the next planned charge window start (for automations)."""

    _attr_has_entity_name = True
    _attr_name = "Next Charge Start"
    _attr_icon = "mdi:play-circle-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: EVChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_next_charge_start"
        self._attr_device_info = _device(entry.entry_id)

    @property
    def native_value(self):
        return self.coordinator.data and self.coordinator.data.get(DATA_NEXT_START)


class NextChargeEndSensor(CoordinatorEntity[EVChargeCoordinator], SensorEntity):
    """Timestamp of the next planned charge window end (for automations)."""

    _attr_has_entity_name = True
    _attr_name = "Next Charge End"
    _attr_icon = "mdi:stop-circle-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: EVChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_next_charge_end"
        self._attr_device_info = _device(entry.entry_id)

    @property
    def native_value(self):
        return self.coordinator.data and self.coordinator.data.get(DATA_NEXT_END)
