"""Config flow for the Smart Charging custom integration.

Set-up is two steps:

1. **Entities** — spot-price forecast sensor, SOC sensor and charger control
   entities.
2. **Parameters** — battery limits (min/max SOC), capacity, daily
   consumption, max charger power, the weekly 100 % boost, an optional
   "ready by" deadline and currency.

There are NO price thresholds to configure: the integration derives "cheap"
from the forecast itself.
"""

from __future__ import annotations

from typing import Any, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGER_MAX_KW,
    CONF_CHARGER_MODE_SENSOR,
    CONF_CHARGER_OPERATION_MODE,
    CONF_CHARGER_RESUME_BUTTON,
    CONF_CHARGER_STOP_BUTTON,
    CONF_CURRENCY,
    CONF_DAILY_CONSUMPTION_PCT,
    CONF_DEADLINE_RESTART_MINUTES,
    CONF_DEADLINE_TIME,
    CONF_MAX_SOC,
    CONF_MIN_DAYS_BETWEEN_FULL,
    CONF_MIN_SOC,
    CONF_SOC_ENTITY,
    CONF_SPOT_PRICES_ENTITY,
    CONF_WEEKLY_FULL_CHARGE,
    CURRENCY_OPTIONS,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_CHARGER_MAX_KW,
    DEFAULT_CURRENCY,
    DEFAULT_DAILY_CONSUMPTION_PCT,
    DEFAULT_DEADLINE_RESTART_MINUTES,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_DAYS_BETWEEN_FULL,
    DEFAULT_MIN_SOC,
    DEFAULT_WEEKLY_FULL_CHARGE,
    DOMAIN,
    MODE_PLAN,
)

CONF_MODE = "mode"


def _auto_detect_spot_prices(entity_ids: list[str]) -> Optional[str]:
    matches = [
        eid
        for eid in entity_ids
        if eid.startswith("sensor.spot_price_") and eid.endswith("_forecast")
    ]
    matches.sort()
    return matches[0] if matches else None


def _auto_detect_charger_mode(entity_ids: list[str]) -> Optional[str]:
    matches = [
        eid
        for eid in entity_ids
        if eid.startswith("sensor.") and eid.endswith("_charger_mode")
    ]
    return matches[0] if matches else None


def _auto_detect_operation_mode(entity_ids: list[str]) -> Optional[str]:
    matches = [
        eid
        for eid in entity_ids
        if eid.startswith("switch.") and "operation_mode" in eid
    ]
    return matches[0] if matches else None


