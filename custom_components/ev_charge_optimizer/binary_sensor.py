"""Binary sensor platform for EV Charge Optimizer."""
from __future__ import annotations

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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EVChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ChargeWindowSensor(coordinator, entry)])


class ChargeWindowSensor(CoordinatorEntity[EVChargeCoordinator], BinarySensorEntity):
    """ON during each individual planned 30-min charge slot; OFF otherwise.

    The sensor registers a point-in-time callback at the START and END of every
    scheduled slot so the state flips at the exact half-hour boundary.
    Link this directly to your car charger switch — it turns on for cheap slots
    and off for expensive ones, automatically throughout the week.
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
        self._timer_unsubs: list = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._on_coordinator_update)
        )
        self._reschedule_timers()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_timers()

    # ------------------------------------------------------------------

    def _cancel_timers(self) -> None:
        for unsub in self._timer_unsubs:
            unsub()
        self._timer_unsubs.clear()

    def _reschedule_timers(self) -> None:
        """Register a callback at the start AND end of every scheduled slot.

        Each slot is 30 min. We register two callbacks per slot:
          - at slot["datetime"]          → sensor turns ON
          - at slot["datetime"] + 30min  → sensor turns OFF
        This gives the charger an exact on/off signal at each half-hour boundary.
        """
        self._cancel_timers()
        if not self.coordinator.data:
            return
        slots = self.coordinator.data.get(DATA_SCHEDULE_SLOTS, [])
        now = dt_util.utcnow()
        seen: set[datetime] = set()
        for slot in slots:
            slot_start = slot["datetime"]
            slot_end = slot_start + timedelta(minutes=30)
            for boundary in (slot_start, slot_end):
                if boundary > now and boundary not in seen:
                    seen.add(boundary)
                    self._timer_unsubs.append(
                        async_track_point_in_time(self.hass, self._boundary_reached, boundary)
                    )

    def _on_coordinator_update(self) -> None:
        self._reschedule_timers()
        self.async_write_ha_state()

    def _boundary_reached(self, _now: datetime) -> None:
        """Called at exact slot boundary — write new state immediately."""
        self.async_write_ha_state()

    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        if not self.coordinator.data:
            return False
        slots = self.coordinator.data.get(DATA_SCHEDULE_SLOTS, [])
        now = dt_util.utcnow()
        return any(
            s["datetime"] <= now < s["datetime"] + timedelta(minutes=30)
            for s in slots
        )

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        slots = self.coordinator.data.get(DATA_SCHEDULE_SLOTS, [])
        now = dt_util.utcnow()
        return {
            "scheduled_slots": [
                {
                    "time": dt_util.as_local(s["datetime"]).strftime("%a %d %b %H:%M"),
                    "price_p_kwh": round(s["price"], 2),
                    "status": "active" if s["datetime"] <= now < s["datetime"] + timedelta(minutes=30) else "upcoming",
                }
                for s in slots
                if s["datetime"] + timedelta(minutes=30) > now
            ]
        }
