"""Data coordinator for EV Smart Charge — fetches AgilePredict data and runs charging strategy."""
from __future__ import annotations

import logging
import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional
from urllib.request import urlopen
import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

from .const import (
    AGILE_PREDICT_API_BASE,
    API_TIMEOUT,
    CHARGE_FULL_IF_TONIGHT_CHEAPER_BY,
    CONF_BATTERY_CAPACITY,
    CONF_CHARGE_RATE,
    CONF_CHARGER_SWITCH,
    CONF_DEPARTURE_TIME,
    CONF_MIN_SOC,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    DEFAULT_FORECAST_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EXPENSIVE_PERIOD_DAYS,
    STRATEGY_CHARGE_FULL_TONIGHT,
    STRATEGY_CHARGE_TONIGHT,
    STRATEGY_MINIMUM_CHARGE,
    STRATEGY_NO_CHARGE_NEEDED,
    STRATEGY_WAIT_FOR_CHEAPER,
    TONIGHT_VS_FUTURE_TOLERANCE,
    WAIT_IF_TOMORROW_CHEAPER_BY,
)

_LOGGER = logging.getLogger(__name__)

# Maximum SOC history entries (keeps ~30 days at daily readings)
SOC_HISTORY_MAX = 60


@dataclass
class PriceSlot:
    """A single half-hour price slot."""

    start: datetime
    price_pence: float  # pence per kWh (inc. standing charge offset)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=30)

    def energy_kwh(self, charge_rate_kw: float) -> float:
        """Energy deliverable in this slot at the given charge rate."""
        return charge_rate_kw * 0.5

    def cost_pence(self, charge_rate_kw: float) -> float:
        """Cost of charging for this slot."""
        return self.price_pence * self.energy_kwh(charge_rate_kw)


@dataclass
class ChargingWindow:
    """A recommended charging window made of consecutive/selected price slots."""

    slots: list[PriceSlot]
    charge_rate_kw: float

    @property
    def start(self) -> datetime:
        return self.slots[0].start if self.slots else None

    @property
    def end(self) -> datetime:
        return self.slots[-1].end if self.slots else None

    @property
    def duration_hours(self) -> float:
        return len(self.slots) * 0.5

    @property
    def energy_kwh(self) -> float:
        return self.charge_rate_kw * self.duration_hours

    @property
    def total_cost_pence(self) -> float:
        return sum(s.cost_pence(self.charge_rate_kw) for s in self.slots)

    @property
    def avg_price_pence(self) -> float:
        if not self.slots:
            return 0.0
        return statistics.mean(s.price_pence for s in self.slots)


@dataclass
class ChargingStrategy:
    """Result of the charging strategy algorithm."""

    strategy_type: str
    recommendation: str
    target_soc: int
    charge_window: Optional[ChargingWindow] = None
    estimated_cost_pence: float = 0.0
    estimated_cost_gbp: float = 0.0
    cheapest_tonight_pence: Optional[float] = None
    forecast_cheapest_pence: Optional[float] = None
    forecast_cheapest_time: Optional[datetime] = None
    night_comparison: dict = field(default_factory=dict)
    reasoning: list[str] = field(default_factory=list)


class AgilePredict:
    """Thin wrapper around the AgilePredict REST API."""

    def __init__(self, region: str, days: int = DEFAULT_FORECAST_DAYS) -> None:
        self.region = region.upper()
        self.days = days

    def fetch_forecast(self) -> list[PriceSlot]:
        """Fetch price forecast and return sorted list of PriceSlots.

        AgilePredict API: GET https://agilepredict.com/api/{REGION}?days=N
        Returns JSON array where each element has:
          - name: forecast identifier
          - created_at: ISO timestamp
          - prices: dict of {ISO-datetime-str: price_pence}
        """
        url = f"{AGILE_PREDICT_API_BASE}/{self.region}?days={self.days}&forecast_count=1&high_low=False"
        _LOGGER.debug("Fetching AgilePredict forecast: %s", url)

        try:
            with urlopen(url, timeout=API_TIMEOUT) as response:
                raw = json.loads(response.read().decode())
        except Exception as exc:
            raise UpdateFailed(f"AgilePredict API error: {exc}") from exc

        if not raw or not isinstance(raw, list):
            raise UpdateFailed("Unexpected AgilePredict response format")

        # Take the most recent forecast (first element)
        forecast = raw[0]
        prices_dict: dict = forecast.get("prices", {})

        slots: list[PriceSlot] = []
        for dt_str, price in prices_dict.items():
            try:
                # Timestamps may be UTC ISO format
                slot_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                slots.append(PriceSlot(start=slot_dt, price_pence=float(price)))
            except (ValueError, TypeError) as exc:
                _LOGGER.warning("Skipping malformed price slot %s: %s", dt_str, exc)

        slots.sort(key=lambda s: s.start)
        _LOGGER.debug("Fetched %d price slots from AgilePredict", len(slots))
        return slots


