"""Data coordinator for EV Charge Optimizer."""
from __future__ import annotations

import logging
import math
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
    DATA_SCHEDULE,
    DATA_SCHEDULE_ACTIVE,
    DATA_NEXT_START,
    DATA_NEXT_END,
    DATA_SUMMARY,
    EVENT_PRICES_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

# Battery level below which we consider it "at safety buffer" and plan the next charge
_SAFETY_PCT = 10.0


class EVChargeCoordinator(DataUpdateCoordinator):
    """Fetches AgilePredict prices and builds a weekly charge schedule."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.config = config
        # Runtime overrides set by number entities
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
            raise UpdateFailed(f"Network error fetching AgilePredict data: {err}") from err

        prices = self._parse_prices(raw)
        if not prices:
            _LOGGER.error("AgilePredict returned no usable price data. Raw: %s", str(raw)[:300])
            raise UpdateFailed("AgilePredict returned no usable price data")

        result = self._compute(prices)

        # Fire a HA event once per day after 16:30 local time.
        # Octopus publishes next-day Agile prices ~4pm, so the first hourly
        # update after 16:30 will pick them up.
        local_now = dt_util.as_local(dt_util.utcnow())
        after_430 = local_now.hour > 16 or (local_now.hour == 16 and local_now.minute >= 30)
        if after_430 and self._last_notification_date != local_now.date():
            self._last_notification_date = local_now.date()
            self.hass.bus.async_fire(
                EVENT_PRICES_UPDATED,
                {
                    "summary": result[DATA_SUMMARY],
                    "current_price": result[DATA_CURRENT_PRICE],
                    "next_charge_start": (
                        result[DATA_NEXT_START].isoformat() if result[DATA_NEXT_START] else None
                    ),
                },
            )

        return result

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_prices(self, raw: Any) -> list[dict]:
        """Return sorted list of {datetime, price, predicted} from AgilePredict response."""
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
    # Schedule computation
    # ------------------------------------------------------------------

    def _compute(self, prices: list[dict]) -> dict[str, Any]:
        now = dt_util.utcnow()

        battery_cap: float = self.config[CONF_BATTERY_CAPACITY]
        charge_rate: float = self.config[CONF_CHARGE_RATE]
        daily_usage: float = float(self.overrides.get(CONF_DAILY_USAGE, self.config[CONF_DAILY_USAGE]))
        target_pct: float = float(self.overrides.get(CONF_TARGET_CHARGE_PCT, self.config[CONF_TARGET_CHARGE_PCT]))

        current_battery_pct = self._get_battery_pct()
        if current_battery_pct is None:
            current_battery_pct = target_pct

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

        # Build schedule
        schedule = self._build_schedule(
            prices=prices,
            current_battery_pct=current_battery_pct,
            target_pct=target_pct,
            daily_usage=daily_usage,
            battery_cap=battery_cap,
            charge_rate=charge_rate,
            now=now,
        )

        # Determine current/next window
        active = next((s for s in schedule if s["start"] <= now < s["end"]), None)
        upcoming = next((s for s in schedule if s["start"] > now), None)

        summary = self._make_summary(schedule, active, current_price, now)

        return {
            DATA_CURRENT_PRICE: round(current_price, 2) if current_price is not None else None,
            DATA_SCHEDULE: schedule,
            DATA_SCHEDULE_ACTIVE: active is not None,
            DATA_NEXT_START: upcoming["start"] if upcoming else None,
            DATA_NEXT_END: upcoming["end"] if upcoming else None,
            DATA_SUMMARY: summary,
        }

    def _build_schedule(
        self,
        prices: list[dict],
        current_battery_pct: float,
        target_pct: float,
        daily_usage: float,
        battery_cap: float,
        charge_rate: float,
        now: datetime,
    ) -> list[dict]:
        """Build a 7-day charge schedule.

        For each cycle, calculates exactly how many 30-min slots are needed
        to charge from the safety level to target%, then uses a sliding window
        to find the cheapest CONTIGUOUS block of that many slots before the
        battery deadline. This means if you need 5 hours, it finds the cheapest
        5-hour window — not just the single cheapest slot.
        """
        if daily_usage <= 0 or charge_rate <= 0:
            return []

        safety_pct = _SAFETY_PCT
        safety_kwh = (safety_pct / 100) * battery_cap
        target_kwh = (target_pct / 100) * battery_cap
        # Slots needed to charge from safety to target
        kwh_per_slot = charge_rate * 0.5  # 30-min slot
        n_slots_needed = max(1, math.ceil((target_kwh - safety_kwh) / kwh_per_slot))

        schedule: list[dict] = []
        battery = current_battery_pct
        t = now
        horizon = now + timedelta(days=7)
        future_slots = [s for s in prices if s["datetime"] > now]

        for _ in range(20):
            current_kwh = (battery / 100) * battery_cap
            usable_kwh = max(0.0, current_kwh - safety_kwh)
            hours_of_range = (usable_kwh / daily_usage) * 24
            must_charge_by = t + timedelta(hours=hours_of_range)

            if must_charge_by > horizon:
                break  # battery lasts the whole week — nothing to plan

            # Find cheapest contiguous block before deadline
            block = self._cheapest_block(
                slots=[s for s in future_slots if t < s["datetime"]],
                n=n_slots_needed,
                deadline=must_charge_by,
            )

            if block is None:
                break

            start_dt = block[0]["datetime"]
            end_dt = block[-1]["datetime"] + timedelta(minutes=30)
            avg_price = sum(s["price"] for s in block) / len(block)
            all_predicted = all(s.get("predicted", True) for s in block)

            schedule.append({
                "start": start_dt,
                "end": end_dt,
                "avg_price": round(avg_price, 2),
                "duration_hours": round(len(block) * 0.5, 1),
                "target_pct": target_pct,
                "predicted": all_predicted,
            })

            battery = target_pct
            t = end_dt

            if t >= horizon:
                break

        return schedule

    def _cheapest_block(
        self, slots: list[dict], n: int, deadline: datetime
    ) -> list[dict] | None:
        """Return the cheapest contiguous n-slot block that ends by deadline.

        Uses a sliding window over time-sorted consecutive slots.
        Falls back to earliest available slots if no perfect block exists.
        """
        # Only consider slots that start early enough so the block ends by deadline
        cutoff = deadline - timedelta(minutes=30 * (n - 1))
        candidates = [s for s in slots if s["datetime"] <= cutoff]
        if not candidates:
            # Deadline too tight — take the n soonest slots we can get
            earliest = slots[:n]
            return earliest if earliest else None

        # Build runs of consecutive slots (30-min apart)
        # Group into runs, then apply sliding window within each run
        best_block: list[dict] | None = None
        best_cost: float | None = None

        i = 0
        while i < len(candidates):
            # Build the longest consecutive run starting at i
            run = [candidates[i]]
            j = i + 1
            while j < len(candidates):
                gap = candidates[j]["datetime"] - candidates[j - 1]["datetime"]
                if gap == timedelta(minutes=30):
                    run.append(candidates[j])
                    j += 1
                else:
                    break

            # Slide an n-wide window over this run
            if len(run) >= n:
                window_cost = sum(s["price"] for s in run[:n])
                if best_cost is None or window_cost < best_cost:
                    best_cost = window_cost
                    best_block = run[:n]
                for k in range(1, len(run) - n + 1):
                    window_cost += run[k + n - 1]["price"] - run[k - 1]["price"]
                    if window_cost < best_cost:
                        best_cost = window_cost
                        best_block = run[k: k + n]
            elif run:
                # Run shorter than needed — take what we can as fallback
                cost = sum(s["price"] for s in run)
                if best_cost is None or cost < best_cost:
                    best_cost = cost
                    best_block = run

            i = j  # move to next run

        return best_block

    # ------------------------------------------------------------------
    # Summary text
    # ------------------------------------------------------------------

    def _make_summary(
        self,
        schedule: list[dict],
        active: dict | None,
        current_price: float | None,
        now: datetime,
    ) -> str:
        if active:
            end_local = dt_util.as_local(active["end"])
            return (
                f"Charging now until {end_local.strftime('%H:%M')} "
                f"({active['avg_price']:.1f}p avg)"
            )
        if not schedule:
            return "No charge sessions planned (check daily usage setting)"

        upcoming = next((s for s in schedule if s["start"] > now), None)
        if not upcoming:
            return "All sessions complete for this week"

        start_local = dt_util.as_local(upcoming["start"])
        end_local = dt_util.as_local(upcoming["end"])
        diff_days = (start_local.date() - dt_util.as_local(now).date()).days
        if diff_days == 0:
            day = "Today"
        elif diff_days == 1:
            day = "Tomorrow"
        else:
            day = start_local.strftime("%A")

        return (
            f"Next: {day} {start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')} "
            f"({upcoming['avg_price']:.1f}p avg, {upcoming['duration_hours']:.1f}h)"
        )

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
