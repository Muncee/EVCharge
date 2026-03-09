"""Binary sensor platform for EV Charge Optimizer."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, DATA_SCHEDULE_SLOTS
from .coordinator import EVChargeCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EVChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ChargeWindowSensor(coordinator, entry)])


class ChargeWindowSensor(CoordinatorEntity[EVChargeCoordinator], BinarySensorEntity):
    """ON during each individual planned 30-min charge slot; OFF otherwise.

    Design notes
    ────────────
    The sensor owns a STABLE copy of the slot list (_own_slots).  This list is
    only replaced by coordinator updates when the sensor is NOT currently ON.
    This stops a coordinator refresh mid-session from prematurely ending the
    charge (which happened because the rising battery level caused the planner
    to drop the remaining session slots from the schedule).

    Timers fire at the exact start/end boundary of every slot so the state
    flips at the right half-hour regardless of coordinator poll timing.
    """

    _attr_has_entity_name = True
    _attr_name = "Charge Window"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: EVChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_charge_window"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EV Charge Optimizer",
            "manufacturer": "AgilePredict",
            "model": "EV Charge Optimizer",
            "entry_type": "service",
        }
        self._own_slots: list[dict] = []   # stable copy — only updated when OFF
        self._timer_unsubs: list = []

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Populate slot list immediately from whatever data the coordinator has
        self._sync_slots_from_coordinator()
        self._reschedule_timers()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_timers()

    # ------------------------------------------------------------------
    # Coordinator updates
    # ------------------------------------------------------------------

    def _handle_coordinator_update(self) -> None:
        """Called on every coordinator refresh (overrides CoordinatorEntity default).

        KEY RULE: if a charge session is currently active, do NOT swap the
        slot list — the rising battery during charging would cause the planner
        to drop the remaining session slots and turn us OFF prematurely.

        Only reschedule timers when the slot list is actually updated.
        """
        now = dt_util.utcnow()
        currently_on = self._is_on_at(now, self._own_slots)

        if not currently_on:
            # Safe to update — no active session
            changed = self._sync_slots_from_coordinator()
            if changed:
                self._reschedule_timers()

        self.async_write_ha_state()

    def _sync_slots_from_coordinator(self) -> bool:
        """Pull latest slots from coordinator data.  Returns True if changed."""
        new_slots = (
            self.coordinator.data.get(DATA_SCHEDULE_SLOTS, [])
            if self.coordinator.data
            else []
        )
        if new_slots != self._own_slots:
            self._own_slots = list(new_slots)
            return True
        return False

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _cancel_timers(self) -> None:
        for unsub in self._timer_unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._timer_unsubs.clear()

    def _reschedule_timers(self) -> None:
        """Register a callback at the START and END of every upcoming slot.

        At each boundary we write state, which re-evaluates `is_on` so the
        sensor flips at the exact half-hour mark regardless of coordinator
        poll timing.  After the boundary fires we also allow the coordinator's
        slot list to be refreshed (if the session just ended).
        """
        self._cancel_timers()
        now = dt_util.utcnow()
        seen: set[datetime] = set()

        for slot in self._own_slots:
            slot_start: datetime = slot["datetime"]
            slot_end: datetime = slot_start + timedelta(minutes=30)

            for boundary in (slot_start, slot_end):
                if boundary > now and boundary not in seen:
                    seen.add(boundary)
                    self._timer_unsubs.append(
                        async_track_point_in_time(
                            self.hass, self._boundary_reached, boundary
                        )
                    )

    def _boundary_reached(self, _now: datetime) -> None:
        """Called at exact slot boundary.

        After each boundary, if the session just ended (is_on → False) we
        allow the coordinator's fresher slot list in.
        """
        now = dt_util.utcnow()
        if not self._is_on_at(now, self._own_slots):
            # Session just ended — safe to pull in updated coordinator data
            self._sync_slots_from_coordinator()
            self._reschedule_timers()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @staticmethod
    def _is_on_at(now: datetime, slots: list[dict]) -> bool:
        return any(
            s["datetime"] <= now < s["datetime"] + timedelta(minutes=30)
            for s in slots
        )

    @property
    def is_on(self) -> bool:
        return self._is_on_at(dt_util.utcnow(), self._own_slots)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict:
        now = dt_util.utcnow()
        return {
            "scheduled_slots": [
                {
                    "time": dt_util.as_local(s["datetime"]).strftime("%a %d %b %H:%M"),
                    "price_p_kwh": round(s["price"], 2),
                    "status": (
                        "active"
                        if s["datetime"] <= now < s["datetime"] + timedelta(minutes=30)
                        else "upcoming"
                    ),
                }
                for s in self._own_slots
                if s["datetime"] + timedelta(minutes=30) > now
            ],
            "session_locked": self._is_on_at(now, self._own_slots),
        }