class EVChargingOptimiser:
    """
    Core intelligent charging algorithm.

    Strategy decision tree
    ──────────────────────
    1. If current SOC >= target SOC                        → NO_CHARGE_NEEDED
    2. If current SOC <= min SOC                           → MINIMUM_CHARGE (force now, plan rest)
    3. If tonight's window is within tolerance of cheapest
       upcoming window                                     → CHARGE_TONIGHT
    4. If tonight is significantly cheaper than the next
       N days on average                                   → CHARGE_FULL_TONIGHT
    5. If tomorrow (or near future) is significantly
       cheaper than tonight                                → WAIT_FOR_CHEAPER
    6. Default                                             → CHARGE_TONIGHT
    """

    def __init__(
        self,
        battery_capacity_kwh: float,
        charge_rate_kw: float,
        target_soc: int,
        min_soc: int,
        departure_time_str: str,  # "HH:MM"
        soc_history: deque,
    ) -> None:
        self.battery_capacity = battery_capacity_kwh
        self.charge_rate = charge_rate_kw
        self.target_soc = target_soc
        self.min_soc = min_soc
        self.departure_time = self._parse_time(departure_time_str)
        self.soc_history = soc_history

    @staticmethod
    def _parse_time(time_str: str) -> time:
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))

    def _slots_for_night(
        self,
        slots: list[PriceSlot],
        reference_date: date,
    ) -> list[PriceSlot]:
        """Return slots between ~21:00 on reference_date and departure_time the following day."""
        # Night window: 21:00 the evening of reference_date → departure the next morning
        night_start = datetime.combine(reference_date, time(21, 0), tzinfo=dt_util.UTC)
        next_day = reference_date + timedelta(days=1)
        night_end = datetime.combine(next_day, self.departure_time, tzinfo=dt_util.UTC)
        return [s for s in slots if night_start <= s.start < night_end]

    def _cheapest_window(
        self,
        night_slots: list[PriceSlot],
        energy_needed_kwh: float,
    ) -> Optional[ChargingWindow]:
        """
        Find the cheapest non-contiguous set of slots that delivers energy_needed_kwh.
        Returns a ChargingWindow with slots sorted chronologically.
        """
        if not night_slots or energy_needed_kwh <= 0:
            return None

        slots_needed = math.ceil(energy_needed_kwh / (self.charge_rate * 0.5))
        slots_needed = min(slots_needed, len(night_slots))

        # Sort by price ascending → take cheapest N → re-sort chronologically
        cheapest = sorted(night_slots, key=lambda s: s.price_pence)[:slots_needed]
        cheapest.sort(key=lambda s: s.start)
        return ChargingWindow(slots=cheapest, charge_rate_kw=self.charge_rate)

    def _estimate_daily_usage_kwh(self) -> Optional[float]:
        """
        Estimate daily energy consumption from SOC history.
        Each history entry is (timestamp, soc_percent).
        """
        if len(self.soc_history) < 4:
            return None

        history = list(self.soc_history)
        # Only use entries where SOC dropped (not charging) to estimate usage
        drops: list[float] = []
        for i in range(1, len(history)):
            ts_prev, soc_prev = history[i - 1]
            ts_curr, soc_curr = history[i]
            elapsed_hours = (ts_curr - ts_prev).total_seconds() / 3600
            if soc_curr < soc_prev and 0 < elapsed_hours <= 26:
                soc_drop = soc_prev - soc_curr
                kwh_used = soc_drop / 100 * self.battery_capacity
                daily_equiv = kwh_used / elapsed_hours * 24
                drops.append(daily_equiv)

        if not drops:
            return None

        # Use median to ignore outliers
        return statistics.median(drops)

    def _days_current_soc_lasts(self, current_soc: float) -> Optional[float]:
        """Estimate how many days the current charge will last given usage history."""
        daily_kwh = self._estimate_daily_usage_kwh()
        if daily_kwh is None or daily_kwh <= 0:
            return None
        usable_kwh = max(0, current_soc - self.min_soc) / 100 * self.battery_capacity
        return usable_kwh / daily_kwh

    def compute_strategy(
        self,
        slots: list[PriceSlot],
        current_soc: float,
    ) -> ChargingStrategy:
        """
        Run the full strategy computation and return a ChargingStrategy.

        Parameters
        ----------
        slots       : full list of PriceSlots from AgilePredict (up to 14 days)
        current_soc : current battery state of charge in %
        """
        now = dt_util.utcnow()
        today = now.date()
        reasoning: list[str] = []

        # ── 1. Already at target ──────────────────────────────────────────────
        if current_soc >= self.target_soc:
            return ChargingStrategy(
                strategy_type=STRATEGY_NO_CHARGE_NEEDED,
                recommendation=f"Battery is at {current_soc:.0f}% — target {self.target_soc}% already met.",
                target_soc=self.target_soc,
                reasoning=["SOC ≥ target SOC"],
            )

        energy_to_target = (self.target_soc - current_soc) / 100 * self.battery_capacity
        energy_to_full = (100 - current_soc) / 100 * self.battery_capacity

        # ── Gather overnight windows ──────────────────────────────────────────
        # tonight = from now → tomorrow departure
        # future nights = next EXPENSIVE_PERIOD_DAYS nights
        tonight_slots = self._slots_for_night(slots, today)
        # If it's already past midnight, 'tonight' window starts from now
        tonight_slots = [s for s in tonight_slots if s.start >= now]

        future_night_windows: list[tuple[date, list[PriceSlot], Optional[ChargingWindow]]] = []
        for offset in range(1, EXPENSIVE_PERIOD_DAYS + 1):
            future_date = today + timedelta(days=offset)
            night_slots = self._slots_for_night(slots, future_date)
            if night_slots:
                window = self._cheapest_window(night_slots, energy_to_target)
                future_night_windows.append((future_date, night_slots, window))

        tonight_window = self._cheapest_window(tonight_slots, energy_to_target)
        tonight_full_window = self._cheapest_window(tonight_slots, energy_to_full)

        # Cheapest price available at all in the forecast
        all_prices = [s.price_pence for s in slots if s.start >= now]
        forecast_cheapest = min(all_prices) if all_prices else None
        forecast_cheapest_slot = (
            min((s for s in slots if s.start >= now), key=lambda s: s.price_pence)
            if all_prices
            else None
        )
        tonight_cheapest = (
            min(s.price_pence for s in tonight_slots) if tonight_slots else None
        )

        # Average price of cheapest windows on future nights
        future_avg_prices: list[float] = []
        for _, _, w in future_night_windows:
            if w:
                future_avg_prices.append(w.avg_price_pence)

        # Build night comparison dict for sensor attributes
        night_comparison: dict = {}
        for future_date, _, w in future_night_windows:
            night_comparison[future_date.isoformat()] = (
                round(w.avg_price_pence, 2) if w else None
            )

        # ── 2. Battery critically low — must charge now ───────────────────────
        if current_soc <= self.min_soc:
            reasoning.append(f"SOC {current_soc:.0f}% ≤ minimum {self.min_soc}%")
            # Still use the cheapest available window even if it's not tonight
            window = tonight_window or (
                future_night_windows[0][2] if future_night_windows else None
            )
            cost = window.total_cost_pence if window else 0.0
            return ChargingStrategy(
                strategy_type=STRATEGY_MINIMUM_CHARGE,
                recommendation=(
                    f"Battery is low ({current_soc:.0f}%). Starting minimum charge now. "
                    f"Optimal window: {window.start.strftime('%H:%M') if window else 'ASAP'}–"
                    f"{window.end.strftime('%H:%M') if window else 'N/A'}."
                ),
                target_soc=self.target_soc,
                charge_window=window,
                estimated_cost_pence=cost,
                estimated_cost_gbp=cost / 100,
                cheapest_tonight_pence=tonight_cheapest,
                forecast_cheapest_pence=forecast_cheapest,
                forecast_cheapest_time=forecast_cheapest_slot.start if forecast_cheapest_slot else None,
                night_comparison=night_comparison,
                reasoning=reasoning,
            )

        # ── 3–6. Strategic decision based on price comparisons ────────────────

        # Can we even charge tonight?
        if not tonight_window:
            # Tonight is empty (e.g. it's already past departure time)
            if future_night_windows:
                next_date, _, next_window = future_night_windows[0]
                reasoning.append("No slots available tonight — recommending next available night")
                cost = next_window.total_cost_pence if next_window else 0.0
                return ChargingStrategy(
                    strategy_type=STRATEGY_WAIT_FOR_CHEAPER,
                    recommendation=(
                        f"No charging window tonight. Next opportunity: "
                        f"{next_date.strftime('%A %d %b')} "
                        f"({next_window.start.strftime('%H:%M') if next_window else 'TBD'}–"
                        f"{next_window.end.strftime('%H:%M') if next_window else 'TBD'})."
                    ),
                    target_soc=self.target_soc,
                    charge_window=next_window,
                    estimated_cost_pence=cost,
                    estimated_cost_gbp=cost / 100,
                    cheapest_tonight_pence=tonight_cheapest,
                    forecast_cheapest_pence=forecast_cheapest,
                    forecast_cheapest_time=forecast_cheapest_slot.start if forecast_cheapest_slot else None,
                    night_comparison=night_comparison,
                    reasoning=reasoning,
                )
            return ChargingStrategy(
                strategy_type=STRATEGY_CHARGE_TONIGHT,
                recommendation="No price data available. Charging at default time.",
                target_soc=self.target_soc,
                reasoning=["No future slots available"],
            )

        tonight_avg = tonight_window.avg_price_pence
        reasoning.append(f"Tonight's avg optimal price: {tonight_avg:.2f}p/kWh")

        # ── Check if we should WAIT for a cheaper future night ────────────────
        if future_night_windows:
            # Find the single cheapest future night
            best_future = min(future_night_windows, key=lambda x: x[2].avg_price_pence if x[2] else float("inf"))
            best_future_date, _, best_future_window = best_future
            best_future_avg = best_future_window.avg_price_pence if best_future_window else float("inf")

            # How many days can current charge last?
            days_left = self._days_current_soc_lasts(current_soc)
            future_day_offset = (best_future_date - today).days

            reasoning.append(
                f"Best future night: {best_future_date} @ {best_future_avg:.2f}p/kWh "
                f"(day +{future_day_offset})"
            )
            if days_left is not None:
                reasoning.append(f"Estimated days on current charge: {days_left:.1f}")

            # Can we safely wait for that cheaper night?
            soc_can_wait = days_left is None or days_left >= future_day_offset

            if (
                soc_can_wait
                and best_future_avg < tonight_avg * (1 - WAIT_IF_TOMORROW_CHEAPER_BY)
                and future_day_offset <= 3  # Only wait up to 3 nights
            ):
                reasoning.append(
                    f"Future night is {(1 - best_future_avg/tonight_avg)*100:.1f}% cheaper — waiting"
                )
                cost = best_future_window.total_cost_pence if best_future_window else 0.0
                return ChargingStrategy(
                    strategy_type=STRATEGY_WAIT_FOR_CHEAPER,
                    recommendation=(
                        f"Cheaper prices forecast on {best_future_date.strftime('%A %d %b')} "
                        f"({best_future_avg:.1f}p vs tonight's {tonight_avg:.1f}p/kWh). "
                        f"Waiting — current charge ({current_soc:.0f}%) should last."
                    ),
                    target_soc=self.target_soc,
                    charge_window=best_future_window,
                    estimated_cost_pence=cost,
                    estimated_cost_gbp=cost / 100,
                    cheapest_tonight_pence=tonight_cheapest,
                    forecast_cheapest_pence=forecast_cheapest,
                    forecast_cheapest_time=forecast_cheapest_slot.start if forecast_cheapest_slot else None,
                    night_comparison=night_comparison,
                    reasoning=reasoning,
                )

        # ── Check if we should CHARGE FULL tonight to bank cheap energy ───────
        # Condition: tonight is significantly cheaper than the next N-day average
        if future_avg_prices:
            future_mean = statistics.mean(future_avg_prices)
            reasoning.append(f"Next {len(future_avg_prices)}-night average: {future_mean:.2f}p/kWh")

            if tonight_avg < future_mean * (1 - CHARGE_FULL_IF_TONIGHT_CHEAPER_BY):
                # Tonight is CHARGE_FULL_IF_TONIGHT_CHEAPER_BY% cheaper than coming days
                # → Recommend charging to 100% tonight
                cost = tonight_full_window.total_cost_pence if tonight_full_window else 0.0
                reasoning.append(
                    f"Tonight is {(1 - tonight_avg/future_mean)*100:.1f}% cheaper than next "
                    f"{len(future_avg_prices)}-night average — charge full"
                )
                return ChargingStrategy(
                    strategy_type=STRATEGY_CHARGE_FULL_TONIGHT,
                    recommendation=(
                        f"Tonight's prices ({tonight_avg:.1f}p/kWh) are significantly cheaper "
                        f"than the next {len(future_avg_prices)}-night average "
                        f"({future_mean:.1f}p/kWh). Charging to 100% tonight to bank cheap "
                        f"energy. Window: "
                        f"{tonight_full_window.start.strftime('%H:%M') if tonight_full_window else 'N/A'}–"
                        f"{tonight_full_window.end.strftime('%H:%M') if tonight_full_window else 'N/A'}."
                    ),
                    target_soc=100,
                    charge_window=tonight_full_window,
                    estimated_cost_pence=cost,
                    estimated_cost_gbp=cost / 100,
                    cheapest_tonight_pence=tonight_cheapest,
                    forecast_cheapest_pence=forecast_cheapest,
                    forecast_cheapest_time=forecast_cheapest_slot.start if forecast_cheapest_slot else None,
                    night_comparison=night_comparison,
                    reasoning=reasoning,
                )

        # ── Default: charge tonight to target ────────────────────────────────
        cost = tonight_window.total_cost_pence
        reasoning.append("Tonight's pricing is reasonable — charging to target")
        return ChargingStrategy(
            strategy_type=STRATEGY_CHARGE_TONIGHT,
            recommendation=(
                f"Charging to {self.target_soc}% tonight. "
                f"Optimal window: "
                f"{tonight_window.start.strftime('%H:%M')}–{tonight_window.end.strftime('%H:%M')} "
                f"@ avg {tonight_avg:.1f}p/kWh. "
                f"Estimated cost: £{cost/100:.2f}."
            ),
            target_soc=self.target_soc,
            charge_window=tonight_window,
            estimated_cost_pence=cost,
            estimated_cost_gbp=cost / 100,
            cheapest_tonight_pence=tonight_cheapest,
            forecast_cheapest_pence=forecast_cheapest,
            forecast_cheapest_time=forecast_cheapest_slot.start if forecast_cheapest_slot else None,
            night_comparison=night_comparison,
            reasoning=reasoning,
        )


