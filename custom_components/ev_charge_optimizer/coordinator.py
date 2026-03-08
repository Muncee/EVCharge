"""Data coordinator for EV Charge Optimizer - fetches AgilePredict prices and computes strategy."""
from __future__ import annotations

import logging
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
    REC_CHARGE_NOW_FULLY,
    REC_CHARGE_NOW_MINIMUM,
    REC_WAIT,
    REC_UNKNOWN,
    DATA_RECOMMENDATION,
    DATA_REASON,
    DATA_CURRENT_PRICE,
    DATA_AVG_PRICE,
    DATA_CHEAP_THRESHOLD,
    DATA_EXPENSIVE_THRESHOLD,
    DATA_IS_CHEAP_NOW,
    DATA_IS_EXPENSIVE_NOW,
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
    DATA_PRICES,
    DATA_LONG_EXPENSIVE_DAYS,
    DATA_CHARGE_NOW_BINARY,
    DATA_NEXT_CHARGE_DATETIME,
    DATA_NEXT_CHARGE_PRICE,
    DATA_NEXT_CHARGE_TARGET_PCT,
    DATA_SECOND_CHARGE_DATETIME,
    DATA_CHARGE_SCHEDULE,
    DATA_SCHEDULE_ACTIVE,
    DATA_NEXT_SCHEDULE_START,
    DATA_NEXT_SCHEDULE_PRICE,
    EVENT_PRICES_UPDATED,
)

_LOGGER = logging.getLogger(__name__)


