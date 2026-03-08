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
    DATA_SCHEDULE_SESSIONS,
    DATA_SCHEDULE_ACTIVE,
    DATA_NEXT_START,
    DATA_NEXT_END,
    DATA_SUMMARY,
)
from .coordinator import EVChargeCoordinator

PENCE_PER_KWH = "p/kWh"


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
    """Current Agile electricity price in p/kWh."""

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
    """Weekly charge schedule — readable summary.

    State: one-line description of what's happening / what's next.
    Attributes: full list of sessions for the week, each showing which
                individual slots were chosen and their prices.
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
        sessions = self.coordinator.data.get(DATA_SCHEDULE_SESSIONS, [])
        now = dt_util.utcnow()
        out = []
        for s in sessions:
            start_local = dt_util.as_local(s["start"])
            end_local = dt_util.as_local(s["end"])
            diff = (start_local.date() - dt_util.as_local(now).date()).days
            day = "Today" if diff == 0 else "Tomorrow" if diff == 1 else start_local.strftime("%A %-d %b")
            if s["start"] <= now < s["end"]:
                status = "active"
            elif s["start"] > now:
                status = "upcoming"
            else:
                status = "past"
            out.append({
                "day": day,
                "start": start_local.strftime("%H:%M"),
                "end": end_local.strftime("%H:%M"),
                "slots": s["n_slots"],
                "duration_hours": round(s["n_slots"] * 0.5, 1),
                "avg_price_p_kwh": s["avg_price"],
                "cheapest_slot_p_kwh": s["min_price"],
                "most_expensive_slot_p_kwh": s["max_price"],
                "prices_predicted": s["predicted"],
                "status": status,
            })
        return {
            "sessions": out,
            "charging_now": self.coordinator.data.get(DATA_SCHEDULE_ACTIVE, False),
        }


class NextChargeStartSensor(CoordinatorEntity[EVChargeCoordinator], SensorEntity):
    """Timestamp of next planned charge slot start."""

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
    """Timestamp of next planned charge slot end."""

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