class EVSmartChargeCoordinator(DataUpdateCoordinator):
    """
    Home Assistant DataUpdateCoordinator for EV Smart Charge.

    Fetches price forecasts from AgilePredict, reads current SOC from
    a HA entity, maintains SOC history, and runs the charging strategy.
    """

    def __init__(self, hass: HomeAssistant, config_entry) -> None:
        self.config_entry = config_entry
        cfg = {**config_entry.data, **config_entry.options}

        self.region: str = cfg[CONF_REGION]
        self.battery_capacity: float = float(cfg[CONF_BATTERY_CAPACITY])
        self.charge_rate: float = float(cfg[CONF_CHARGE_RATE])
        self.target_soc: int = int(cfg.get(CONF_TARGET_SOC, 80))
        self.min_soc: int = int(cfg.get(CONF_MIN_SOC, 20))
        self.departure_time: str = cfg.get(CONF_DEPARTURE_TIME, "07:30")
        self.soc_entity: Optional[str] = cfg.get(CONF_SOC_ENTITY)
        self.charger_switch: Optional[str] = cfg.get(CONF_CHARGER_SWITCH)
        scan_interval_s: int = int(cfg.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

        # Persistent SOC history: deque of (datetime, soc_percent)
        self._soc_history: deque = deque(maxlen=SOC_HISTORY_MAX)

        # Cached price slots
        self._price_slots: list[PriceSlot] = []

        # Last computed strategy
        self.strategy: Optional[ChargingStrategy] = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval_s),
        )

    def _get_current_soc(self) -> Optional[float]:
        """Read current SOC from the configured HA entity."""
        if not self.soc_entity:
            return None
        state = self.hass.states.get(self.soc_entity)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _record_soc(self, soc: float) -> None:
        """Add a SOC reading to the rolling history."""
        now = dt_util.utcnow()
        # Only record if enough time has passed since last reading (≥15 min)
        if self._soc_history:
            last_ts, _ = self._soc_history[-1]
            if (now - last_ts).total_seconds() < 900:
                return
        self._soc_history.append((now, soc))

    async def _async_update_data(self) -> dict:
        """Fetch data from AgilePredict and recompute charging strategy."""
        # Refresh options that may have changed
        cfg = {**self.config_entry.data, **self.config_entry.options}
        self.target_soc = int(cfg.get(CONF_TARGET_SOC, 80))
        self.min_soc = int(cfg.get(CONF_MIN_SOC, 20))
        self.departure_time = cfg.get(CONF_DEPARTURE_TIME, "07:30")
        self.soc_entity = cfg.get(CONF_SOC_ENTITY)

        # Fetch fresh price forecast
        api = AgilePredict(self.region)
        try:
            self._price_slots = await self.hass.async_add_executor_job(api.fetch_forecast)
        except UpdateFailed:
            # If fetch fails, re-use cached slots so sensors don't go unavailable
            if not self._price_slots:
                raise
            _LOGGER.warning("AgilePredict fetch failed — using cached slots")

        # Read current SOC
        current_soc = self._get_current_soc()
        if current_soc is not None:
            self._record_soc(current_soc)
        else:
            # Fall back to a neutral value so the algorithm still runs
            current_soc = 50.0
            _LOGGER.debug("SOC entity unavailable — using placeholder %.0f%%", current_soc)

        # Run strategy
        optimiser = EVChargingOptimiser(
            battery_capacity_kwh=self.battery_capacity,
            charge_rate_kw=self.charge_rate,
            target_soc=self.target_soc,
            min_soc=self.min_soc,
            departure_time_str=self.departure_time,
            soc_history=self._soc_history,
        )
        self.strategy = optimiser.compute_strategy(self._price_slots, current_soc)

        _LOGGER.info(
            "EV strategy: %s | SOC: %.0f%% | Window: %s–%s | Cost: £%.2f",
            self.strategy.strategy_type,
            current_soc,
            self.strategy.charge_window.start.strftime("%H:%M") if self.strategy.charge_window else "—",
            self.strategy.charge_window.end.strftime("%H:%M") if self.strategy.charge_window else "—",
            self.strategy.estimated_cost_gbp,
        )

        return {
            "strategy": self.strategy,
            "current_soc": current_soc,
            "price_slots": self._price_slots,
            "soc_history_len": len(self._soc_history),
        }

    @property
    def should_charge_now(self) -> bool:
        """
        Return True if we are currently inside the recommended charging window.
        Used by the binary sensor.
        """
        if self.strategy is None or self.strategy.charge_window is None:
            return False
        if self.strategy.strategy_type == STRATEGY_NO_CHARGE_NEEDED:
            return False
        now = dt_util.utcnow()
        return self.strategy.charge_window.start <= now < self.strategy.charge_window.end

    @property
    def current_price(self) -> Optional[float]:
        """Return the current half-hour price in p/kWh."""
        now = dt_util.utcnow()
        for slot in self._price_slots:
            if slot.start <= now < slot.end:
                return slot.price_pence
        return None

    @property
    def estimated_daily_usage(self) -> Optional[float]:
        """Return estimated daily usage in kWh from history."""
        if len(self._soc_history) < 4:
            return None
        history = list(self._soc_history)
        drops: list[float] = []
        for i in range(1, len(history)):
            ts_prev, soc_prev = history[i - 1]
            ts_curr, soc_curr = history[i]
            elapsed_hours = (ts_curr - ts_prev).total_seconds() / 3600
            if soc_curr < soc_prev and 0 < elapsed_hours <= 26:
                soc_drop = soc_prev - soc_curr
                kwh_used = soc_drop / 100 * self.battery_capacity
                daily_equiv = kwh_used / elapsed_hours * 24
                drops.append(daily_equiv)
        if not drops:
            return None
        return round(statistics.median(drops), 2)
