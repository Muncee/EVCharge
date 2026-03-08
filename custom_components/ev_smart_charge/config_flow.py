"""Config flow for EV Smart Charge integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_CHARGE_RATE,
    CONF_CHARGER_SWITCH,
    CONF_DEPARTURE_TIME,
    CONF_MIN_SOC,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_SOC_ENTITY,
    CONF_TARGET_SOC,
    DEFAULT_DEPARTURE_TIME,
    DEFAULT_MIN_SOC,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TARGET_SOC,
    DOMAIN,
    REGION_CODES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="EV Smart Charge"): str,
        vol.Required(CONF_REGION): vol.In(
            {code: f"{code} — {name}" for code, name in REGION_CODES.items()}
        ),
        vol.Required(CONF_BATTERY_CAPACITY): vol.All(
            vol.Coerce(float), vol.Range(min=5, max=200)
        ),
        vol.Required(CONF_CHARGE_RATE): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=50)
        ),
        vol.Required(CONF_TARGET_SOC, default=DEFAULT_TARGET_SOC): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=100)
        ),
        vol.Required(CONF_MIN_SOC, default=DEFAULT_MIN_SOC): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=50)
        ),
        vol.Required(CONF_DEPARTURE_TIME, default=DEFAULT_DEPARTURE_TIME): str,
        vol.Optional(CONF_SOC_ENTITY): str,
        vol.Optional(CONF_CHARGER_SWITCH): str,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TARGET_SOC, default=DEFAULT_TARGET_SOC): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=100)
        ),
        vol.Required(CONF_MIN_SOC, default=DEFAULT_MIN_SOC): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=50)
        ),
        vol.Required(CONF_DEPARTURE_TIME, default=DEFAULT_DEPARTURE_TIME): str,
        vol.Optional(CONF_SOC_ENTITY): str,
        vol.Optional(CONF_CHARGER_SWITCH): str,
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=300, max=86400)
        ),
    }
)


def _validate_time(time_str: str) -> bool:
    """Validate HH:MM time format."""
    try:
        parts = time_str.split(":")
        if len(parts) != 2:
            return False
        hour, minute = int(parts[0]), int(parts[1])
        return 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, AttributeError):
        return False


class EVSmartChargeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EV Smart Charge."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate departure time format
            if not _validate_time(user_input.get(CONF_DEPARTURE_TIME, "")):
                errors[CONF_DEPARTURE_TIME] = "invalid_time_format"
            elif user_input[CONF_MIN_SOC] >= user_input[CONF_TARGET_SOC]:
                errors[CONF_MIN_SOC] = "min_soc_too_high"
            else:
                # Test API connectivity
                from .coordinator import AgilePredict

                try:
                    api = AgilePredict(user_input[CONF_REGION])
                    await self.hass.async_add_executor_job(api.fetch_forecast)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.error("Failed to connect to AgilePredict API: %s", exc)
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=user_input[CONF_NAME],
                        data=user_input,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "api_url": "https://agilepredict.com/api",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EVSmartChargeOptionsFlow:
        """Return the options flow handler."""
        return EVSmartChargeOptionsFlow(config_entry)


class EVSmartChargeOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for EV Smart Charge."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle options step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not _validate_time(user_input.get(CONF_DEPARTURE_TIME, "")):
                errors[CONF_DEPARTURE_TIME] = "invalid_time_format"
            elif user_input[CONF_MIN_SOC] >= user_input[CONF_TARGET_SOC]:
                errors[CONF_MIN_SOC] = "min_soc_too_high"
            else:
                return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options or self.config_entry.data
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TARGET_SOC,
                        default=current.get(CONF_TARGET_SOC, DEFAULT_TARGET_SOC),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=100)),
                    vol.Required(
                        CONF_MIN_SOC,
                        default=current.get(CONF_MIN_SOC, DEFAULT_MIN_SOC),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=50)),
                    vol.Required(
                        CONF_DEPARTURE_TIME,
                        default=current.get(CONF_DEPARTURE_TIME, DEFAULT_DEPARTURE_TIME),
                    ): str,
                    vol.Optional(
                        CONF_SOC_ENTITY,
                        description={"suggested_value": current.get(CONF_SOC_ENTITY, "")},
                    ): str,
                    vol.Optional(
                        CONF_CHARGER_SWITCH,
                        description={"suggested_value": current.get(CONF_CHARGER_SWITCH, "")},
                    ): str,
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=300, max=86400)),
                }
            ),
            errors=errors,
        )