class EVChargeCoordinator(DataUpdateCoordinator):
    """Fetches AgilePredict price data and computes an EV charging strategy."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.config = config
        # Runtime overrides set by number entities (target%, daily usage)
        self.overrides: dict[str, Any] = {}
        # Track last notification date so we fire once per day after 4:30pm
        self._last_notification_date: Any = None

    async def _async_update_data(self) -> dict[str, Any]:
        region = self.config[CONF_REGION]
        url = f"{API_BASE_URL}/{region}?days={API_DAYS}&forecast_count=1&high_low=True"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        raise UpdateFailed(
                            f"AgilePredict API returned HTTP {resp.status}"
                        )
                    raw = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Network error fetching AgilePredict data: {err}") from err

        _LOGGER.debug("AgilePredict raw response (first 500 chars): %s", str(raw)[:500])
        prices = self._parse_prices(raw)
        if not prices:
            _LOGGER.error(
                "AgilePredict returned no usable price data. "
                "Raw response type=%s, value (first 300 chars)=%s",
                type(raw).__name__,
                str(raw)[:300],
            )
            raise UpdateFailed("AgilePredict returned no usable price data")

        current_battery_pct = self._get_battery_pct()
        result = self._compute_strategy(prices, current_battery_pct)

        # Fire a HA event once per day at the first update after 16:30 local time.
        # This aligns with Octopus publishing next-day Agile prices (~4pm).
        local_now = dt_util.as_local(dt_util.utcnow())
        if (
            local_now.hour > 16 or (local_now.hour == 16 and local_now.minute >= 30)
        ) and self._last_notification_date != local_now.date():
            self._last_notification_date = local_now.date()
            self.hass.bus.async_fire(
                EVENT_PRICES_UPDATED,
                {
                    "recommendation": result[DATA_RECOMMENDATION],
                    "reason": result[DATA_REASON],
                    "current_price": result[DATA_CURRENT_PRICE],
                    "next_schedule_start": (
                        result[DATA_NEXT_SCHEDULE_START].isoformat()
                        if result[DATA_NEXT_SCHEDULE_START]
                        else None
                    ),
                    "next_schedule_price": result[DATA_NEXT_SCHEDULE_PRICE],
                },
            )
            _LOGGER.info(
                "ev_charge_optimizer: fired %s — %s",
                EVENT_PRICES_UPDATED,
                result[DATA_REASON],
            )

        return result

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_prices(self, raw: Any) -> list[dict]:
        """Parse API response into a sorted list of {datetime, price, high, low} dicts."""
        prices: list[dict] = []
        try:
            # API returns an array of forecasts; take the most recent
            forecast = raw[0] if isinstance(raw, list) and raw else raw
            if not isinstance(forecast, dict):
                _LOGGER.error("Unexpected AgilePredict forecast type: %s", type(forecast).__name__)
                return prices
            raw_prices = forecast.get("prices") or forecast.get("data") or []
            _LOGGER.debug("raw_prices type=%s len=%s", type(raw_prices).__name__, len(raw_prices) if hasattr(raw_prices, "__len__") else "?")

            if isinstance(raw_prices, dict):
                # Dict keyed by datetime string
                for dt_str, vals in raw_prices.items():
                    dt = dt_util.parse_datetime(dt_str)
                    if not dt:
                        _LOGGER.debug("Could not parse datetime: %s", dt_str)
                        continue
                    if isinstance(vals, dict):
                        price = float(vals.get("forecast") or vals.get("price") or vals.get("value") or 0)
                        high = float(vals.get("high", price))
                        low = float(vals.get("low", price))
                    else:
                        price = float(vals)
                        high = low = price
                    prices.append({"datetime": dt, "price": price, "high": high, "low": low})

            elif isinstance(raw_prices, list):
                # List of slot objects
                for item in raw_prices:
                    if not isinstance(item, dict):
                        continue
                    dt_str = (
                        item.get("date_time")
                        or item.get("datetime")
                        or item.get("valid_from")
                        or item.get("time")
                        or item.get("period")
                    )
                    price = float(
                        item.get("agile_pred")
                        or item.get("forecast")
                        or item.get("price")
                        or item.get("value_inc_vat")
                        or item.get("value")
                        or 0
                    )
                    high = float(item.get("agile_high") or item.get("high") or price)
                    low = float(item.get("agile_low") or item.get("low") or price)
                    if dt_str:
                        dt = dt_util.parse_datetime(str(dt_str))
                        if dt:
                            prices.append({"datetime": dt, "price": price, "high": high, "low": low})

        except Exception as err:
            _LOGGER.error("Failed to parse AgilePredict response: %s", err)

        prices.sort(key=lambda x: x["datetime"])
        return prices

    # ------------------------------------------------------------------
    # Battery state
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
    # Strategy
    # ------------------------------------------------------------------

    # Hard-coded safety buffer — battery below this = must charge now
    _SAFETY_PCT = 10.0
    # Truly empty — charge regardless of price
    _CRITICAL_PCT = 5.0
    # Only recommend waiting if the upcoming slot is at least this much cheaper
    _WAIT_SAVING_THRESHOLD = 0.80  # 20% cheaper

    def _compute_strategy(
        self, prices: list[dict], current_battery_pct: float | None
    ) -> dict[str, Any]:
        now = dt_util.utcnow()

        battery_cap: float = self.config[CONF_BATTERY_CAPACITY]
        charge_rate: float = self.config[CONF_CHARGE_RATE]
        # These two can be overridden at runtime by number entities
        daily_usage: float = float(self.overrides.get(CONF_DAILY_USAGE, self.config[CONF_DAILY_USAGE]))
        target_pct: float = float(self.overrides.get(CONF_TARGET_CHARGE_PCT, self.config[CONF_TARGET_CHARGE_PCT]))

        # If battery level unknown, assume at target
        if current_battery_pct is None:
            current_battery_pct = target_pct

        # ---------------------------------------------------------------
        # Price analysis — use only future prices vs the 14-day window
        # ---------------------------------------------------------------
        future_slots = [s for s in prices if s["datetime"] > now]
        if not future_slots:
            return self._unknown_result()

        current_slot = next(
            (s for s in prices if s["datetime"] <= now < s["datetime"] + timedelta(minutes=30)),
            None,
        )
        current_price = current_slot["price"] if current_slot else future_slots[0]["price"]

        # Up to 14 days of future prices for percentile/rank analysis
        analysis_slots = future_slots[: 14 * 48]
        analysis_prices_vals = [s["price"] for s in analysis_slots]
        sorted_analysis = sorted(analysis_prices_vals)
        avg_price = statistics.mean(analysis_prices_vals)

        # Fixed thresholds (bottom 20% = cheap, top 25% = expensive)
        cheap_threshold = self._percentile(sorted_analysis, 20)
        expensive_threshold = self._percentile(sorted_analysis, 75)

        # Where does current price rank in next 14 days? (0 = cheapest, 1 = most expensive)
        current_rank = (
            sum(1 for p in sorted_analysis if p <= current_price) / len(sorted_analysis)
            if sorted_analysis
            else 0.5
        )

        # Label slots for binary sensor / next cheap window
        for slot in prices:
            slot["cheap"] = slot["price"] <= cheap_threshold
            slot["expensive"] = slot["price"] >= expensive_threshold

        is_cheap_now = current_price <= cheap_threshold
        is_expensive_now = current_price >= expensive_threshold
        next_cheap_slot = next((s for s in future_slots if s["cheap"]), None)
        hours_until_cheap = (
            (next_cheap_slot["datetime"] - now).total_seconds() / 3600
            if next_cheap_slot
            else None
        )

        # ---------------------------------------------------------------
        # Battery metrics — safety buffer is always 10%
        # ---------------------------------------------------------------
        safety_kwh = (self._SAFETY_PCT / 100) * battery_cap
        current_kwh = (current_battery_pct / 100) * battery_cap
        target_kwh = (target_pct / 100) * battery_cap
        kwh_needed = max(0.0, target_kwh - current_kwh)
        hours_to_charge = kwh_needed / charge_rate if charge_rate > 0 else 0.0

        usable_kwh = max(0.0, current_kwh - safety_kwh)
        days_of_range = usable_kwh / daily_usage if daily_usage > 0 else 999.0
        hours_of_range = days_of_range * 24

        # ---------------------------------------------------------------
        # Cheapest slot within battery range
        # ---------------------------------------------------------------
        must_charge_by = now + timedelta(hours=hours_of_range)
        latest_start = must_charge_by - timedelta(hours=max(hours_to_charge, 0.5))
        reachable_slots = [s for s in future_slots if s["datetime"] <= latest_start]
        cheapest_within_range = (
            min(reachable_slots, key=lambda x: x["price"]) if reachable_slots else None
        )

        # ---------------------------------------------------------------
        # Cheapest slot in the next 24 hours (parked-car lookahead)
        # ---------------------------------------------------------------
        slots_24h = [s for s in future_slots if s["datetime"] < now + timedelta(hours=24)]
        cheapest_24h = min(slots_24h, key=lambda x: x["price"]) if slots_24h else None

        # ---------------------------------------------------------------
        # Upcoming expensive period analysis (vs median, not a fixed threshold)
        # ---------------------------------------------------------------
        median_price = self._percentile(sorted_analysis, 50)
        upcoming_7day = [s for s in future_slots if s["datetime"] < now + timedelta(days=7)]
        pct_above_median_7day = (
            sum(1 for s in upcoming_7day if s["price"] > median_price) / len(upcoming_7day)
            if upcoming_7day
            else 0.0
        )
        long_expensive_days = self._longest_consecutive_expensive(future_slots)

        # ---------------------------------------------------------------
        # Today's cheapest + globally cheapest upcoming
        # ---------------------------------------------------------------
        today_midnight = now.replace(hour=23, minute=59, second=59, microsecond=0)
        today_slots = [s for s in prices if now <= s["datetime"] <= today_midnight]
        cheapest_today = min(today_slots, key=lambda x: x["price"]) if today_slots else None
        cheapest_upcoming = min(future_slots, key=lambda x: x["price"]) if future_slots else None

        # ---------------------------------------------------------------
        # Cost estimates
        # ---------------------------------------------------------------
        cost_now = round(current_price * kwh_needed / 100, 2)
        cost_cheapest_today = round(
            cheapest_today["price"] * kwh_needed / 100 if cheapest_today else cost_now, 2
        )
        estimated_savings = round(max(0.0, cost_now - cost_cheapest_today), 2)

        # ---------------------------------------------------------------
        # Decision
        # ---------------------------------------------------------------
        recommendation, reason = self._decide_strategy(
            current_price=current_price,
            current_rank=current_rank,
            kwh_needed=kwh_needed,
            current_battery_pct=current_battery_pct,
            target_pct=target_pct,
            days_of_range=days_of_range,
            cheapest_within_range=cheapest_within_range,
            cheapest_24h=cheapest_24h,
            pct_above_median_7day=pct_above_median_7day,
            avg_price=avg_price,
            now=now,
        )

        charge_now_binary = recommendation in (REC_CHARGE_NOW_FULLY, REC_CHARGE_NOW_MINIMUM)

        # Multi-day charge plan (legacy 2-session)
        next_charge_dt, next_charge_price, next_charge_target, second_charge_dt = (
            self._plan_charge_sessions(
                prices=prices,
                current_battery_pct=current_battery_pct,
                target_pct=target_pct,
                daily_usage=daily_usage,
                battery_cap=battery_cap,
                charge_rate=charge_rate,
                now=now,
            )
        )

        # Full weekly schedule
        charge_schedule = self._build_charge_schedule(
            prices=prices,
            current_battery_pct=current_battery_pct,
            target_pct=target_pct,
            daily_usage=daily_usage,
            battery_cap=battery_cap,
            charge_rate=charge_rate,
            now=now,
        )
        # First future window from schedule
        future_windows = [w for w in charge_schedule if w["end"] > now]
        active_window = next((w for w in future_windows if w["start"] <= now < w["end"]), None)
        next_window = next((w for w in future_windows if w["start"] > now), None)

        return {
            DATA_RECOMMENDATION: recommendation,
            DATA_REASON: reason,
            DATA_CURRENT_PRICE: round(current_price, 2),
            DATA_AVG_PRICE: round(avg_price, 2),
            DATA_CHEAP_THRESHOLD: round(cheap_threshold, 2),
            DATA_EXPENSIVE_THRESHOLD: round(expensive_threshold, 2),
            DATA_IS_CHEAP_NOW: is_cheap_now,
            DATA_IS_EXPENSIVE_NOW: is_expensive_now,
            DATA_NEXT_CHEAP_WINDOW: next_cheap_slot["datetime"] if next_cheap_slot else None,
            DATA_NEXT_CHEAP_PRICE: round(next_cheap_slot["price"], 2) if next_cheap_slot else None,
            DATA_HOURS_UNTIL_CHEAP: round(hours_until_cheap, 1) if hours_until_cheap is not None else None,
            DATA_CURRENT_BATTERY_PCT: round(current_battery_pct, 1),
            DATA_KWH_NEEDED: round(kwh_needed, 2),
            DATA_HOURS_TO_CHARGE: round(hours_to_charge, 2),
            DATA_COST_NOW: cost_now,
            DATA_COST_CHEAPEST: cost_cheapest_today,
            DATA_ESTIMATED_SAVINGS: estimated_savings,
            DATA_DAYS_OF_RANGE: round(days_of_range, 1),
            DATA_PCT_EXPENSIVE_7DAYS: round(pct_above_median_7day * 100, 0),
            DATA_CHEAPEST_TODAY_PRICE: round(cheapest_today["price"], 2) if cheapest_today else None,
            DATA_CHEAPEST_TODAY_START: cheapest_today["datetime"] if cheapest_today else None,
            DATA_CHEAPEST_UPCOMING_PRICE: round(cheapest_upcoming["price"], 2) if cheapest_upcoming else None,
            DATA_CHEAPEST_UPCOMING_START: cheapest_upcoming["datetime"] if cheapest_upcoming else None,
            DATA_LONG_EXPENSIVE_DAYS: round(long_expensive_days, 1),
            DATA_CHARGE_NOW_BINARY: charge_now_binary,
            DATA_PRICES: prices,
            DATA_NEXT_CHARGE_DATETIME: next_charge_dt,
            DATA_NEXT_CHARGE_PRICE: next_charge_price,
            DATA_NEXT_CHARGE_TARGET_PCT: next_charge_target,
            DATA_SECOND_CHARGE_DATETIME: second_charge_dt,
            DATA_CHARGE_SCHEDULE: charge_schedule,
            DATA_SCHEDULE_ACTIVE: active_window is not None,
            DATA_NEXT_SCHEDULE_START: next_window["start"] if next_window else None,
            DATA_NEXT_SCHEDULE_PRICE: round(next_window["price"], 2) if next_window else None,
        }

    def _decide_strategy(
        self,
        *,
        current_price: float,
        current_rank: float,
        kwh_needed: float,
        current_battery_pct: float,
        target_pct: float,
        days_of_range: float,
        cheapest_within_range: dict | None,
        cheapest_24h: dict | None,
        pct_above_median_7day: float,
        avg_price: float,
        now: datetime,
    ) -> tuple[str, str]:
        """
        Decide whether to charge now, wait, or do nothing.

        Key design principle: the car is parked most of the time. The battery
        depletion rate (daily_usage) does NOT apply while parked, so we should
        not use it to gate whether waiting is possible. Instead we always check
        the next 24 hours for a cheaper slot first.

        current_rank: 0.0 = cheapest slot in next 14 days, 1.0 = most expensive.
        """
        def _wait_msg(slot: dict, note: str) -> tuple[str, str]:
            wait_price = slot["price"]
            when = self._format_wait_time(slot["datetime"], now)
            savings = round((current_price - wait_price) * kwh_needed / 100, 2) if kwh_needed > 0 else 0.0
            savings_str = f", saves ~£{savings:.2f}" if savings > 0.01 else ""
            return (
                REC_WAIT,
                f"Wait until {when} — {wait_price:.1f}p/kWh vs {current_price:.1f}p now"
                f"{savings_str}. {note}",
            )

        # 1. Truly empty — must charge immediately regardless of price
        if current_battery_pct <= self._CRITICAL_PCT:
            return (
                REC_CHARGE_NOW_MINIMUM,
                f"Battery is critically low at {current_battery_pct:.0f}% — "
                f"charge now ({current_price:.1f}p/kWh) before it runs out completely.",
            )

        # 2. Cheap slot coming within 24 hours that's at least 20% cheaper than now.
        #    The car is likely parked — always worth waiting even with a low battery.
        if (
            cheapest_24h is not None
            and cheapest_24h["price"] < current_price * self._WAIT_SAVING_THRESHOLD
        ):
            low_note = (
                f"Battery is low ({current_battery_pct:.0f}%) — don't drive far before then."
                if current_battery_pct < 25
                else f"Your battery will last {days_of_range:.1f} days."
            )
            return _wait_msg(cheapest_24h, low_note)

        # 3. Opportunistic top-up: prices are in the bottom 15% of the next 2 weeks
        #    AND an expensive period dominates the coming week.
        if current_rank <= 0.15 and kwh_needed > 0 and pct_above_median_7day >= 0.60:
            savings = round((avg_price - current_price) * kwh_needed / 100, 2)
            rank_pct = max(1, int(current_rank * 100))
            return (
                REC_CHARGE_NOW_FULLY,
                f"Charge now — prices are in the cheapest {rank_pct}% of the next 2 weeks "
                f"({current_price:.1f}p/kWh vs {avg_price:.1f}p average). "
                f"Expensive prices expected for most of the next 7 days. "
                f"Charging to {target_pct:.0f}% now saves ~£{savings:.2f} vs waiting.",
            )

        # 4. Now is the cheapest (or near-cheapest) slot within battery driving range
        if (
            cheapest_within_range is not None
            and current_price <= cheapest_within_range["price"] * 1.10
            and kwh_needed > 0
        ):
            return (
                REC_CHARGE_NOW_FULLY,
                f"Good time to charge — {current_price:.1f}p/kWh is the cheapest available "
                f"in the next {days_of_range:.1f} days "
                f"(14-day average: {avg_price:.1f}p/kWh).",
            )

        # 5. Cheaper slot coming within driving range — wait for it
        if cheapest_within_range is not None:
            return _wait_msg(
                cheapest_within_range,
                f"Your battery will last {days_of_range:.1f} days.",
            )

        # 6. No cheaper option anywhere near — charge now
        return (
            REC_CHARGE_NOW_MINIMUM,
            f"Charge now — no significantly cheaper period is coming within your battery range. "
            f"Current price: {current_price:.1f}p/kWh (14-day average: {avg_price:.1f}p/kWh).",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _plan_charge_sessions(
        self,
        prices: list[dict],
        current_battery_pct: float,
        target_pct: float,
        daily_usage: float,
        battery_cap: float,
        charge_rate: float,
        now: datetime,
    ) -> tuple:
        """Calculate optimal timing for the next two charge sessions.

        Returns (next_charge_dt, next_charge_price, next_charge_target_pct, second_charge_dt).
        """
        if daily_usage <= 0:
            return None, None, None, None

        safety_kwh = (self._SAFETY_PCT / 100) * battery_cap
        current_kwh = (current_battery_pct / 100) * battery_cap
        target_kwh = (target_pct / 100) * battery_cap
        hours_to_charge = max(0.5, (target_kwh - safety_kwh) / charge_rate)

        # Session 1: find cheapest slot before battery hits safety level
        usable_kwh = max(0.0, current_kwh - safety_kwh)
        hours_of_range = (usable_kwh / daily_usage) * 24
        must_charge_by = now + timedelta(hours=hours_of_range)
        latest_start = must_charge_by - timedelta(hours=hours_to_charge)

        future_slots = [s for s in prices if s["datetime"] > now]
        candidates = [s for s in future_slots if s["datetime"] <= latest_start]
        if not candidates:
            candidates = future_slots[:8]
        if not candidates:
            return None, None, None, None

        optimal = min(candidates, key=lambda x: x["price"])

        # Session 2: after charging to target at session 1
        usable_kwh_2 = max(0.0, target_kwh - safety_kwh)
        hours_of_range_2 = (usable_kwh_2 / daily_usage) * 24
        must_charge_by_2 = optimal["datetime"] + timedelta(hours=hours_of_range_2)
        latest_start_2 = must_charge_by_2 - timedelta(hours=hours_to_charge)

        candidates_2 = [
            s for s in future_slots
            if optimal["datetime"] < s["datetime"] <= latest_start_2
        ]
        second_optimal = min(candidates_2, key=lambda x: x["price"]) if candidates_2 else None

        return (
            optimal["datetime"],
            round(optimal["price"], 2),
            target_pct,
            second_optimal["datetime"] if second_optimal else None,
        )

    def _build_charge_schedule(
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

        Returns a list of dicts: {start, end, price, target_pct}.
        Each entry is a planned charge window. The car is assumed to deplete
        at daily_usage kWh/day between sessions.
        """
        if daily_usage <= 0:
            return []

        schedule: list[dict] = []
        battery = current_battery_pct
        t = now
        safety_kwh = (self._SAFETY_PCT / 100) * battery_cap
        target_kwh = (target_pct / 100) * battery_cap
        horizon = now + timedelta(days=7)
        future_slots = [s for s in prices if s["datetime"] > now]

        for _ in range(20):  # safety cap on iterations
            kwh = (battery / 100) * battery_cap
            usable = max(0.0, kwh - safety_kwh)
            hours_until_empty = (usable / daily_usage) * 24
            must_charge_by = t + timedelta(hours=hours_until_empty)

            if must_charge_by > horizon:
                break  # battery lasts beyond the 7-day window — done

            kwh_needed = max(0.0, target_kwh - max(kwh, safety_kwh))
            hours_to_charge = max(0.5, kwh_needed / charge_rate) if charge_rate > 0 else 1.0
            latest_start = must_charge_by - timedelta(hours=hours_to_charge)

            candidates = [s for s in future_slots if t < s["datetime"] <= latest_start]
            if not candidates:
                # No room before deadline — take the soonest available slot
                candidates = [s for s in future_slots if s["datetime"] > t][:8]
            if not candidates:
                break

            best = min(candidates, key=lambda x: x["price"])
            start_dt = best["datetime"]
            end_dt = start_dt + timedelta(hours=hours_to_charge)

            schedule.append({
                "start": start_dt,
                "end": end_dt,
                "price": best["price"],
                "target_pct": target_pct,
            })

            battery = target_pct
            t = end_dt

            if t >= horizon:
                break

        return schedule

    def _format_wait_time(self, dt: datetime, now: datetime) -> str:
        """Format a future datetime as e.g. 'today at 06:30', 'tomorrow at 14:00', 'Friday at 02:00'."""
        local_dt = dt_util.as_local(dt)
        local_now = dt_util.as_local(now)
        diff_days = (local_dt.date() - local_now.date()).days
        time_str = local_dt.strftime("%H:%M")
        if diff_days == 0:
            return f"today at {time_str}"
        if diff_days == 1:
            return f"tomorrow at {time_str}"
        return f"{local_dt.strftime('%A')} at {time_str}"

    def _longest_consecutive_expensive(self, future_slots: list[dict]) -> float:
        """Return length in days of the longest consecutive run of expensive slots."""
        max_run = 0
        current_run = 0
        for slot in future_slots:
            if slot.get("expensive"):
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                current_run = 0
        return max_run * 0.5 / 24  # 30-min slots -> days

    def _percentile(self, sorted_data: list[float], pct: float) -> float:
        if not sorted_data:
            return 0.0
        k = (len(sorted_data) - 1) * pct / 100
        f = int(k)
        c = f + 1
        if c >= len(sorted_data):
            return sorted_data[-1]
        return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])

    def _unknown_result(self) -> dict[str, Any]:
        return {
            DATA_RECOMMENDATION: REC_UNKNOWN,
            DATA_REASON: "Unable to fetch or parse AgilePredict price data.",
            DATA_CURRENT_PRICE: None,
            DATA_AVG_PRICE: None,
            DATA_CHEAP_THRESHOLD: None,
            DATA_EXPENSIVE_THRESHOLD: None,
            DATA_IS_CHEAP_NOW: False,
            DATA_IS_EXPENSIVE_NOW: False,
            DATA_NEXT_CHEAP_WINDOW: None,
            DATA_NEXT_CHEAP_PRICE: None,
            DATA_HOURS_UNTIL_CHEAP: None,
            DATA_CURRENT_BATTERY_PCT: None,
            DATA_KWH_NEEDED: None,
            DATA_HOURS_TO_CHARGE: None,
            DATA_COST_NOW: None,
            DATA_COST_CHEAPEST: None,
            DATA_ESTIMATED_SAVINGS: None,
            DATA_DAYS_OF_RANGE: None,
            DATA_PCT_EXPENSIVE_7DAYS: None,
            DATA_CHEAPEST_TODAY_PRICE: None,
            DATA_CHEAPEST_TODAY_START: None,
            DATA_CHEAPEST_UPCOMING_PRICE: None,
            DATA_CHEAPEST_UPCOMING_START: None,
            DATA_LONG_EXPENSIVE_DAYS: None,
            DATA_CHARGE_NOW_BINARY: False,
            DATA_PRICES: [],
            DATA_NEXT_CHARGE_DATETIME: None,
            DATA_NEXT_CHARGE_PRICE: None,
            DATA_NEXT_CHARGE_TARGET_PCT: None,
            DATA_SECOND_CHARGE_DATETIME: None,
            DATA_CHARGE_SCHEDULE: [],
            DATA_SCHEDULE_ACTIVE: False,
            DATA_NEXT_SCHEDULE_START: None,
            DATA_NEXT_SCHEDULE_PRICE: None,
        }
