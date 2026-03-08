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
    DATA_SCHEDULE_SLOTS,
    DATA_SCHEDULE_SESSIONS,
    DATA_SCHEDULE_ACTIVE,
    DATA_NEXT_START,
    DATA_NEXT_END,
    DATA_SUMMARY,
    EVENT_PRICES_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

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

        # Fire event once per day after 16:30 local — when next-day Agile prices land
        local_now = dt_util.as_local(dt_util.utcnow())
        after_430 = local_now.hour > 16 or (local_now.hour == 16 and local_now.minute >= 30)
        if after_430 and self._last_notification_date != local_now.date():
            self._last_notification_date = local_now.date()
            self.hass.bus.async_fire(
                EVENT_PRICES_UPDATED,
                {
                    "summary": result[DATA_SUMMARY],
                    "current_price": result[DATA_CURRENT_PRICE],
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
    # Strategy
    # ------------------------------------------------------------------

    def _compute(self, prices: list[dict]) -> dict[str, Any]:
        now = dt_util.utcnow()

        battery_cap: float = self.config[CONF_BATTERY_CAPACITY]
        charge_rate: float = self.config[CONF_CHARGE_RATE]
        daily_usage: float = float(self.overrides.get(CONF_DAILY_USAGE, self.config[CONF_DAILY_USAGE]))
        target_pct: float = float(self.overrides.get(CONF_TARGET_CHARGE_PCT, self.config[CONF_TARGET_CHARGE_PCT]))

        current_battery_pct = self._get_battery_pct() or target_pct

        # Current price
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
        schedule_slots = self._build_schedule(
            prices=prices,
            current_battery_pct=current_battery_pct,
            target_pct=target_pct,
            daily_usage=daily_usage,
            battery_cap=battery_cap,
            charge_rate=charge_rate,
            now=now,
        )

        # Group into display sessions
        sessions = _group_into_sessions(schedule_slots)

        # Is a charge slot active right now?
        active_slot = next(
            (s for s in schedule_slots if s["datetime"] <= now < s["datetime"] + timedelta(minutes=30)),
            None,
        )

        # Next upcoming slot
        next_slot = next((s for s in schedule_slots if s["datetime"] > now), None)

        summary = self._make_summary(sessions, active_slot, now)

        return {
            DATA_CURRENT_PRICE: round(current_price, 2) if current_price is not None else None,
            DATA_SCHEDULE_SLOTS: schedule_slots,
            DATA_SCHEDULE_SESSIONS: sessions,
            DATA_SCHEDULE_ACTIVE: active_slot is not None,
            DATA_NEXT_START: next_slot["datetime"] if next_slot else None,
            DATA_NEXT_END: next_slot["datetime"] + timedelta(minutes=30) if next_slot else None,
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
        """Build a 7-day list of individual 30-min charge slots.

        For each charge cycle:
          1. Calculate when the battery will hit the safety threshold
          2. Calculate how many slots are needed to reach target%
          3. Pick the N *cheapest individual slots* within the deadline window
             (not necessarily consecutive — charger turns on/off per slot)
          4. Repeat from target% after the last planned slot
        """
        if daily_usage <= 0 or charge_rate <= 0:
            return []

        safety_kwh = (_SAFETY_PCT / 100) * battery_cap
        target_kwh = (target_pct / 100) * battery_cap
        kwh_per_slot = charge_rate * 0.5  # energy added in one 30-min slot

        all_slots: list[dict] = []
        battery = current_battery_pct
        t = now
        horizon = now + timedelta(days=7)
        future_prices = [s for s in prices if s["datetime"] >= now]
        scheduled_dts: set[datetime] = set()  # avoid double-booking a slot

        for _ in range(20):
            current_kwh = (battery / 100) * battery_cap
            usable_kwh = max(0.0, current_kwh - safety_kwh)
            hours_of_range = (usable_kwh / daily_usage) * 24
            must_charge_by = t + timedelta(hours=hours_of_range)

            if must_charge_by > horizon:
                break  # battery lasts the full week

            # How many slots to go from safety% to target%?
            kwh_needed = max(0.0, target_kwh - safety_kwh)
            n_slots = max(1, math.ceil(kwh_needed / kwh_per_slot))

            # Candidate slots: after t, before deadline, not already scheduled
            candidates = [
                s for s in future_prices
                if s["datetime"] > t
                and s["datetime"] < must_charge_by
                and s["datetime"] not in scheduled_dts
            ]

            if len(candidates) < n_slots:
                # Deadline is tight — take what we can find
                candidates = [
                    s for s in future_prices
                    if s["datetime"] > t and s["datetime"] not in scheduled_dts
                ][:n_slots * 3]

            if not candidates:
                break

            # Pick the cheapest N individual slots
            chosen = sorted(candidates, key=lambda s: s["price"])[:n_slots]
            chosen.sort(key=lambda s: s["datetime"])

            for slot in chosen:
                scheduled_dts.add(slot["datetime"])
                all_slots.append(slot)

            # Next cycle starts after the last chosen slot
            last_slot = chosen[-1]
            t = last_slot["datetime"] + timedelta(minutes=30)
            battery = target_pct

            if t >= horizon:
                break

        all_slots.sort(key=lambda s: s["datetime"])
        return all_slots

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _make_summary(
        self,
        sessions: list[dict],
        active_slot: dict | None,
        now: datetime,
    ) -> str:
        if active_slot:
            # Find which session is active
            active_sess = next(
                (s for s in sessions if s["start"] <= now < s["end"]),
                None,
            )
            if active_sess:
                end_local = dt_util.as_local(active_sess["end"])
                return (
                    f"Charging now until {end_local.strftime('%H:%M')} "
                    f"({active_sess['avg_price']:.1f}p avg)"
                )
            return "Charging now"

        if not sessions:
            return "No charge sessions planned — check daily usage setting"

        upcoming = next((s for s in sessions if s["start"] > now), None)
        if not upcoming:
            return "All sessions complete for this week"

        start_local = dt_util.as_local(upcoming["start"])
        end_local = dt_util.as_local(upcoming["end"])
        diff = (start_local.date() - dt_util.as_local(now).date()).days
        day = "Today" if diff == 0 else "Tomorrow" if diff == 1 else start_local.strftime("%A")

        n_slots = upcoming["n_slots"]
        hours = n_slots * 0.5
        return (
            f"Next: {day} {start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')} "
            f"({n_slots} slots, {hours:.1f}h, {upcoming['avg_price']:.1f}p avg)"
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


# ------------------------------------------------------------------
# Grouping helper (module-level — used by both coordinator and sensors)
# ------------------------------------------------------------------

def _group_into_sessions(slots: list[dict]) -> list[dict]:
    """Group consecutive 30-min slots into display sessions.

    Two slots belong to the same session if they are exactly 30 min apart.
    Non-consecutive cheap slots appear as separate session entries.
    """
    if not slots:
        return []

    sessions = []
    run = [slots[0]]

    for slot in slots[1:]:
        gap = slot["datetime"] - run[-1]["datetime"]
        if gap == timedelta(minutes=30):
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
