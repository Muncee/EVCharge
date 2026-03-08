"""Sensor platform for EV Charge Optimizer."""
from __future__ import annotations

from datetime import timedelta
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
    DATA_AVG_14DAY,
    DATA_WEEKLY_PLAN,
    DATA_SCHEDULE_ACTIVE,
    DATA_NEXT_START,
    DATA_NEXT_END,
    DATA_SUMMARY,
    DATA_NOTIFICATION_TEXT,
    DATA_SCHEDULE_SLOTS,
)
from .coordinator import EVChargeCoordinator, ACTION_NO_CHARGE

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

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        return {"14_day_average_p_kwh": self.coordinator.data.get(DATA_AVG_14DAY)}


class WeeklyScheduleSensor(CoordinatorEntity[EVChargeCoordinator], SensorEntity):
    """Weekly charge plan — the main sensor.

    State: one-line description of current status or next action.

    Key attributes:
      sessions        — the 7-day plan (suitable for a custom card / template)
      markdown_table  — pre-formatted text for a Markdown card
      chart_data      — 48h of price points with scheduled flag (for ApexCharts)
      notification_text — multi-line text for automations to send as push notification
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
        d = self.coordinator.data
        plan = d.get(DATA_WEEKLY_PLAN, [])
        now = dt_util.utcnow()

        return {
            # Full 7-day plan — use in custom cards or template sensors
            "sessions": _plan_for_card(plan),

            # Pre-formatted Markdown table — drop into a Markdown card as:
            #   {{ state_attr('sensor.ev_charge_optimizer_weekly_charge_schedule','markdown_table') }}
            "markdown_table": _make_markdown_table(plan),

            # 48h price + scheduled-slot data — use in ApexCharts:
            #   series: [{data: state_attr(..., 'chart_data')}]
            "chart_data": _make_chart_data(d.get(DATA_SCHEDULE_SLOTS, []), now),

            # Ready-made notification body — use in an automation action:
            #   message: "{{ state_attr('sensor...', 'notification_text') }}"
            "notification_text": d.get(DATA_NOTIFICATION_TEXT),

            "charging_now": d.get(DATA_SCHEDULE_ACTIVE, False),
            "avg_14day_price_p_kwh": d.get(DATA_AVG_14DAY),
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


# ---------------------------------------------------------------------------
# Attribute formatters
# ---------------------------------------------------------------------------

def _plan_for_card(plan: list[dict]) -> list[dict]:
    """Simplified plan list for custom Lovelace cards."""
    ACTION_ICONS = {
        "no_charge": "✅",
        "charge": "🔵",
        "opportunistic": "🔵",
        "full_charge": "⚡",
    }
    out = []
    for d in plan:
        action = d.get("action", "no_charge")
        out.append({
            "date": d["date"],
            "day": d["day_name"],
            "short_date": d["short_date"],
            "icon": ACTION_ICONS.get(action, ""),
            "action": action,
            "label": d.get("reason", ""),
            "slots": d.get("charge_slot_times", "—"),
            "duration_hours": d.get("charge_hours", 0),
            "avg_price": d.get("day_avg_price"),
            "cheapest_price": d.get("day_min_price"),
            "vs_14day_avg_pct": d.get("vs_14day_avg_pct"),
            "battery_start_pct": d.get("battery_start_pct"),
            "prices_predicted": d.get("prices_predicted", True),
            "charging": action != "no_charge",
        })
    return out


def _make_markdown_table(plan: list[dict]) -> str:
    """Generate a Markdown table for a Lovelace Markdown card."""
    ACTION_ICONS = {
        "no_charge": "✅",
        "charge": "🔵",
        "opportunistic": "🔵",
        "full_charge": "⚡",
    }
    lines = ["| Day | Plan | Slots | Price |", "|-----|------|-------|-------|"]
    for d in plan:
        action = d.get("action", "no_charge")
        icon = ACTION_ICONS.get(action, "")
        day = f"**{d['day_name']} {d['short_date']}**" if action != "no_charge" else f"{d['day_name']} {d['short_date']}"
        slots = d.get("charge_slot_times", "—")
        if action == "no_charge":
            label = "No charge"
            price = f"{d.get('day_avg_price', '?')}p"
        elif action == "full_charge":
            label = "Full charge ⚡"
            price = f"**{d.get('day_min_price', '?')}p** cheapest"
        else:
            label = "Charge"
            price = f"{d.get('day_avg_price', '?')}p avg"
        predicted = " *(predicted)*" if d.get("prices_predicted") else ""
        lines.append(f"| {icon} {day} | {label}{predicted} | {slots} | {price} |")
    return "\n".join(lines)


def _make_chart_data(schedule_slots: list[dict], now) -> list[dict]:
    """Return 48h of {x: ISO timestamp, y: price, scheduled: bool} for ApexCharts.

    Use in a chart card:
      series:
        - data: "{{ state_attr('sensor.ev_charge_optimizer_weekly_charge_schedule', 'chart_data') }}"
          color_threshold:
            - value: 0
              color: green   # scheduled
    """
    horizon = now + timedelta(hours=48)
    scheduled_dts = {s["datetime"] for s in schedule_slots}
    # We need the price series — build from schedule_slots plus non-scheduled nearby slots
    # chart_data only covers slots already in the coordinator's price list via schedule_slots
    # For a richer chart, we expose all scheduled slots plus mark status
    points = []
    seen_dts = set()
    for slot in schedule_slots:
        dt = slot["datetime"]
        if dt > horizon:
            break
        points.append({
            "x": dt.isoformat(),
            "y": round(slot["price"], 2),
            "scheduled": True,
        })
        seen_dts.add(dt)
    points.sort(key=lambda p: p["x"])
    return points
