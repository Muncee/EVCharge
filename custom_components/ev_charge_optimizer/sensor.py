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
    DATA_SCHEDULE_SESSIONS,
    DATA_WEEKLY_COST_GBP,
    DATA_WEEKLY_KWH,
    CONF_BATTERY_CAPACITY,
    CONF_CHARGE_RATE,
    CONF_TARGET_CHARGE_PCT,
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
        ChargeCostSensor(coordinator, entry),
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
    """Weekly charge plan.

    State: one-line description of current status or next action.

    Key attributes:
      sessions         — 7-day plan (for custom Lovelace cards)
      markdown_table   — Markdown card ready (includes rationale per day)
      notification_text — push notification body
      chart_data       — 48h {x, y, scheduled} for ApexCharts
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
            "sessions": _plan_for_card(plan),
            "markdown_table": _make_markdown_table(plan),
            "notification_text": d.get(DATA_NOTIFICATION_TEXT),
            "chart_data": _make_chart_data(d.get(DATA_SCHEDULE_SLOTS, []), now),
            "charging_now": d.get(DATA_SCHEDULE_ACTIVE, False),
            "avg_14day_price_p_kwh": d.get(DATA_AVG_14DAY),
            "weekly_cost_gbp": d.get(DATA_WEEKLY_COST_GBP),
            "weekly_kwh": d.get(DATA_WEEKLY_KWH),
        }


class NextChargeStartSensor(CoordinatorEntity[EVChargeCoordinator], SensorEntity):
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


class ChargeCostSensor(CoordinatorEntity[EVChargeCoordinator], SensorEntity):
    """Estimated cost and duration for planned charging this week.

    State: total estimated cost in £ for all scheduled charges this week.

    Key attributes:
      next_session_cost_gbp   — cost of the very next charge session
      next_session_duration   — "3h 30m" for next session
      next_session_kwh        — kWh added in next session
      next_session_start      — ISO timestamp
      weekly_cost_gbp         — total cost across all sessions this week
      weekly_kwh              — total kWh across all sessions
      sessions                — per-day breakdown [{day, cost, kwh, duration, slots}]
      time_to_target_hours    — hours from now to reach target% at current charge rate
    """

    _attr_has_entity_name = True
    _attr_name = "Charge Cost Estimate"
    _attr_icon = "mdi:currency-gbp"
    _attr_native_unit_of_measurement = "GBP"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EVChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_charge_cost"
        self._attr_device_info = _device(entry.entry_id)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(DATA_WEEKLY_COST_GBP)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        d = self.coordinator.data
        plan = d.get(DATA_WEEKLY_PLAN, [])
        sessions_data = d.get(DATA_SCHEDULE_SESSIONS, [])
        now = dt_util.utcnow()

        # Per-day cost breakdown (only charging days)
        day_breakdown = [
            {
                "date": day["date"],
                "day": day["day_name"],
                "short_date": day["short_date"],
                "cost_gbp": day.get("charge_cost_gbp", 0),
                "kwh": day.get("charge_kwh", 0),
                "duration_text": _duration_text(day.get("charge_hours", 0)),
                "slots": day.get("charge_slot_times", "—"),
                "target_pct": day.get("target_pct"),
                "avg_price": day.get("day_avg_price"),
            }
            for day in plan
            if day.get("action") != ACTION_NO_CHARGE and day.get("n_slots", 0) > 0
        ]

        # Find next session (first session whose start is in the future)
        next_session = next(
            (s for s in sessions_data if s["start"] > now),
            None,
        )
        next_cost = None
        next_duration = None
        next_kwh = None
        next_start = None
        charge_rate = self.coordinator.config.get(CONF_CHARGE_RATE, 7.0)
        battery_cap = self.coordinator.config.get(CONF_BATTERY_CAPACITY, 60.0)
        target_pct = float(self.coordinator.config.get(CONF_TARGET_CHARGE_PCT, 80.0))
        current_battery = self.coordinator._get_battery_pct() or target_pct

        if next_session:
            slot_kwh = charge_rate * 0.5
            n = next_session["n_slots"]
            next_kwh = round(n * slot_kwh, 2)
            next_cost = round(
                sum((s["price"] / 100) * slot_kwh * 100 for s in next_session["slots"]) / 100,
                2,
            )
            next_duration = _duration_text(n * 0.5)
            next_start = next_session["start"].isoformat()

        # Time to reach target from current battery (wall-clock if charging continuously)
        kwh_needed = max(0.0, (target_pct - current_battery) / 100 * battery_cap)
        time_to_target = round(kwh_needed / charge_rate, 2) if charge_rate > 0 else None

        return {
            "next_session_cost_gbp": next_cost,
            "next_session_duration": next_duration,
            "next_session_kwh": next_kwh,
            "next_session_start": next_start,
            "weekly_cost_gbp": d.get(DATA_WEEKLY_COST_GBP),
            "weekly_kwh": d.get(DATA_WEEKLY_KWH),
            "sessions": day_breakdown,
            "time_to_target_hours": time_to_target,
            "time_to_target_text": _duration_text(time_to_target) if time_to_target else "—",
            "current_battery_pct": round(current_battery, 1),
            "target_pct": target_pct,
        }


# ---------------------------------------------------------------------------
# Attribute formatters
# ---------------------------------------------------------------------------

def _plan_for_card(plan: list[dict]) -> list[dict]:
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
            "duration_text": _duration_text(d.get("charge_hours", 0)),
            "avg_price": d.get("day_avg_price"),
            "cheapest_price": d.get("day_min_price"),
            "vs_14day_avg_pct": d.get("vs_14day_avg_pct"),
            "battery_start_pct": d.get("battery_start_pct"),
            "target_pct": d.get("target_pct"),
            "charge_kwh": d.get("charge_kwh", 0),
            "charge_cost_gbp": d.get("charge_cost_gbp", 0),
            "prices_predicted": d.get("prices_predicted", True),
            "charging": action != "no_charge",
        })
    return out


def _make_markdown_table(plan: list[dict]) -> str:
    """Markdown for a Lovelace card — includes the reason as a sub-row."""
    ACTION_ICONS = {
        "no_charge": "✅",
        "charge": "🔵",
        "opportunistic": "🔵",
        "full_charge": "⚡",
    }
    lines = [
        "| | Day | Slots | Price | Cost |",
        "|---|---|---|---|---|",
    ]
    for d in plan:
        action = d.get("action", "no_charge")
        icon = ACTION_ICONS.get(action, "")
        charging = action != "no_charge"

        day_label = (
            f"**{d['day_name']} {d['short_date']}**" if charging
            else f"{d['day_name']} {d['short_date']}"
        )
        slots = d.get("charge_slot_times", "—") if charging else "—"

        if charging:
            avg_p = d.get("day_avg_price", "?")
            target = d.get("target_pct")
            price_cell = f"**{avg_p:.1f}p** → {target:.0f}%" if target else f"**{avg_p:.1f}p**"
            cost = d.get("charge_cost_gbp", 0)
            kwh = d.get("charge_kwh", 0)
            predicted = " *(est.)*" if d.get("prices_predicted") else ""
            cost_cell = f"**£{cost:.2f}** / {kwh:.1f}kWh{predicted}"
        else:
            price_cell = f"{d.get('day_avg_price', '?'):.1f}p"
            cost_cell = "—"

        lines.append(f"| {icon} | {day_label} | {slots} | {price_cell} | {cost_cell} |")

        # Add reason as an italic sub-row for charging days
        if charging:
            reason = d.get("reason", "")
            if reason:
                # Truncate long reasons for the table
                short_reason = reason[:120] + "…" if len(reason) > 120 else reason
                lines.append(f"| | *{short_reason}* | | | |")

    return "\n".join(lines)


def _make_chart_data(schedule_slots: list[dict], now) -> list[dict]:
    horizon = now + timedelta(hours=48)
    points = []
    for slot in schedule_slots:
        dt = slot["datetime"]
        if dt > horizon:
            break
        points.append({
            "x": dt.isoformat(),
            "y": round(slot["price"], 2),
            "scheduled": True,
        })
    points.sort(key=lambda p: p["x"])
    return points


def _duration_text(hours: float | None) -> str:
    if not hours:
        return "—"
    h = int(hours)
    m = int(round((hours - h) * 60))
    if h == 0:
        return f"{m}m"
    if m == 0:
        return f"{h}h"
    return f"{h}h {m}m"
