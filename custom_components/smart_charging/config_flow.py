"""Config flow for the Smart Charging custom integration.

Set-up is two steps:

1. **Entities** — spot-price forecast sensor, SOC sensor and charger control
   entities.
2. **Parameters** — battery limits (min/max SOC), capacity, max charger
   power, price currency, the weekly 100 % boost and — when the car has
   regular away-times — the away-window plus daily consumption.

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
    CONF_CHARGER_ENERGY_SENSOR,
    CONF_CHARGER_MAX_KW,
    CONF_CHARGER_MODE_SENSOR,
    CONF_CHARGER_OPERATION_MODE,
    CONF_CHARGER_RESUME_BUTTON,
    CONF_CHARGER_STOP_BUTTON,
    CONF_CURRENCY,
    CONF_DAILY_CONSUMPTION_PCT,
    CONF_MAX_SOC,
    CONF_MIN_DAYS_BETWEEN_FULL,
    CONF_MIN_SOC,
    CONF_SOC_ENTITY,
    CONF_SOC_STALE_HOURS,
    CONF_SPOT_PRICES_ENTITY,
    CONF_USAGE_AWAY_END,
    CONF_USAGE_AWAY_START,
    CONF_USAGE_DAYS,
    CONF_USAGE_ENABLED,
    CONF_WEEKLY_FULL_CHARGE,
    CURRENCY_OPTIONS,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_CHARGER_MAX_KW,
    DEFAULT_CURRENCY,
    DEFAULT_DAILY_CONSUMPTION_PCT,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_DAYS_BETWEEN_FULL,
    DEFAULT_MIN_SOC,
    DEFAULT_SOC_STALE_HOURS,
    DEFAULT_USAGE_DAYS,
    DEFAULT_USAGE_ENABLED,
    DEFAULT_WEEKLY_FULL_CHARGE,
    DOMAIN,
    MODE_PLAN,
    USAGE_DAY_OPTIONS,
)

# OptionsFlowWithReload only exists on Home Assistant >= 2025.9; fall back to
# the plain OptionsFlow on older core so the integration always imports.
_OptionsFlowBase = getattr(
    config_entries, "OptionsFlowWithReload", config_entries.OptionsFlow
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
            vol.Optional(
                CONF_CHARGER_ENERGY_SENSOR,
                default=_entity_option(current.get(CONF_CHARGER_ENERGY_SENSOR, "")),
            ): selector.selector({"entity": {"domain": "sensor"}}),
        }
    )


def _params_schema(current: dict[str, Any]) -> vol.Schema:
    """Step 2: battery limits and preferences (no price thresholds).

    The away-window fields (days + from/to times) sit on the same page and
    are only required while "continuous usage" is on.
    """
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
                CONF_CHARGER_MAX_KW,
                default=_as_float(current.get(CONF_CHARGER_MAX_KW), DEFAULT_CHARGER_MAX_KW),
            ): selector.selector(
                {"number": {"mode": "box", "min": 1, "max": 22, "step": 0.1}}
            ),
            currency_field: selector.selector(
                {"select": {"options": list(CURRENCY_OPTIONS), "mode": "dropdown"}}
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
            vol.Required(
                CONF_USAGE_ENABLED,
                default=_as_bool(
                    current.get(CONF_USAGE_ENABLED), DEFAULT_USAGE_ENABLED
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_USAGE_DAYS,
                default=current.get(CONF_USAGE_DAYS, DEFAULT_USAGE_DAYS),
            ): selector.selector(
                {"select": {"options": list(USAGE_DAY_OPTIONS), "mode": "dropdown"}}
            ),
            vol.Optional(
                CONF_USAGE_AWAY_START,
                default=_entity_option(current.get(CONF_USAGE_AWAY_START, "")),
            ): selector.TimeSelector(),
            vol.Optional(
                CONF_USAGE_AWAY_END,
                default=_entity_option(current.get(CONF_USAGE_AWAY_END, "")),
            ): selector.TimeSelector(),
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
                CONF_SOC_STALE_HOURS,
                default=_as_float(
                    current.get(CONF_SOC_STALE_HOURS), DEFAULT_SOC_STALE_HOURS
                ),
            ): selector.selector(
                {"number": {"mode": "box", "min": 1, "max": 168, "step": 1}}
            ),
        }
    )


def _normalize_hm(value: Any) -> str:
    """Normalise a time string ("HH:MM", "HH:MM:SS") to "HH:MM"."""
    parts = str(value).strip().split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return ""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _validate_params(user_input: dict[str, Any]) -> dict[str, str]:
    """Validate the merged params form; returns a field -> error-key map.

    The away-window fields are only required (and must differ) when
    "continuous usage" is enabled; otherwise they are ignored.
    """
    if not _as_bool(user_input.get(CONF_USAGE_ENABLED), DEFAULT_USAGE_ENABLED):
        return {}
    return _validate_usage(user_input)


def _validate_usage(user_input: dict[str, Any]) -> dict[str, str]:
    """Validate the away-window fields; returns a field -> error-key map."""
    errors: dict[str, str] = {}
    start = _normalize_hm(user_input.get(CONF_USAGE_AWAY_START, ""))
    end = _normalize_hm(user_input.get(CONF_USAGE_AWAY_END, ""))
    if not start:
        errors[CONF_USAGE_AWAY_START] = "usage_required"
    if not end:
        errors[CONF_USAGE_AWAY_END] = "usage_required"
    if start and end and start == end:
        errors[CONF_USAGE_AWAY_END] = "usage_same_time"
    return errors


class SmartChargingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Smart Charging."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._entity_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Step 1: select entities (prices, SOC and charger controls)."""
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
        """Step 2: battery limits, prices, weekly boost and away-times."""
        errors: dict[str, str] = {}
        entity_data = getattr(self, "_entity_data", {})
        if user_input is not None:
            errors = _validate_params(user_input)
            if not errors:
                return self.async_create_entry(
                    title="Smart Charging",
                    data={**entity_data, **user_input, CONF_MODE: MODE_PLAN},
                )

        return self.async_show_form(
            step_id="params",
            data_schema=_params_schema(user_input or entity_data),
            errors=errors,
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return SmartChargingOptionsFlow(config_entry)


class SmartChargingOptionsFlow(_OptionsFlowBase):
    """Options flow — edit entities and parameters of an existing entry.

    ``_OptionsFlowBase`` is ``OptionsFlowWithReload`` on HA >= 2025.9 (saves
    reload the integration immediately) and the plain ``OptionsFlow`` on older
    core (changes take effect at the next refresh). It must not be combined
    with config-entry update listeners — this integration registers none.
    """

    automatic_reload = True

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._entity_data: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Start the options flow with the entities step."""
        return await self.async_step_entities(user_input)

    async def async_step_entities(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        # Prefill from the *effective* config (options override data).
        entry_data = {**self._config_entry.data, **self._config_entry.options}
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
                    CONF_SOC_STALE_HOURS: entry_data.get(
                        CONF_SOC_STALE_HOURS, DEFAULT_SOC_STALE_HOURS
                    ),
                    CONF_WEEKLY_FULL_CHARGE: entry_data.get(
                        CONF_WEEKLY_FULL_CHARGE, DEFAULT_WEEKLY_FULL_CHARGE
                    ),
                    CONF_MIN_DAYS_BETWEEN_FULL: entry_data.get(
                        CONF_MIN_DAYS_BETWEEN_FULL, DEFAULT_MIN_DAYS_BETWEEN_FULL
                    ),
                    # Currency is kept raw; an unset value falls back to the
                    # default when the params schema is built.
                    CONF_USAGE_ENABLED: entry_data.get(
                        CONF_USAGE_ENABLED, DEFAULT_USAGE_ENABLED
                    ),
                    CONF_USAGE_DAYS: entry_data.get(
                        CONF_USAGE_DAYS, DEFAULT_USAGE_DAYS
                    ),
                    CONF_USAGE_AWAY_START: entry_data.get(CONF_USAGE_AWAY_START, ""),
                    CONF_USAGE_AWAY_END: entry_data.get(CONF_USAGE_AWAY_END, ""),
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
                CONF_CHARGER_ENERGY_SENSOR,
            )
        }
        return self.async_show_form(
            step_id="entities", data_schema=_entities_schema(current), errors=errors
        )

    async def async_step_params(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entity_data = getattr(
            self,
            "_entity_data",
            {**self._config_entry.data, **self._config_entry.options},
        )
        if user_input is not None:
            errors = _validate_params(user_input)
            if not errors:
                data = dict(self._config_entry.data)
                data.update(entity_data)
                data.update(user_input)
                return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="params",
            data_schema=_params_schema(user_input or entity_data),
            errors=errors,
            last_step=True,
        )