"""Config flow for EV Charge Optimizer."""
from __future__ import annotations

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_REGION,
    CONF_BATTERY_CAPACITY,
    CONF_DAILY_USAGE,
    CONF_CHARGE_RATE,
    CONF_TARGET_CHARGE_PCT,
    CONF_BATTERY_ENTITY,
    API_BASE_URL,
    OCTOPUS_REGIONS,
)


def _base_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_REGION, default=defaults.get(CONF_REGION, "G")): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=k, label=f"{k} – {v}")
                        for k, v in OCTOPUS_REGIONS.items()
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_BATTERY_CAPACITY, default=defaults.get(CONF_BATTERY_CAPACITY, 60.0)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5, max=200, step=0.5, unit_of_measurement="kWh", mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_DAILY_USAGE, default=defaults.get(CONF_DAILY_USAGE, 15.0)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=100, step=0.5, unit_of_measurement="kWh/day", mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_CHARGE_RATE, default=defaults.get(CONF_CHARGE_RATE, 7.4)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=22, step=0.1, unit_of_measurement="kW", mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_TARGET_CHARGE_PCT, default=defaults.get(CONF_TARGET_CHARGE_PCT, 80)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50, max=100, step=1, unit_of_measurement="%", mode=selector.NumberSelectorMode.SLIDER
                )
            ),
            vol.Optional(
                CONF_BATTERY_ENTITY, default=defaults.get(CONF_BATTERY_ENTITY, "")
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
        }
    )


async def _validate_region(hass: HomeAssistant, region: str) -> str | None:
    """Try fetching one day of data for the region. Returns error key or None."""
    url = f"{API_BASE_URL}/{region}?days=1"
    session = async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return "cannot_connect"
    except aiohttp.ClientError:
        return "cannot_connect"
    return None


class EVChargeOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EV Charge Optimizer."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            region = user_input[CONF_REGION]
            error = await _validate_region(self.hass, region)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(f"ev_charge_{region}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"EV Charge Optimizer ({OCTOPUS_REGIONS.get(region, region)})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_base_schema(user_input or {}),
            errors=errors,
            description_placeholders={
                "api_url": "https://agilepredict.com/api/",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> EVChargeOptionsFlow:
        return EVChargeOptionsFlow(config_entry)


class EVChargeOptionsFlow(config_entries.OptionsFlow):
    """Allow reconfiguration without re-adding the integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = dict(self._config_entry.data)
        current.update(self._config_entry.options)

        return self.async_show_form(
            step_id="init",
            data_schema=_base_schema(current),
            errors=errors,
        )
