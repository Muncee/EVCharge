"""EV Charge Optimizer - Home Assistant integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .coordinator import EVChargeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "number"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EV Charge Optimizer from a config entry.

    We attempt an immediate API fetch so sensors have real data on first load.
    If the fetch fails (e.g. HA just booted and the network isn't ready yet),
    we still complete setup and let the coordinator's update_interval handle
    the retry — rather than raising ConfigEntryNotReady and forcing the user
    to wait for HA's exponential-backoff retry cycle (up to 30 min) or manually
    reloading the integration.

    Entities will show 'unavailable' until the first successful fetch, which
    the coordinator will attempt again after UPDATE_INTERVAL_MINUTES.
    """
    config = {**entry.data, **entry.options}
    coordinator = EVChargeCoordinator(hass, config)

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady as err:
        # API was unreachable at startup (common right after HA boot).
        # Schedule a retry in 5 minutes rather than waiting the full update
        # interval (30 min), so data appears quickly once the network is up.
        _LOGGER.warning(
            "EV Charge Optimizer: initial price fetch failed (%s). "
            "Will retry in 5 minutes — no action needed.",
            err,
        )

        async def _retry_fetch(_now=None) -> None:
            await coordinator.async_refresh()

        entry.async_on_unload(async_call_later(hass, 5 * 60, _retry_fetch))
        # Don't re-raise: complete setup so entities register and users can
        # see 'unavailable' state rather than a broken integration card.

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update (e.g. target %, daily usage changed in UI)."""
    await hass.config_entries.async_reload(entry.entry_id)
