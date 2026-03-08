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
    CONF_MIN_CHARGE_PCT,
    CONF_TARGET_CHARGE_PCT,
    CONF_BATTERY_ENTITY,
    CONF_CHEAP_PERCENTILE,
    CONF_EXPENSIVE_PERCENTILE,
    REC_CHARGE_NOW_FULLY,
    REC_CHARGE_NOW_MINIMUM,
    REC_CHARGE_DAILY,
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
        return self._compute_strategy(prices, current_battery_pct)

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

    def _compute_strategy(
        self, prices: list[dict], current_battery_pct: float | None
    ) -> dict[str, Any]:
        now = dt_util.utcnow()

        battery_cap: float = self.config[CONF_BATTERY_CAPACITY]
        daily_usage: float = self.config[CONF_DAILY_USAGE]
        charge_rate: float = self.config[CONF_CHARGE_RATE]
        min_pct: float = self.config[CONF_MIN_CHARGE_PCT]
        target_pct: float = self.config[CONF_TARGET_CHARGE_PCT]
        cheap_pct_threshold: float = self.config.get(CONF_CHEAP_PERCENTILE, 30)
        expensive_pct_threshold: float = self.config.get(CONF_EXPENSIVE_PERCENTILE, 70)

        # If battery level unknown, assume at target
        if current_battery_pct is None:
            current_battery_pct = target_pct

        all_price_values = [s["price"] for s in prices]
        if not all_price_values:
            return self._unknown_result()

        sorted_prices_vals = sorted(all_price_values)
        cheap_threshold = self._percentile(sorted_prices_vals, cheap_pct_threshold)
        expensive_threshold = self._percentile(sorted_prices_vals, expensive_pct_threshold)
        avg_price = statistics.mean(all_price_values)

        # Label slots
        for slot in prices:
            slot["cheap"] = slot["price"] <= cheap_threshold
            slot["expensive"] = slot["price"] >= expensive_threshold

        # Current slot
        current_slot = next(
            (
                s for s in prices
                if s["datetime"] <= now < s["datetime"] + timedelta(minutes=30)
            ),
            None,
        )
        current_price = current_slot["price"] if current_slot else all_price_values[0]

        # Future slots
        future_slots = [s for s in prices if s["datetime"] > now]
        next_cheap_slot = next((s for s in future_slots if s["cheap"]), None)
        hours_until_cheap = (
            (next_cheap_slot["datetime"] - now).total_seconds() / 3600
            if next_cheap_slot
            else None
        )

        # Battery kWh
        current_kwh = (current_battery_pct / 100) * battery_cap
        target_kwh = (target_pct / 100) * battery_cap
        min_kwh = (min_pct / 100) * battery_cap
        kwh_needed = max(0.0, target_kwh - current_kwh)
        hours_to_charge = kwh_needed / charge_rate if charge_rate > 0 else 0.0

        # Battery range before hitting minimum
        usable_kwh = max(0.0, current_kwh - min_kwh)
        days_of_range = usable_kwh / daily_usage if daily_usage > 0 else 999
        hours_until_min = days_of_range * 24

        # Expensive period analysis over next 7 days
        slots_7day = [
            s for s in future_slots if s["datetime"] < now + timedelta(days=7)
        ]
        pct_expensive_7day = (
            sum(1 for s in slots_7day if s["expensive"]) / len(slots_7day)
            if slots_7day
            else 0.0
        )

        # Find longest consecutive expensive block (in days)
        long_expensive_days = self._longest_consecutive_expensive(future_slots)

        # Today's cheapest slot
        today_midnight = now.replace(hour=23, minute=59, second=59, microsecond=0)
        today_slots = [s for s in prices if now <= s["datetime"] <= today_midnight]
        cheapest_today = (
            min(today_slots, key=lambda x: x["price"]) if today_slots else None
        )

        # Cheapest slot across all future prices
        cheapest_upcoming = (
            min(future_slots, key=lambda x: x["price"]) if future_slots else None
        )

        is_cheap_now = current_price <= cheap_threshold
        is_expensive_now = current_price >= expensive_threshold

        # ---------------------------------------------------------------
        # Cost estimates
        # ---------------------------------------------------------------
        cost_now = round(current_price * kwh_needed / 100, 2)  # £
        cost_cheapest_today = round(
            (cheapest_today["price"] * kwh_needed / 100) if cheapest_today else cost_now,
            2,
        )
        estimated_savings = round(max(0.0, cost_now - cost_cheapest_today), 2)

        # ---------------------------------------------------------------
        # Strategy decision
        # ---------------------------------------------------------------
        recommendation, reason = self._decide_strategy(
            is_cheap_now=is_cheap_now,
            is_expensive_now=is_expensive_now,
            current_price=current_price,
            kwh_needed=kwh_needed,
            pct_expensive_7day=pct_expensive_7day,
            long_expensive_days=long_expensive_days,
            hours_until_cheap=hours_until_cheap,
            hours_until_min=hours_until_min,
            next_cheap_slot=next_cheap_slot,
            current_battery_pct=current_battery_pct,
            target_pct=target_pct,
            min_pct=min_pct,
            avg_price=avg_price,
            cheap_threshold=cheap_threshold,
        )

        charge_now_binary = recommendation in (
            REC_CHARGE_NOW_FULLY,
            REC_CHARGE_NOW_MINIMUM,
        )

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
            DATA_PCT_EXPENSIVE_7DAYS: round(pct_expensive_7day * 100, 0),
            DATA_CHEAPEST_TODAY_PRICE: round(cheapest_today["price"], 2) if cheapest_today else None,
            DATA_CHEAPEST_TODAY_START: cheapest_today["datetime"] if cheapest_today else None,
            DATA_CHEAPEST_UPCOMING_PRICE: round(cheapest_upcoming["price"], 2) if cheapest_upcoming else None,
            DATA_CHEAPEST_UPCOMING_START: cheapest_upcoming["datetime"] if cheapest_upcoming else None,
            DATA_LONG_EXPENSIVE_DAYS: round(long_expensive_days, 1),
            DATA_CHARGE_NOW_BINARY: charge_now_binary,
            DATA_PRICES: prices,
        }

    def _decide_strategy(
        self,
        *,
        is_cheap_now: bool,
        is_expensive_now: bool,
        current_price: float,
        kwh_needed: float,
        pct_expensive_7day: float,
        long_expensive_days: float,
        hours_until_cheap: float | None,
        hours_until_min: float,
        next_cheap_slot: dict | None,
        current_battery_pct: float,
        target_pct: float,
        min_pct: float,
        avg_price: float,
        cheap_threshold: float,
    ) -> tuple[str, str]:
        """
        Decision tree for charging recommendation.

        Priority order:
        1. Battery critically low -> charge now regardless of price
        2. Cheap now + long expensive period coming -> charge fully now
        3. Cheap now + prices generally stable -> charge daily
        4. Expensive now + cheap window soon + battery has range -> wait
        5. Expensive now + battery low -> charge minimum
        6. Moderate price + expensive period approaching -> charge minimum
        7. Default -> charge daily
        """

        # 1. Battery critically low - must charge now
        if current_battery_pct <= min_pct:
            return (
                REC_CHARGE_NOW_MINIMUM,
                (
                    f"Battery is at {current_battery_pct:.0f}%, at or below the minimum "
                    f"threshold of {min_pct:.0f}%. Charging now regardless of price "
                    f"({current_price:.1f}p/kWh)."
                ),
            )

        # 2. Cheap now + significant expensive period coming -> charge fully
        if is_cheap_now and kwh_needed > 0 and pct_expensive_7day >= 0.35 and long_expensive_days >= 1.5:
            return (
                REC_CHARGE_NOW_FULLY,
                (
                    f"Prices are cheap now ({current_price:.1f}p/kWh, below the "
                    f"{cheap_threshold:.1f}p/kWh cheap threshold). "
                    f"{pct_expensive_7day * 100:.0f}% of the next 7 days will be expensive, "
                    f"with up to {long_expensive_days:.1f} consecutive expensive days. "
                    f"Recommend charging to {target_pct:.0f}% now to avoid high future costs."
                ),
            )

        # 3. Cheap now + upcoming prices also reasonable -> charge daily
        if is_cheap_now:
            return (
                REC_CHARGE_DAILY,
                (
                    f"Prices are cheap now ({current_price:.1f}p/kWh) and upcoming prices "
                    f"are generally reasonable (only {pct_expensive_7day * 100:.0f}% of the "
                    f"next 7 days are expensive). Charge daily during the cheapest windows "
                    f"rather than filling up now."
                ),
            )

        # 4. Not cheap now, but cheap window is coming soon and battery can wait
        if (
            hours_until_cheap is not None
            and hours_until_cheap <= 8
            and hours_until_min > hours_until_cheap + 1
        ):
            return (
                REC_WAIT,
                (
                    f"Current price is {current_price:.1f}p/kWh. A cheaper window "
                    f"({next_cheap_slot['price']:.1f}p/kWh) starts in "
                    f"{hours_until_cheap:.1f} hours. Your battery has enough range "
                    f"to wait ({hours_until_min:.1f}h until minimum level)."
                ),
            )

        # 5. Cheap window is many hours away but battery can wait - still worth waiting
        if (
            hours_until_cheap is not None
            and hours_until_cheap <= 24
            and hours_until_min > hours_until_cheap + 2
            and not is_expensive_now
        ):
            return (
                REC_WAIT,
                (
                    f"Current price ({current_price:.1f}p/kWh) is above the cheap threshold "
                    f"({cheap_threshold:.1f}p/kWh). A cheaper window starts in "
                    f"{hours_until_cheap:.1f}h at {next_cheap_slot['price']:.1f}p/kWh. "
                    f"Battery range is sufficient to wait."
                ),
            )

        # 6. Expensive now, battery can't wait, or no cheap window soon -> charge minimum
        if is_expensive_now or (
            hours_until_cheap is not None and hours_until_min <= hours_until_cheap
        ):
            return (
                REC_CHARGE_NOW_MINIMUM,
                (
                    f"Prices are {'high' if is_expensive_now else 'moderate'} "
                    f"({current_price:.1f}p/kWh) and battery needs topping up "
                    f"(only {hours_until_min:.1f}h of range remaining before hitting "
                    f"the {min_pct:.0f}% minimum). Charging to minimum now."
                ),
            )

        # 7. Moderate prices, expensive period approaching -> top up to minimum
        if pct_expensive_7day >= 0.25:
            return (
                REC_CHARGE_NOW_MINIMUM,
                (
                    f"Current price is moderate ({current_price:.1f}p/kWh) with "
                    f"{pct_expensive_7day * 100:.0f}% of the next 7 days expected to be "
                    f"expensive. Topping up to minimum level as a precaution."
                ),
            )

        # 8. Default: charge daily during cheapest available windows
        return (
            REC_CHARGE_DAILY,
            (
                f"Prices are moderate ({current_price:.1f}p/kWh, average "
                f"{avg_price:.1f}p/kWh over 14 days). Continue your normal daily "
                f"charging schedule, targeting the cheapest overnight windows."
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
        }
