"""Data coordinator for EV Charge Optimizer."""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    API_BASE_URL,
    API_DAYS,
    UPDATE_INTERVAL_MINUTES,
    CONF_REGION,
    CONF_BATTERY_CAPACITY,
    CONF_DAILY_USAGE,
    CONF_CHARGE_RATE,
    CONF_TARGET_CHARGE_PCT,
    CONF_BATTERY_ENTITY,
    DATA_CURRENT_PRICE,
    DATA_AVG_14DAY,
    DATA_SCHEDULE_SLOTS,
    DATA_SCHEDULE_SESSIONS,
    DATA_WEEKLY_PLAN,
    DATA_SCHEDULE_ACTIVE,
    DATA_NEXT_START,
    DATA_NEXT_END,
    DATA_SUMMARY,
    DATA_NOTIFICATION_TEXT,
    EVENT_PRICES_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

_SAFETY_PCT = 10.0

# Day plan action codes
ACTION_NO_CHARGE = "no_charge"
ACTION_CHARGE = "charge"               # must-charge (battery running low)
ACTION_OPPORTUNISTIC = "opportunistic"  # battery fine but tonight is cheap vs upcoming
ACTION_FULL_CHARGE = "full_charge"     # exceptional price — charge to 100%


class EVChargeCoordinator(DataUpdateCoordinator):
    """Fetches AgilePredict prices and builds an intelligent weekly charge schedule."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.config = config
        self.overrides: dict[str, Any] = {}
        self._last_notification_date: Any = None

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        region = self.config[CONF_REGION]
        url = f"{API_BASE_URL}/{region}?days={API_DAYS}&forecast_count=1&high_low=True"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        raise UpdateFailed(f"AgilePredict API returned HTTP {resp.status}")
                    raw = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Network error: {err}") from err

        prices = self._parse_prices(raw)
        if not prices:
            _LOGGER.error("AgilePredict returned no usable price data. Raw: %s", str(raw)[:300])
            raise UpdateFailed("AgilePredict returned no usable price data")

        result = self._compute(prices)

        # Fire notification event once per day after 16:30 local time
        local_now = dt_util.as_local(dt_util.utcnow())
        after_430 = local_now.hour > 16 or (local_now.hour == 16 and local_now.minute >= 30)
        if after_430 and self._last_notification_date != local_now.date():
            self._last_notification_date = local_now.date()
            self.hass.bus.async_fire(
                EVENT_PRICES_UPDATED,
                {
                    "summary": result[DATA_SUMMARY],
                    "notification_text": result[DATA_NOTIFICATION_TEXT],
                    "current_price": result[DATA_CURRENT_PRICE],
                    "avg_14day_price": result[DATA_AVG_14DAY],
                },
            )

        return result

    # ------------------------------------------------------------------
    # Price parsing
    # ------------------------------------------------------------------

    def _parse_prices(self, raw: Any) -> list[dict]:
        """Return sorted list of {datetime, price, predicted} dicts."""
        prices: list[dict] = []
        try:
            forecast = raw[0] if isinstance(raw, list) and raw else raw
            if not isinstance(forecast, dict):
                return prices
            raw_prices = forecast.get("prices") or forecast.get("data") or []

            if isinstance(raw_prices, dict):
                for dt_str, vals in raw_prices.items():
                    dt = dt_util.parse_datetime(dt_str)
                    if not dt:
                        continue
                    if isinstance(vals, dict):
                        price = float(vals.get("forecast") or vals.get("price") or vals.get("value") or 0)
                        high = float(vals.get("high", price))
                        low = float(vals.get("low", price))
                    else:
                        price = float(vals)
                        high = low = price
                    prices.append({"datetime": dt, "price": price, "predicted": high != low})

            elif isinstance(raw_prices, list):
                for item in raw_prices:
                    if not isinstance(item, dict):
                        continue
                    dt_str = (
                        item.get("date_time") or item.get("datetime")
                        or item.get("valid_from") or item.get("time") or item.get("period")
                    )
                    price = float(
                        item.get("agile_pred") or item.get("forecast")
                        or item.get("price") or item.get("value_inc_vat")
                        or item.get("value") or 0
                    )
                    high = float(item.get("agile_high") or item.get("high") or price)
                    low = float(item.get("agile_low") or item.get("low") or price)
                    if dt_str:
                        dt = dt_util.parse_datetime(str(dt_str))
                        if dt:
                            prices.append({"datetime": dt, "price": price, "predicted": high != low})
        except Exception as err:
            _LOGGER.error("Failed to parse AgilePredict response: %s", err)

        prices.sort(key=lambda x: x["datetime"])
        return prices

    # ------------------------------------------------------------------
    # Main computation
    # ------------------------------------------------------------------

    def _compute(self, prices: list[dict]) -> dict[str, Any]:
        now = dt_util.utcnow()

        battery_cap: float = self.config[CONF_BATTERY_CAPACITY]
        charge_rate: float = self.config[CONF_CHARGE_RATE]
        daily_usage: float = float(self.overrides.get(CONF_DAILY_USAGE, self.config[CONF_DAILY_USAGE]))
        target_pct: float = float(self.overrides.get(CONF_TARGET_CHARGE_PCT, self.config[CONF_TARGET_CHARGE_PCT]))
        current_battery_pct = self._get_battery_pct() or target_pct

        # Current slot price
        current_slot = next(
            (s for s in prices if s["datetime"] <= now < s["datetime"] + timedelta(minutes=30)),
            None,
        )
        future_slots = [s for s in prices if s["datetime"] > now]
        current_price = (
            current_slot["price"] if current_slot
            else (future_slots[0]["price"] if future_slots else None)
        )

        # 14-day average for all context calculations
        all_14d_prices = [s["price"] for s in future_slots[: 14 * 48]]
        avg_14day = statistics.mean(all_14d_prices) if all_14d_prices else 10.0

        # Build the intelligent weekly plan
        weekly_plan, schedule_slots = self._plan_weekly_schedule(
            prices=prices,
            current_battery_pct=current_battery_pct,
            target_pct=target_pct,
            daily_usage=daily_usage,
            battery_cap=battery_cap,
            charge_rate=charge_rate,
            avg_14day=avg_14day,
            now=now,
        )

        # Group individual slots into consecutive sessions (for display / binary sensor attrs)
        sessions = _group_into_sessions(schedule_slots)

        # Active / next slot
        active_slot = next(
            (s for s in schedule_slots if s["datetime"] <= now < s["datetime"] + timedelta(minutes=30)),
            None,
        )
        next_slot = next((s for s in schedule_slots if s["datetime"] > now), None)

        summary = self._make_summary(weekly_plan, active_slot, now)
        notification_text = self._make_notification_text(weekly_plan, avg_14day, now)

        return {
            DATA_CURRENT_PRICE: round(current_price, 2) if current_price is not None else None,
            DATA_AVG_14DAY: round(avg_14day, 2),
            DATA_SCHEDULE_SLOTS: schedule_slots,
            DATA_SCHEDULE_SESSIONS: sessions,
            DATA_WEEKLY_PLAN: weekly_plan,
            DATA_SCHEDULE_ACTIVE: active_slot is not None,
            DATA_NEXT_START: next_slot["datetime"] if next_slot else None,
            DATA_NEXT_END: next_slot["datetime"] + timedelta(minutes=30) if next_slot else None,
            DATA_SUMMARY: summary,
            DATA_NOTIFICATION_TEXT: notification_text,
        }

    # ------------------------------------------------------------------
    # The weekly planner — the main brain
    # ------------------------------------------------------------------

    def _plan_weekly_schedule(
        self,
        prices: list[dict],
        current_battery_pct: float,
        target_pct: float,
        daily_usage: float,
        battery_cap: float,
        charge_rate: float,
        avg_14day: float,
        now: datetime,
    ) -> tuple[list[dict], list[dict]]:
        """Build a 7-day plan with intelligent reasoning.

        For each day:
          - Tracks battery state through the week
          - Must-charge: battery will hit safety threshold
          - Full-charge: today is exceptionally cheap AND upcoming days are expensive
            e.g. battery at 81%, tonight is 2p, next 10 days average 12p → charge to 100%
          - Opportunistic: today is meaningfully cheaper than upcoming days
          - No charge: prices are average or above, battery is healthy

        Returns (weekly_plan, flat_schedule_slots).
        """
        local_now = dt_util.as_local(now)
        future_slots = [s for s in prices if s["datetime"] > now]

        if not future_slots or daily_usage <= 0 or charge_rate <= 0:
            return [], []

        # Price percentiles for the full 14-day window
        all_14d = sorted(s["price"] for s in future_slots[: 14 * 48])
        p15 = _percentile(all_14d, 15)   # very cheap
        p35 = _percentile(all_14d, 35)   # somewhat cheap

        # Per-calendar-day price summary for next 7 days
        day_summaries = []
        for i in range(7):
            day_date = local_now.date() + timedelta(days=i)
            day_start_local = datetime(
                day_date.year, day_date.month, day_date.day,
                tzinfo=local_now.tzinfo,
            )
            day_start_utc = dt_util.as_utc(day_start_local)
            day_end_utc = day_start_utc + timedelta(days=1)
            # Today: only future slots; other days: all slots
            if i == 0:
                day_slots = [s for s in future_slots if s["datetime"] < day_end_utc]
            else:
                day_slots = [s for s in prices if day_start_utc <= s["datetime"] < day_end_utc]

            if day_slots:
                day_avg = statistics.mean(s["price"] for s in day_slots)
                day_min = min(s["price"] for s in day_slots)
            else:
                day_avg = avg_14day
                day_min = avg_14day

            day_summaries.append({
                "date": day_date,
                "slots": day_slots,
                "avg": day_avg,
                "min": day_min,
            })

        # Battery simulation
        safety_pct = _SAFETY_PCT
        kwh_per_slot = charge_rate * 0.5
        depletion_pct_per_day = (daily_usage / battery_cap) * 100

        battery = current_battery_pct
        scheduled_dts: set[datetime] = set()
        weekly_plan: list[dict] = []
        all_charge_slots: list[dict] = []

        for i, day in enumerate(day_summaries):
            battery_at_start = battery

            # Will battery hit safety within the next ~2 days without charging?
            battery_in_2_days = battery_at_start - 2 * depletion_pct_per_day
            must_charge = battery_in_2_days <= safety_pct

            # What do the upcoming days look like price-wise?
            lookahead = day_summaries[i + 1 : i + 6]  # next 5 days
            next_days_avg = (
                statistics.mean(d["avg"] for d in lookahead) if lookahead else avg_14day
            )
            # How much more expensive are upcoming days vs today?
            price_ratio = next_days_avg / day["avg"] if day["avg"] > 0 else 1.0

            today_is_very_cheap = day["avg"] <= p15
            today_is_cheap = day["avg"] <= p35
            upcoming_expensive = next_days_avg > avg_14day * 1.08  # upcoming is above avg

            # --- Decision ---
            if must_charge:
                # Battery genuinely running low — must charge
                # If it's also cheap, charge to full
                if today_is_cheap and upcoming_expensive and price_ratio >= 1.3:
                    action = ACTION_FULL_CHARGE
                    charge_to = 100.0
                    reason = (
                        f"Battery needs charging (will reach {safety_pct:.0f}% soon) "
                        f"AND today is {((1 - day['avg'] / avg_14day) * 100):.0f}% cheaper "
                        f"than the 14-day average. "
                        f"Next {len(lookahead)} days average {next_days_avg:.1f}p — "
                        f"topping up fully while it's cheap."
                    )
                else:
                    action = ACTION_CHARGE
                    charge_to = target_pct
                    reason = (
                        f"Battery will reach the {safety_pct:.0f}% safety threshold "
                        f"within the next 2 days — charging to {target_pct:.0f}%."
                    )

            elif today_is_very_cheap and upcoming_expensive and price_ratio >= 1.5 and battery_at_start < 98:
                # Exceptional price today, expensive week ahead — fully charge
                action = ACTION_FULL_CHARGE
                charge_to = 100.0
                reason = (
                    f"Today's prices ({day['avg']:.1f}p avg) are exceptionally cheap — "
                    f"in the bottom 15% of the next 2 weeks "
                    f"({((1 - day['avg'] / avg_14day) * 100):.0f}% below the {avg_14day:.1f}p average). "
                    f"Next {len(lookahead)} days average {next_days_avg:.1f}p "
                    f"({price_ratio:.1f}x more expensive) — recommended to fully charge now."
                )

            elif today_is_cheap and upcoming_expensive and price_ratio >= 1.25 and battery_at_start < (target_pct - 5):
                # Meaningfully cheaper today, battery below target — top up
                action = ACTION_OPPORTUNISTIC
                charge_to = target_pct
                reason = (
                    f"Good opportunity — today's prices ({day['avg']:.1f}p avg) are "
                    f"{((1 - day['avg'] / avg_14day) * 100):.0f}% below average "
                    f"and the next {len(lookahead)} days average {next_days_avg:.1f}p "
                    f"({price_ratio:.1f}x more expensive). "
                    f"Charging to {target_pct:.0f}% while it's relatively cheap."
                )

            else:
                action = ACTION_NO_CHARGE
                charge_to = None
                if day["avg"] > avg_14day * 1.10:
                    reason = (
                        f"Prices are {((day['avg'] / avg_14day - 1) * 100):.0f}% above "
                        f"the 14-day average ({day['avg']:.1f}p vs {avg_14day:.1f}p) — "
                        f"not charging today. Battery at {battery_at_start:.0f}%."
                    )
                else:
                    reason = (
                        f"Battery healthy at {battery_at_start:.0f}% and prices are average "
                        f"({day['avg']:.1f}p) — no charge needed today."
                    )

            # --- Pick cheapest individual slots for this day's charge ---
            charge_slots: list[dict] = []
            if action != ACTION_NO_CHARGE:
                current_kwh = (battery_at_start / 100) * battery_cap
                target_kwh = (charge_to / 100) * battery_cap
                charge_kwh = max(0.0, target_kwh - current_kwh)
                n_slots = max(1, math.ceil(charge_kwh / kwh_per_slot))

                # Candidates: this day's slots (plus early next morning if needed)
                available = [s for s in day["slots"] if s["datetime"] not in scheduled_dts]
                if len(available) < n_slots and i + 1 < len(day_summaries):
                    next_morning = [
                        s for s in day_summaries[i + 1]["slots"]
                        if s["datetime"] not in scheduled_dts
                        and dt_util.as_local(s["datetime"]).hour < 9
                    ]
                    available = available + next_morning

                chosen = sorted(available, key=lambda s: s["price"])[:n_slots]
                chosen.sort(key=lambda s: s["datetime"])

                for s in chosen:
                    scheduled_dts.add(s["datetime"])
                charge_slots = chosen
                all_charge_slots.extend(chosen)

                battery = min(100.0, charge_to)
            else:
                battery = max(0.0, battery_at_start - depletion_pct_per_day)

            # Slot times for display
            slot_times = _format_slot_times(charge_slots, local_now)
            vs_avg = ((day["avg"] - avg_14day) / avg_14day * 100) if avg_14day > 0 else 0
            all_predicted = all(s.get("predicted", True) for s in day["slots"][:3]) if day["slots"] else True

            weekly_plan.append({
                "date": day["date"].isoformat(),
                "day_name": day["date"].strftime("%A"),
                "short_date": day["date"].strftime("%-d %b"),
                "action": action,
                "reason": reason,
                "charge_slots": [
                    {
                        "datetime": s["datetime"].isoformat(),
                        "time": dt_util.as_local(s["datetime"]).strftime("%H:%M"),
                        "price_p_kwh": round(s["price"], 2),
                        "predicted": s.get("predicted", True),
                    }
                    for s in charge_slots
                ],
                "charge_slot_times": slot_times,
                "target_pct": charge_to,
                "battery_start_pct": round(battery_at_start, 1),
                "day_avg_price": round(day["avg"], 2),
                "day_min_price": round(day["min"], 2),
                "vs_14day_avg_pct": round(vs_avg, 1),
                "prices_predicted": all_predicted,
                "n_slots": len(charge_slots),
                "charge_hours": round(len(charge_slots) * 0.5, 1),
            })

        all_charge_slots.sort(key=lambda s: s["datetime"])
        return weekly_plan, all_charge_slots

    # ------------------------------------------------------------------
    # Text generation
    # ------------------------------------------------------------------

    def _make_summary(
        self,
        weekly_plan: list[dict],
        active_slot: dict | None,
        now: datetime,
    ) -> str:
        if active_slot:
            return f"Charging now ({active_slot['price']:.1f}p/kWh)"
        if not weekly_plan:
            return "No schedule — check settings"
        # Find today
        local_now = dt_util.as_local(now)
        today = local_now.date()
        for day in weekly_plan:
            import datetime as _dt
            day_date = _dt.date.fromisoformat(day["date"])
            if day_date == today and day["action"] != ACTION_NO_CHARGE:
                return f"Today: {_action_label(day['action'])} — {day['charge_slot_times']}"
        # No charge today — show next charge
        for day in weekly_plan:
            if day["action"] != ACTION_NO_CHARGE and day["charge_slots"]:
                return (
                    f"Next: {day['day_name']} {day['charge_slot_times']} "
                    f"({day['day_avg_price']:.1f}p avg)"
                )
        return "Battery healthy — no charging needed this week"

    def _make_notification_text(
        self, weekly_plan: list[dict], avg_14day: float, now: datetime
    ) -> str:
        """Multi-line text for push notifications and Markdown cards."""
        local_now = dt_util.as_local(now)
        lines = [f"📅 EV Charge Schedule — updated {local_now.strftime('%-d %b %H:%M')}", ""]
        for day in weekly_plan:
            action = day["action"]
            label = _action_label(action)
            if action == ACTION_NO_CHARGE:
                icon = "🟢"
                detail = f"{day['day_avg_price']:.1f}p avg"
                if day["vs_14day_avg_pct"] > 10:
                    detail += f" (+{day['vs_14day_avg_pct']:.0f}% vs avg)"
            elif action == ACTION_FULL_CHARGE:
                icon = "⚡"
                detail = (
                    f"{day['charge_slot_times']} · "
                    f"{day['day_min_price']:.1f}p cheapest · "
                    f"{day['charge_hours']:.1f}h"
                )
            else:
                icon = "🔵"
                detail = (
                    f"{day['charge_slot_times']} · "
                    f"{day['day_avg_price']:.1f}p avg · "
                    f"{day['charge_hours']:.1f}h"
                )

            predicted_note = " (predicted)" if day["prices_predicted"] else ""
            lines.append(
                f"{icon} {day['day_name']} {day['short_date']}: {label}{predicted_note}"
            )
            lines.append(f"   {detail}")

        lines.append("")
        lines.append(f"14-day average: {avg_14day:.1f}p/kWh")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Battery helper
    # ------------------------------------------------------------------

    def _get_battery_pct(self) -> float | None:
        entity_id = self.config.get(CONF_BATTERY_ENTITY)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable", ""):
            try:
                return float(state.state)
            except ValueError:
                pass
        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _group_into_sessions(slots: list[dict]) -> list[dict]:
    """Group consecutive 30-min slots into display sessions."""
    if not slots:
        return []
    sessions = []
    run = [slots[0]]
    for slot in slots[1:]:
        if slot["datetime"] - run[-1]["datetime"] == timedelta(minutes=30):
            run.append(slot)
        else:
            sessions.append(_session_from_run(run))
            run = [slot]
    sessions.append(_session_from_run(run))
    return sessions


def _session_from_run(run: list[dict]) -> dict:
    return {
        "start": run[0]["datetime"],
        "end": run[-1]["datetime"] + timedelta(minutes=30),
        "n_slots": len(run),
        "avg_price": round(sum(s["price"] for s in run) / len(run), 2),
        "min_price": round(min(s["price"] for s in run), 2),
        "max_price": round(max(s["price"] for s in run), 2),
        "predicted": all(s.get("predicted", True) for s in run),
        "slots": [{"datetime": s["datetime"], "price": round(s["price"], 2)} for s in run],
    }


def _format_slot_times(slots: list[dict], local_now: datetime) -> str:
    """Return a compact human-readable list of slot times, grouping consecutive runs."""
    if not slots:
        return "—"
    sessions = _group_into_sessions(slots)
    parts = []
    for s in sessions:
        start = dt_util.as_local(s["start"]).strftime("%H:%M")
        end = dt_util.as_local(s["end"]).strftime("%H:%M")
        if s["n_slots"] == 1:
            parts.append(start)
        else:
            parts.append(f"{start}–{end}")
    return ", ".join(parts)


def _action_label(action: str) -> str:
    return {
        ACTION_NO_CHARGE: "No charge",
        ACTION_CHARGE: "Charge overnight",
        ACTION_OPPORTUNISTIC: "Charge (good price)",
        ACTION_FULL_CHARGE: "FULL CHARGE — exceptional value",
    }.get(action, action)


def _percentile(sorted_data: list[float], pct: float) -> float:
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * pct / 100
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[-1]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])
