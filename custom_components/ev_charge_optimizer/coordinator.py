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
    DATA_WEEKLY_COST_GBP,
    DATA_WEEKLY_KWH,
    EVENT_PRICES_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

_SAFETY_PCT = 10.0

# Day plan action codes
ACTION_NO_CHARGE = "no_charge"
ACTION_CHARGE = "charge"               # must-charge (battery running low)
ACTION_OPPORTUNISTIC = "opportunistic"  # battery fine but tonight is cheap vs upcoming
ACTION_FULL_CHARGE = "full_charge"     # exceptional price — charge to 100%
ACTION_ROUTINE = "routine"             # battery below target, cheapest night in 2-3 day window


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

        weekly_cost = round(sum(d.get("charge_cost_gbp", 0) for d in weekly_plan), 2)
        weekly_kwh = round(sum(d.get("charge_kwh", 0) for d in weekly_plan), 2)

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
            DATA_WEEKLY_COST_GBP: weekly_cost,
            DATA_WEEKLY_KWH: weekly_kwh,
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

        # Per-calendar-day price summary for next 7 days.
        #
        # KEY DESIGN: decisions and slot picking use the OVERNIGHT WINDOW
        # (18:00 today → 09:00 tomorrow) rather than the full calendar day.
        # This stops the planner from scheduling charging during expensive
        # daytime hours just because it's still "today".
        day_summaries = []
        for i in range(7):
            day_date = local_now.date() + timedelta(days=i)

            # Full calendar day (for avg/min display only)
            day_start_local = datetime(
                day_date.year, day_date.month, day_date.day,
                tzinfo=local_now.tzinfo,
            )
            day_start_utc = dt_util.as_utc(day_start_local)
            day_end_utc = day_start_utc + timedelta(days=1)

            # Overnight charging window: 18:00 tonight → 09:00 tomorrow
            overnight_start_local = datetime(
                day_date.year, day_date.month, day_date.day, 18, 0,
                tzinfo=local_now.tzinfo,
            )
            next_date = day_date + timedelta(days=1)
            overnight_end_local = datetime(
                next_date.year, next_date.month, next_date.day, 9, 0,
                tzinfo=local_now.tzinfo,
            )
            overnight_start_utc = dt_util.as_utc(overnight_start_local)
            overnight_end_utc = dt_util.as_utc(overnight_end_local)

            # For today: start from max(now, 18:00) so we never look backwards
            effective_overnight_start = max(now, overnight_start_utc) if i == 0 else overnight_start_utc

            all_day_slots = [
                s for s in (future_slots if i == 0 else prices)
                if day_start_utc <= s["datetime"] < day_end_utc
            ]
            overnight_slots_all = [
                s for s in prices
                if effective_overnight_start <= s["datetime"] < overnight_end_utc
            ]

            # Average/min based on overnight window — that's what drives the decision
            if overnight_slots_all:
                day_avg = statistics.mean(s["price"] for s in overnight_slots_all)
                day_min = min(s["price"] for s in overnight_slots_all)
            elif all_day_slots:
                day_avg = statistics.mean(s["price"] for s in all_day_slots)
                day_min = min(s["price"] for s in all_day_slots)
            else:
                day_avg = avg_14day
                day_min = avg_14day

            day_summaries.append({
                "date": day_date,
                "slots": all_day_slots,              # display only
                "overnight_slots": overnight_slots_all,  # slot picking
                "overnight_start": effective_overnight_start,
                "overnight_end": overnight_end_utc,
                "avg": day_avg,
                "min": day_min,
            })

        # Two-pass schedule.
        #
        # Pass 1 — _build_optimal_schedule():
        #   Finds every "battery deadline" and assigns the charge to the CHEAPEST
        #   overnight window before that deadline, not just "the day the alarm trips".
        #   This fixes the Tuesday-vs-Wednesday bug: if battery hits safety on
        #   Wednesday but Tuesday overnight is cheaper, we schedule on Tuesday.
        #
        # Pass 2 — below:
        #   Builds the full display plan using the assignments from pass 1, then
        #   adds cost / kWh / duration data and generates reason text.

        charge_assignments = self._build_optimal_schedule(
            day_summaries=day_summaries,
            current_battery_pct=current_battery_pct,
            target_pct=target_pct,
            daily_usage=daily_usage,
            battery_cap=battery_cap,
            charge_rate=charge_rate,
            avg_14day=avg_14day,
            p15=p15,
            p35=p35,
        )

        # Pass 2 — build display plan
        kwh_per_slot = charge_rate * 0.5
        depletion_pct_per_day = (daily_usage / battery_cap) * 100
        battery = current_battery_pct
        scheduled_dts: set[datetime] = set()
        weekly_plan: list[dict] = []
        all_charge_slots: list[dict] = []

        for i, day in enumerate(day_summaries):
            battery_at_start = battery
            assignment = charge_assignments.get(i)

            if assignment:
                action = assignment["action"]
                charge_to = assignment["charge_to"]
                target_reason = assignment["target_reason"]
                cheapest_day_name = assignment.get("cheapest_day_name", day["date"].strftime("%A"))
                triggered_by = assignment.get("triggered_by", i)

                # Build reason text
                if action == ACTION_CHARGE:
                    if triggered_by != i:
                        reason = (
                            f"{cheapest_day_name} is the cheapest overnight window before "
                            f"the battery deadline ({day_summaries[triggered_by]['date'].strftime('%A')}). "
                            f"Tonight averages {day['avg']:.1f}p. "
                            f"{target_reason} — charging to {charge_to:.0f}%."
                        )
                    else:
                        reason = (
                            f"Battery must charge by tonight — no cheaper window available. "
                            f"{target_reason} — charging to {charge_to:.0f}%."
                        )
                elif action == ACTION_FULL_CHARGE:
                    reason = (
                        f"Cheapest overnight in the forecast ({day['avg']:.1f}p avg, "
                        f"{((1 - day['avg'] / avg_14day) * 100):.0f}% below the {avg_14day:.1f}p avg). "
                        f"{target_reason} — charging to {charge_to:.0f}%."
                    )
                elif action == ACTION_ROUTINE:
                    lookahead_n = assignment.get("routine_lookahead", 3)
                    reason = (
                        f"Battery at {battery_at_start:.0f}%, below target. "
                        f"Tonight ({day['avg']:.1f}p avg) is the cheapest of the next "
                        f"{lookahead_n} night(s). {target_reason} — charging to {charge_to:.0f}%."
                    )

                else:  # ACTION_OPPORTUNISTIC
                    lookahead = day_summaries[i + 1 : i + 6]
                    next_avg = statistics.mean(d["avg"] for d in lookahead) if lookahead else avg_14day
                    reason = (
                        f"Good opportunity — {day['avg']:.1f}p tonight vs {next_avg:.1f}p "
                        f"over the next {len(lookahead)} days. "
                        f"{target_reason} — charging to {charge_to:.0f}%."
                    )

                # Pick cheapest slots from overnight window
                available = [s for s in day["overnight_slots"] if s["datetime"] not in scheduled_dts]

                # Must-charge safety fallback: if overnight is empty, use next 48h
                if action == ACTION_CHARGE and not available:
                    cutoff = now + timedelta(hours=48)
                    available = [
                        s for s in prices
                        if s["datetime"] > now and s["datetime"] < cutoff
                        and s["datetime"] not in scheduled_dts
                    ]

                # For opportunistic/full_charge: only use slots below 14-day average.
                # Routine charging uses any slot in the overnight window (no price filter)
                # so it still works when all slots are above average.
                if action in (ACTION_OPPORTUNISTIC, ACTION_FULL_CHARGE):
                    cheap_available = [s for s in available if s["price"] < avg_14day]
                    if not cheap_available:
                        action = ACTION_NO_CHARGE
                        charge_to = None
                        reason = (
                            f"Overnight window has no slots below the 14-day average "
                            f"({avg_14day:.1f}p) — skipping charge today."
                        )
                    else:
                        available = cheap_available

                charge_slots: list[dict] = []
                if action != ACTION_NO_CHARGE:
                    current_kwh = (battery_at_start / 100) * battery_cap
                    charge_kwh = max(0.0, (charge_to / 100 * battery_cap) - current_kwh)
                    n_slots = max(1, math.ceil(charge_kwh / kwh_per_slot))
                    chosen = sorted(available, key=lambda s: s["price"])[:n_slots]
                    chosen.sort(key=lambda s: s["datetime"])
                    for s in chosen:
                        scheduled_dts.add(s["datetime"])
                    charge_slots = chosen
                    all_charge_slots.extend(charge_slots)
                    battery = min(100.0, charge_to)
                else:
                    battery = max(0.0, battery_at_start - depletion_pct_per_day)

            else:
                action = ACTION_NO_CHARGE
                charge_to = None
                charge_slots = []
                avg_text = f"{day['avg']:.1f}p"
                if day["avg"] > avg_14day * 1.10:
                    reason = (
                        f"Overnight prices are {((day['avg'] / avg_14day - 1) * 100):.0f}% above "
                        f"the 14-day average ({avg_text} vs {avg_14day:.1f}p) — not charging."
                    )
                else:
                    reason = (
                        f"Battery at {battery_at_start:.0f}%, prices are average ({avg_text}) "
                        f"— no charge needed."
                    )
                battery = max(0.0, battery_at_start - depletion_pct_per_day)

            # Cost & energy for this day's charging
            slot_cost_pence = sum((s["price"] / 100) * kwh_per_slot * 100 for s in charge_slots)
            slot_kwh = len(charge_slots) * kwh_per_slot

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
                "charge_kwh": round(slot_kwh, 2),
                "charge_cost_gbp": round(slot_cost_pence / 100, 2),
            })

        all_charge_slots.sort(key=lambda s: s["datetime"])
        return weekly_plan, all_charge_slots

    # ------------------------------------------------------------------
    # Pass 1: Optimal charge assignment
    # ------------------------------------------------------------------

    def _build_optimal_schedule(
        self,
        day_summaries: list[dict],
        current_battery_pct: float,
        target_pct: float,
        daily_usage: float,
        battery_cap: float,
        charge_rate: float,
        avg_14day: float,
        p15: float,
        p35: float,
    ) -> dict[int, dict]:
        """Two-pass planner: assign charges to the cheapest window before each deadline.

        Returns {day_idx: {action, charge_to, target_reason, cheapest_day_name, triggered_by}}

        For mandatory charges (battery near safety):
          - Calculate how many days until the battery hits the safety threshold.
          - Look at overnight windows from today up to that deadline.
          - Assign the charge to the CHEAPEST window, not the deadline day.
          - This ensures 'Tuesday 25.5p chosen over Wednesday 26.3p' when battery
            deadline falls on Wednesday.

        For opportunistic charges (battery healthy but prices are cheap):
          - Added for unscheduled cheap days after mandatory planning is done.
        """
        assignments: dict[int, dict] = {}
        depletion = (daily_usage / battery_cap) * 100
        battery = current_battery_pct

        i = 0
        while i < 7:
            if i in assignments:
                battery = assignments[i]["charge_to"]
                i += 1
                continue

            # How many days until battery hits safety threshold?
            days_until_critical = (battery - _SAFETY_PCT) / depletion if depletion > 0 else 99

            if days_until_critical <= 2:
                # Must charge before the deadline.
                # deadline = last day we can charge and still be OK.
                # We look one day earlier than the absolute last day so there's headroom.
                deadline_idx = min(i + max(0, math.floor(days_until_critical) - 1), 6)
                search_window = [
                    j for j in range(i, deadline_idx + 1)
                    if j not in assignments
                ]
                if not search_window:
                    search_window = [i]

                # Pick the cheapest overnight window in the search window
                best_j = min(search_window, key=lambda j: day_summaries[j]["avg"])
                best_day = day_summaries[best_j]

                strategic_tgt, tgt_reason = self._strategic_target(
                    day_summaries=day_summaries,
                    from_idx=best_j,
                    today_overnight_avg=best_day["avg"],
                    user_target_pct=target_pct,
                    daily_usage=daily_usage,
                    battery_cap=battery_cap,
                    avg_14day=avg_14day,
                    p35=p35,
                )

                action = (
                    ACTION_FULL_CHARGE if best_day["avg"] <= p15 else ACTION_CHARGE
                )
                assignments[best_j] = {
                    "action": action,
                    "charge_to": strategic_tgt,
                    "target_reason": tgt_reason,
                    "cheapest_day_name": best_day["date"].strftime("%A"),
                    "triggered_by": i,  # day that triggered the need
                }

                # Advance simulation: deplete to best_j then charge
                for _ in range(best_j - i):
                    battery = max(0.0, battery - depletion)
                battery = strategic_tgt
                i = best_j + 1

            else:
                # Battery OK — check for opportunistic charge on day i
                day = day_summaries[i]
                lookahead = day_summaries[i + 1 : i + 6]
                next_days_avg = (
                    statistics.mean(d["avg"] for d in lookahead) if lookahead else avg_14day
                )
                price_ratio = next_days_avg / day["avg"] if day["avg"] > 0 else 1.0
                today_is_cheap = day["avg"] <= p35
                upcoming_expensive = next_days_avg > avg_14day * 1.08

                if (
                    today_is_cheap
                    and upcoming_expensive
                    and price_ratio >= 1.25
                    and battery < (target_pct - 5)
                ):
                    strategic_tgt, tgt_reason = self._strategic_target(
                        day_summaries=day_summaries,
                        from_idx=i,
                        today_overnight_avg=day["avg"],
                        user_target_pct=target_pct,
                        daily_usage=daily_usage,
                        battery_cap=battery_cap,
                        avg_14day=avg_14day,
                        p35=p35,
                    )
                    action = ACTION_FULL_CHARGE if day["avg"] <= p15 else ACTION_OPPORTUNISTIC
                    assignments[i] = {
                        "action": action,
                        "charge_to": strategic_tgt,
                        "target_reason": tgt_reason,
                        "cheapest_day_name": day["date"].strftime("%A"),
                        "triggered_by": i,
                    }
                    battery = strategic_tgt

                elif battery < target_pct - 5:
                    # Routine recharge: battery has dropped below target.
                    # Look at tonight and the next 2 nights; charge on the
                    # cheapest window.  Cap the lookahead by battery safety
                    # margin so we never wait too long when running low.
                    if depletion > 0:
                        max_safe_wait = max(0, math.floor((battery - _SAFETY_PCT) / depletion) - 1)
                    else:
                        max_safe_wait = 2
                    lookahead_n = min(3, max_safe_wait + 1)
                    routine_window = day_summaries[i : i + lookahead_n]
                    unassigned = [k for k in range(len(routine_window)) if (i + k) not in assignments]

                    if unassigned:
                        best_offset = min(unassigned, key=lambda k: routine_window[k]["avg"])
                        if best_offset == 0:
                            # Tonight is cheapest in the window — charge now
                            strategic_tgt, tgt_reason = self._strategic_target(
                                day_summaries=day_summaries,
                                from_idx=i,
                                today_overnight_avg=day["avg"],
                                user_target_pct=target_pct,
                                daily_usage=daily_usage,
                                battery_cap=battery_cap,
                                avg_14day=avg_14day,
                                p35=p35,
                            )
                            action = ACTION_FULL_CHARGE if day["avg"] <= p15 else ACTION_ROUTINE
                            assignments[i] = {
                                "action": action,
                                "charge_to": strategic_tgt,
                                "target_reason": tgt_reason,
                                "cheapest_day_name": day["date"].strftime("%A"),
                                "triggered_by": i,
                                "routine_lookahead": lookahead_n,
                            }
                            battery = strategic_tgt
                        else:
                            # A cheaper night is coming — wait
                            battery = max(0.0, battery - depletion)
                    else:
                        battery = max(0.0, battery - depletion)

                else:
                    battery = max(0.0, battery - depletion)

                i += 1

        return assignments

    # ------------------------------------------------------------------
    # Strategic target calculation
    # ------------------------------------------------------------------

    def _strategic_target(
        self,
        day_summaries: list[dict],
        from_idx: int,
        today_overnight_avg: float,
        user_target_pct: float,
        daily_usage: float,
        battery_cap: float,
        avg_14day: float,
        p35: float,
    ) -> tuple[float, str]:
        """Work out the optimal charge target for a charge window.

        Rather than always using the user's configured target (or always 100%),
        this looks at the gap until the next cheap window and the price premium
        during that gap, then sets a target that:
          - Is never below the user's configured target (that's their floor)
          - Covers enough energy to reach the next cheap window safely
          - Goes higher if the gap is long/expensive relative to today's price
          - Goes to 100% if today is significantly cheaper than the coming gap

        Returns (target_pct, one_line_reason).
        """
        depletion_pct_per_day = (daily_usage / battery_cap) * 100

        # Find the next cheap overnight window after today
        days_gap = None
        next_cheap_avg = None
        for j in range(from_idx + 1, len(day_summaries)):
            d = day_summaries[j]
            if d["avg"] <= avg_14day * 0.92 or d["avg"] <= p35:
                days_gap = j - from_idx
                next_cheap_avg = d["avg"]
                break

        if days_gap is None:
            # No cheap window in the 7-day forecast at all
            days_gap = len(day_summaries) - from_idx
            next_cheap_avg = avg_14day

        # How expensive is the gap between now and the next cheap window?
        gap_days_list = day_summaries[from_idx + 1 : from_idx + 1 + days_gap]
        avg_gap_price = (
            statistics.mean(d["avg"] for d in gap_days_list)
            if gap_days_list else avg_14day
        )

        # Minimum charge needed to reach the next cheap window above safety
        # Add a 1-day buffer so we arrive with some headroom, not fumes
        pct_to_bridge = ((days_gap + 1) * depletion_pct_per_day)
        min_bridge_target = min(100.0, _SAFETY_PCT + pct_to_bridge)

        # Price premium: how much more expensive is the gap vs tonight?
        gap_premium = avg_gap_price / today_overnight_avg if today_overnight_avg > 0 else 1.0

        # Strategic top-up: the more expensive the gap vs today, the more we charge
        # Premium ≥ 3×  → fill to 100%  (massive saving opportunity)
        # Premium ≥ 2×  → fill to 90%   (strong saving)
        # Premium ≥ 1.5×→ fill to max(bridge, 80%)
        # Otherwise     → fill to max(bridge, user_target)
        if gap_premium >= 3.0 or days_gap >= 6:
            strategic = 100.0
            reason = (
                f"Next cheap window in {days_gap} day(s) ({next_cheap_avg:.1f}p avg); "
                f"gap averages {avg_gap_price:.1f}p ({gap_premium:.1f}x today's price) — "
                f"filling to 100% to minimise charging during expensive period"
            )
        elif gap_premium >= 2.0 or days_gap >= 4:
            strategic = max(min_bridge_target, 90.0)
            reason = (
                f"Next cheap window in {days_gap} day(s) ({next_cheap_avg:.1f}p avg); "
                f"gap averages {avg_gap_price:.1f}p ({gap_premium:.1f}x tonight) — "
                f"charging to {strategic:.0f}% to cover the gap cheaply"
            )
        elif gap_premium >= 1.5 or days_gap >= 2:
            strategic = max(min_bridge_target, 80.0)
            reason = (
                f"Next cheap window in {days_gap} day(s) ({next_cheap_avg:.1f}p avg); "
                f"gap averages {avg_gap_price:.1f}p — "
                f"charging to {strategic:.0f}% to avoid paying peak rates"
            )
        else:
            strategic = max(min_bridge_target, user_target_pct)
            reason = (
                f"Next cheap window in {days_gap} day(s) ({next_cheap_avg:.1f}p avg) — "
                f"charging to {strategic:.0f}% (enough to reach it comfortably)"
            )

        # Never go below the user's configured target
        final = max(user_target_pct, strategic)
        return round(final, 1), reason

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
            elif action == ACTION_ROUTINE:
                icon = "🔋"
                detail = (
                    f"{day['charge_slot_times']} · "
                    f"{day['day_avg_price']:.1f}p avg · "
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
        ACTION_ROUTINE: "Routine charge",
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
