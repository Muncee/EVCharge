"""Binary sensor platform for EV Charge Optimizer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    DATA_CHARGE_NOW_BINARY,
    DATA_IS_CHEAP_NOW,
    DATA_IS_EXPENSIVE_NOW,
    DATA_CHARGE_SCHEDULE,
)
from .coordinator import EVChargeCoordinator


@dataclass(frozen=True, kw_only=True)
class EVBinarySensorDescription(BinarySensorEntityDescription):
    data_key: str = ""


BINARY_SENSOR_DESCRIPTIONS: tuple[EVBinarySensorDescription, ...] = (
    EVBinarySensorDescription(
        key="charge_now",
        name="Charge EV Now",
        icon="mdi:car-electric",
        device_class=BinarySensorDeviceClass.RUNNING,
        data_key=DATA_CHARGE_NOW_BINARY,
    ),
    EVBinarySensorDescription(
        key="cheap_period",
        name="Cheap Electricity Period",
        icon="mdi:lightning-bolt-circle",
        data_key=DATA_IS_CHEAP_NOW,
    ),
    EVBinarySensorDescription(
        key="expensive_period",
        name="Expensive Electricity Period",
        icon="mdi:alert-circle",
        data_key=DATA_IS_EXPENSIVE_NOW,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EVChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        EVChargeBinarySensor(coordinator, entry, desc)
        for desc in BINARY_SENSOR_DESCRIPTIONS
    ]
    entities.append(EVChargeScheduledSensor(coordinator, entry))
    async_add_entities(entities)


class EVChargeBinarySensor(CoordinatorEntity[EVChargeCoordinator], BinarySensorEntity):
    """Binary sensor that reads a boolean from the EV Charge coordinator."""

    entity_description: EVBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EVChargeCoordinator,
        entry: ConfigEntry,
        description: EVBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "EV Charge Optimizer",
            "manufacturer": "AgilePredict",
            "model": "EV Charge Optimizer",
            "entry_type": "service",
        }

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return bool(self.coordinator.data.get(self.entity_description.data_key, False))


class EVChargeScheduledSensor(CoordinatorEntity[EVChargeCoordinator], BinarySensorEntity):
    """Binary sensor that is ON during planned charge windows.

    Registers point-in-time callbacks at each window boundary so the state
    changes at the exact 30-minute slot edge — not just when the coordinator
    next polls. This makes it safe to link directly to a smart charger switch.
    """

    _attr_has_entity_name = True
    _attr_name = "Scheduled Charge Window"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: EVChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_scheduled_charge_window"
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
        # Reschedule boundary timers whenever coordinator refreshes
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
        """Register a point-in-time callback at each schedule window boundary."""
        self._cancel_timers()
        if not self.coordinator.data:
            return
        schedule = self.coordinator.data.get(DATA_CHARGE_SCHEDULE, [])
        now = dt_util.utcnow()
        seen: set[datetime] = set()
        for window in schedule:
            for boundary in (window["start"], window["end"]):
                if boundary > now and boundary not in seen:
                    seen.add(boundary)
                    self._timer_unsubs.append(
                        async_track_point_in_time(
                            self.hass, self._boundary_reached, boundary
                        )
                    )

    def _on_coordinator_update(self) -> None:
        self._reschedule_timers()
        self.async_write_ha_state()

    def _boundary_reached(self, _now: datetime) -> None:
        """Called at exact window start/end — flip state immediately."""
        self.async_write_ha_state()

    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        if not self.coordinator.data:
            return False
        schedule = self.coordinator.data.get(DATA_CHARGE_SCHEDULE, [])
        now = dt_util.utcnow()
        return any(w["start"] <= now < w["end"] for w in schedule)

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        schedule = self.coordinator.data.get(DATA_CHARGE_SCHEDULE, [])
        return {
            "schedule": [
                {
                    "start": w["start"].isoformat(),
                    "end": w["end"].isoformat(),
                    "price_p_kwh": round(w["price"], 2),
                    "target_pct": w["target_pct"],
                }
                for w in schedule
            ]
        }
