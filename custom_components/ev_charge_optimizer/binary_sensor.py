"""Binary sensor platform for EV Charge Optimizer."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, DATA_SCHEDULE
from .coordinator import EVChargeCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EVChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ChargeWindowSensor(coordinator, entry)])


class ChargeWindowSensor(CoordinatorEntity[EVChargeCoordinator], BinarySensorEntity):
    """ON during every planned charge slot; OFF otherwise.

    Registers point-in-time callbacks at each window boundary so the entity
    state changes at the exact 30-minute slot edge — safe to link to a charger switch.
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

    def _cancel_timers(self) -> None:
        for unsub in self._timer_unsubs:
            unsub()
        self._timer_unsubs.clear()

    def _reschedule_timers(self) -> None:
        self._cancel_timers()
        if not self.coordinator.data:
            return
        schedule = self.coordinator.data.get(DATA_SCHEDULE, [])
        now = dt_util.utcnow()
        seen: set[datetime] = set()
        for window in schedule:
            for boundary in (window["start"], window["end"]):
                if boundary > now and boundary not in seen:
                    seen.add(boundary)
                    self._timer_unsubs.append(
                        async_track_point_in_time(self.hass, self._boundary_reached, boundary)
                    )

    def _on_coordinator_update(self) -> None:
        self._reschedule_timers()
        self.async_write_ha_state()

    def _boundary_reached(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        if not self.coordinator.data:
            return False
        schedule = self.coordinator.data.get(DATA_SCHEDULE, [])
        now = dt_util.utcnow()
        return any(w["start"] <= now < w["end"] for w in schedule)

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        schedule = self.coordinator.data.get(DATA_SCHEDULE, [])
        return {
            "schedule": [
                {
                    "start": w["start"].isoformat(),
                    "end": w["end"].isoformat(),
                    "avg_price_p_kwh": w["avg_price"],
                    "duration_hours": w["duration_hours"],
                }
                for w in schedule
            ]
        }