def _entity_option(value: Optional[str]) -> str:
    return value or ""


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _entities_schema(current: dict[str, Any]) -> vol.Schema:
    """Step 1: the entities the integration reads and controls."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SPOT_PRICES_ENTITY,
                default=_entity_option(current.get(CONF_SPOT_PRICES_ENTITY, "")),
            ): selector.selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                CONF_SOC_ENTITY,
                default=_entity_option(current.get(CONF_SOC_ENTITY, "")),
            ): selector.selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                CONF_CHARGER_OPERATION_MODE,
                default=_entity_option(current.get(CONF_CHARGER_OPERATION_MODE, "")),
            ): selector.selector({"entity": {"domain": "switch"}}),
            vol.Optional(
                CONF_CHARGER_RESUME_BUTTON,
                default=_entity_option(current.get(CONF_CHARGER_RESUME_BUTTON, "")),
            ): selector.selector({"entity": {"domain": "button"}}),
            vol.Optional(
                CONF_CHARGER_STOP_BUTTON,
                default=_entity_option(current.get(CONF_CHARGER_STOP_BUTTON, "")),
            ): selector.selector({"entity": {"domain": "button"}}),
            vol.Optional(
                CONF_CHARGER_MODE_SENSOR,
                default=_entity_option(current.get(CONF_CHARGER_MODE_SENSOR, "")),
            ): selector.selector({"entity": {"domain": "sensor"}}),
        }
    )


def _params_schema(current: dict[str, Any]) -> vol.Schema:
    """Step 2: battery limits and preferences (no price thresholds)."""
    currency_default = current.get(CONF_CURRENCY)
    currency_field = (
        vol.Required(CONF_CURRENCY, default=currency_default)
        if currency_default
        else vol.Required(CONF_CURRENCY, default=DEFAULT_CURRENCY)
    )
    return vol.Schema(
        {
            vol.Required(
                CONF_MIN_SOC,
                default=_as_float(current.get(CONF_MIN_SOC), DEFAULT_MIN_SOC),
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 100, "step": 1}}
            ),
            vol.Required(
                CONF_MAX_SOC,
                default=_as_float(current.get(CONF_MAX_SOC), DEFAULT_MAX_SOC),
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 100, "step": 1}}
            ),
            vol.Required(
                CONF_BATTERY_CAPACITY_KWH,
                default=_as_float(
                    current.get(CONF_BATTERY_CAPACITY_KWH), DEFAULT_BATTERY_CAPACITY_KWH
                ),
            ): selector.selector(
                {"number": {"mode": "box", "min": 1, "max": 200, "step": 0.5}}
            ),
            vol.Required(
                CONF_DAILY_CONSUMPTION_PCT,
                default=_as_float(
                    current.get(CONF_DAILY_CONSUMPTION_PCT),
                    DEFAULT_DAILY_CONSUMPTION_PCT,
                ),
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 100, "step": 1}}
            ),
            vol.Required(
                CONF_CHARGER_MAX_KW,
                default=_as_float(current.get(CONF_CHARGER_MAX_KW), DEFAULT_CHARGER_MAX_KW),
            ): selector.selector(
                {"number": {"mode": "box", "min": 1, "max": 22, "step": 0.1}}
            ),
            vol.Required(
                CONF_WEEKLY_FULL_CHARGE,
                default=_as_bool(
                    current.get(CONF_WEEKLY_FULL_CHARGE), DEFAULT_WEEKLY_FULL_CHARGE
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_MIN_DAYS_BETWEEN_FULL,
                default=_as_float(
                    current.get(CONF_MIN_DAYS_BETWEEN_FULL),
                    DEFAULT_MIN_DAYS_BETWEEN_FULL,
                ),
            ): selector.selector(
                {"number": {"mode": "box", "min": 1, "max": 30, "step": 1}}
            ),
            vol.Optional(
                CONF_DEADLINE_TIME,
                default=_entity_option(current.get(CONF_DEADLINE_TIME, "")),
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_DEADLINE_RESTART_MINUTES,
                default=_as_float(
                    current.get(CONF_DEADLINE_RESTART_MINUTES),
                    DEFAULT_DEADLINE_RESTART_MINUTES,
                ),
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 600, "step": 5}}
            ),
            currency_field: selector.selector(
                {"select": {"options": list(CURRENCY_OPTIONS), "mode": "dropdown"}}
            ),
        }
    )


class SmartChargingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Smart Charging."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._entity_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Step 1: select entities (prices, SOC, charger controls, deadline)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._entity_data = dict(user_input)
            return self.async_show_form(
                step_id="params",
                data_schema=_params_schema(self._entity_data),
                errors=errors,
                last_step=True,
            )

        entity_ids = list(self.hass.states.async_entity_ids())
        current = {
            CONF_SPOT_PRICES_ENTITY: _auto_detect_spot_prices(entity_ids),
            CONF_CHARGER_MODE_SENSOR: _auto_detect_charger_mode(entity_ids),
            CONF_CHARGER_OPERATION_MODE: _auto_detect_operation_mode(entity_ids),
        }
        return self.async_show_form(
            step_id="user",
            data_schema=_entities_schema(current),
            errors=errors,
        )

    async def async_step_params(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Step 2: battery limits, boost and currency."""
        errors: dict[str, str] = {}
        entity_data = getattr(self, "_entity_data", {})
        if user_input is not None:
            data = dict(entity_data)
            data.update(user_input)
            data[CONF_MODE] = MODE_PLAN
            return self.async_create_entry(title="Smart Charging", data=data)

        return self.async_show_form(
            step_id="params",
            data_schema=_params_schema(entity_data),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return SmartChargingOptionsFlow(config_entry)


class SmartChargingOptionsFlow(config_entries.OptionsFlow):
    """Options flow — edit entities and parameters of an existing entry."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Start the options flow with the entities step."""
        return await self.async_step_entities(user_input)

    async def async_step_entities(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry_data = dict(self._config_entry.data)
        if user_input is not None:
            self._entity_data = dict(user_input)
            self._entity_data.update(
                {
                    CONF_MIN_SOC: entry_data.get(CONF_MIN_SOC, DEFAULT_MIN_SOC),
                    CONF_MAX_SOC: entry_data.get(CONF_MAX_SOC, DEFAULT_MAX_SOC),
                    CONF_BATTERY_CAPACITY_KWH: entry_data.get(
                        CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH
                    ),
                    CONF_DAILY_CONSUMPTION_PCT: entry_data.get(
                        CONF_DAILY_CONSUMPTION_PCT, DEFAULT_DAILY_CONSUMPTION_PCT
                    ),
                    CONF_CHARGER_MAX_KW: entry_data.get(
                        CONF_CHARGER_MAX_KW, DEFAULT_CHARGER_MAX_KW
                    ),
                    CONF_WEEKLY_FULL_CHARGE: entry_data.get(
                        CONF_WEEKLY_FULL_CHARGE, DEFAULT_WEEKLY_FULL_CHARGE
                    ),
                    CONF_MIN_DAYS_BETWEEN_FULL: entry_data.get(
                        CONF_MIN_DAYS_BETWEEN_FULL, DEFAULT_MIN_DAYS_BETWEEN_FULL
                    ),
                    CONF_DEADLINE_TIME: entry_data.get(CONF_DEADLINE_TIME, ""),
                    CONF_DEADLINE_RESTART_MINUTES: entry_data.get(
                        CONF_DEADLINE_RESTART_MINUTES, DEFAULT_DEADLINE_RESTART_MINUTES
                    ),
                    # Keep the stored currency; falls back to the default in
                    # _params_schema when unset.
                    CONF_CURRENCY: entry_data.get(CONF_CURRENCY),
                }
            )
            return await self.async_step_params(None)

        current = {
            key: entry_data.get(key, "")
            for key in (
                CONF_SPOT_PRICES_ENTITY,
                CONF_SOC_ENTITY,
                CONF_CHARGER_OPERATION_MODE,
                CONF_CHARGER_RESUME_BUTTON,
                CONF_CHARGER_STOP_BUTTON,
                CONF_CHARGER_MODE_SENSOR,
            )
        }
        return self.async_show_form(
            step_id="entities", data_schema=_entities_schema(current), errors=errors
        )

    async def async_step_params(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entity_data = getattr(self, "_entity_data", dict(self._config_entry.data))
        if user_input is not None:
            data = dict(self._config_entry.data)
            data.update(entity_data)
            data.update(user_input)
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="params",
            data_schema=_params_schema(entity_data),
            errors=errors,
        )