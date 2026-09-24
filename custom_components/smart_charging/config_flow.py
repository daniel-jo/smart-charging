"""Config flow for the Smart Charging custom integration."""

from __future__ import annotations

from typing import Any, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_NEED_KWH,
    CONF_CHARGER_MAX_KW,
    CONF_CHARGER_MODE_SENSOR,
    CONF_CHARGER_OPERATION_MODE,
    CONF_CHARGER_RESUME_BUTTON,
    CONF_CHARGER_STOP_BUTTON,
    CONF_CURRENCY,
    CONF_DEADLINE_ENTITY,
    CONF_SPOT_PRICES_ENTITY,
    CONF_THRESHOLD_START,
    CONF_THRESHOLD_STOP,
    CURRENCY_OPTIONS,
    DEFAULT_BATTERY_NEED_KWH,
    DEFAULT_CHARGER_MAX_KW,
    DEFAULT_THRESHOLD_START,
    DEFAULT_THRESHOLD_STOP,
    DOMAIN,
    MODE_PLAN,
)

CONF_MODE = "mode"


def _auto_detect_spot_prices(entity_ids: list[str]) -> Optional[str]:
    matches = [
        eid for eid in entity_ids
        if eid.startswith("sensor.spot_prices_") and eid.endswith("_forecast")
    ]
    matches.sort()
    return matches[0] if matches else None


def _auto_detect_charger_mode(entity_ids: list[str]) -> Optional[str]:
    matches = [eid for eid in entity_ids if eid.startswith("sensor.") and eid.endswith("_charger_mode")]
    return matches[0] if matches else None


def _auto_detect_operation_mode(entity_ids: list[str]) -> Optional[str]:
    matches = [eid for eid in entity_ids if eid.startswith("switch.") and "operation_mode" in eid]
    return matches[0] if matches else None


def _entity_option(value: Optional[str]) -> str:
    return value or ""


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _params_schema(current: dict[str, Any]) -> vol.Schema:
    """Build the parameter schema with defaults from current values."""
    currency_default = current.get(CONF_CURRENCY)
    currency_field = (
        vol.Required(CONF_CURRENCY, default=currency_default)
        if currency_default
        else vol.Required(CONF_CURRENCY)
    )
    return vol.Schema(
        {
            vol.Required(
                CONF_THRESHOLD_START,
                default=_as_float(current.get(CONF_THRESHOLD_START), DEFAULT_THRESHOLD_START),
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 5, "step": 0.01}}
            ),
            vol.Required(
                CONF_THRESHOLD_STOP,
                default=_as_float(current.get(CONF_THRESHOLD_STOP), DEFAULT_THRESHOLD_STOP),
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 5, "step": 0.01}}
            ),
            vol.Required(
                CONF_CHARGER_MAX_KW,
                default=_as_float(current.get(CONF_CHARGER_MAX_KW), DEFAULT_CHARGER_MAX_KW),
            ): selector.selector(
                {"number": {"mode": "box", "min": 1, "max": 22, "step": 0.1}}
            ),
            vol.Optional(
                CONF_BATTERY_NEED_KWH,
                default=_as_float(current.get(CONF_BATTERY_NEED_KWH), DEFAULT_BATTERY_NEED_KWH),
            ): selector.selector(
                {"number": {"mode": "box", "min": 0, "max": 150, "step": 0.1}}
            ),
            currency_field: selector.selector(
                {"select": {"options": list(CURRENCY_OPTIONS), "mode": "dropdown"}}
            ),
        }
    )
class SmartChargingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Smart Charging."""

    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Step 1: select entities (spot prices, charger controls, deadline)."""
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
        spot = _auto_detect_spot_prices(entity_ids)
        charger_mode = _auto_detect_charger_mode(entity_ids)
        operation_mode = _auto_detect_operation_mode(entity_ids)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SPOT_PRICES_ENTITY,
                    default=_entity_option(spot),
                ): selector.selector(
                    {"entity": {"domain": "sensor"}}
                ),
                vol.Optional(
                    CONF_CHARGER_OPERATION_MODE,
                    default=_entity_option(operation_mode),
                ): selector.selector({"entity": {"domain": "switch"}}),
                vol.Optional(
                    CONF_CHARGER_RESUME_BUTTON
                ): selector.selector({"entity": {"domain": "button"}}),
                vol.Optional(
                    CONF_CHARGER_STOP_BUTTON
                ): selector.selector({"entity": {"domain": "button"}}),
                vol.Optional(
                    CONF_CHARGER_MODE_SENSOR,
                    default=_entity_option(charger_mode),
                ): selector.selector(
                    {"entity": {"domain": "sensor"}}
                ),
                vol.Optional(CONF_DEADLINE_ENTITY): selector.selector(
                    {"entity": {"domain": ["input_datetime", "datetime"]}}
                ),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_params(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
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
                    CONF_THRESHOLD_START: entry_data.get(CONF_THRESHOLD_START, DEFAULT_THRESHOLD_START),
                    CONF_THRESHOLD_STOP: entry_data.get(CONF_THRESHOLD_STOP, DEFAULT_THRESHOLD_STOP),
                    CONF_CHARGER_MAX_KW: entry_data.get(CONF_CHARGER_MAX_KW, DEFAULT_CHARGER_MAX_KW),
                    CONF_BATTERY_NEED_KWH: entry_data.get(CONF_BATTERY_NEED_KWH, DEFAULT_BATTERY_NEED_KWH),
                    # Keep the stored currency (no default: legacy entries that
                    # predate the setting are forced to choose one).
                    CONF_CURRENCY: entry_data.get(CONF_CURRENCY),
                }
            )
            return await self.async_step_params(None)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SPOT_PRICES_ENTITY,
                    default=entry_data.get(CONF_SPOT_PRICES_ENTITY, ""),
                ): selector.selector({"entity": {"domain": "sensor"}}),
                vol.Optional(
                    CONF_CHARGER_OPERATION_MODE,
                    default=entry_data.get(CONF_CHARGER_OPERATION_MODE, ""),
                ): selector.selector({"entity": {"domain": "switch"}}),
                vol.Optional(
                    CONF_CHARGER_RESUME_BUTTON,
                    default=entry_data.get(CONF_CHARGER_RESUME_BUTTON, ""),
                ): selector.selector({"entity": {"domain": "button"}}),
                vol.Optional(
                    CONF_CHARGER_STOP_BUTTON,
                    default=entry_data.get(CONF_CHARGER_STOP_BUTTON, ""),
                ): selector.selector({"entity": {"domain": "button"}}),
                vol.Optional(
                    CONF_CHARGER_MODE_SENSOR,
                    default=entry_data.get(CONF_CHARGER_MODE_SENSOR, ""),
                ): selector.selector({"entity": {"domain": "sensor"}}),
                vol.Optional(
                    CONF_DEADLINE_ENTITY,
                    default=entry_data.get(CONF_DEADLINE_ENTITY, ""),
                ): selector.selector(
                    {"entity": {"domain": ["input_datetime", "datetime"]}}
                ),
            }
        )
        return self.async_show_form(step_id="entities", data_schema=schema, errors=errors)

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