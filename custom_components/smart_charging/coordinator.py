"""Coordinator for the Smart Charging integration.

Event-driven: listens to state changes on the configured input entities
(spot prices forecast, charger mode, deadline, mode select) plus a periodic
tick, throttles recomputes, and updates the plan. In Live mode it calls the
charger switch/button services when the desired next action changes.

Only services written: switch.turn_on/off on the operation-mode entity and
button.press on the resume/stop buttons. The integration NEVER writes
available_current.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import event as ha_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import helper
from .const import (
    CHARGER_CONNECTED_STATES,
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
    DEFAULT_BATTERY_NEED_KWH,
    DEFAULT_CHARGER_MAX_KW,
    DEFAULT_CURRENCY,
    DEFAULT_THRESHOLD_START,
    DEFAULT_THRESHOLD_STOP,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    MODE_LIVE,
    MODE_OFF,
    MODE_PLAN,
)

_LOGGER = logging.getLogger(__name__)


def resolve_options(entry: ConfigEntry) -> dict:
    """Resolve effective options (entry data first, options override)."""
    options = {
        CONF_SPOT_PRICES_ENTITY: entry.data.get(CONF_SPOT_PRICES_ENTITY, ""),
        CONF_CHARGER_OPERATION_MODE: entry.data.get(CONF_CHARGER_OPERATION_MODE, ""),
        CONF_CHARGER_RESUME_BUTTON: entry.data.get(CONF_CHARGER_RESUME_BUTTON, ""),
        CONF_CHARGER_STOP_BUTTON: entry.data.get(CONF_CHARGER_STOP_BUTTON, ""),
        CONF_CHARGER_MODE_SENSOR: entry.data.get(CONF_CHARGER_MODE_SENSOR, ""),
        CONF_DEADLINE_ENTITY: entry.data.get(CONF_DEADLINE_ENTITY, ""),
        CONF_THRESHOLD_START: entry.data.get(CONF_THRESHOLD_START, DEFAULT_THRESHOLD_START),
        CONF_THRESHOLD_STOP: entry.data.get(CONF_THRESHOLD_STOP, DEFAULT_THRESHOLD_STOP),
        CONF_CHARGER_MAX_KW: entry.data.get(CONF_CHARGER_MAX_KW, DEFAULT_CHARGER_MAX_KW),
        CONF_BATTERY_NEED_KWH: entry.data.get(CONF_BATTERY_NEED_KWH, DEFAULT_BATTERY_NEED_KWH),
        CONF_CURRENCY: entry.data.get(CONF_CURRENCY, DEFAULT_CURRENCY),
    }
    options.update(entry.options)
    return options


class SmartChargingCoordinator(DataUpdateCoordinator):
    """Recompute the charging plan and (in Live mode) act on it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._listeners: list[CALLBACK_TYPE] = []
        self._last_action_signature: Optional[str] = None
        self._last_summary: Optional[str] = None
        self._mode: str = str(entry.data.get("mode", MODE_PLAN))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES),
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def options(self) -> dict:
        return resolve_options(self._entry)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def now(self) -> datetime:
        return dt_util.utcnow()

    @property
    def plan(self) -> Optional[helper.Plan]:
        return self.data if isinstance(self.data, helper.Plan) else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_setup(self) -> None:
        """Subscribe to entity changes and the periodic tick."""
        for entity_id in self._entity_ids():
            if entity_id:
                self._listeners.append(
                    ha_event.async_track_state_change_event(
                        self.hass,
                        [entity_id],
                        self._on_entity_change,
                    )
                )
        self._listeners.append(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_START,
                lambda _: self.hass.async_create_task(self.async_request_refresh()),
            )
        )
        self.async_set_update_interval(
            timedelta(minutes=self._update_interval_minutes())
        )

    def async_unload(self) -> None:
        """Remove listeners."""
        for listener in self._listeners:
            listener()
        self._listeners = []

    @callback
    def _on_entity_change(self, _event: Any) -> None:
        """Queue a recompute when one of the watched entities changes."""
        self.hass.async_create_task(self.async_request_refresh())

    async def async_shutdown(self) -> None:
        self.async_unload()
        await super().async_shutdown()
# ------------------------------------------------------------------
    # Recompute
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> helper.Plan:
        opts = self.options

        price_hours = self._price_hours(opts.get(CONF_SPOT_PRICES_ENTITY, ""))
        connected = self._is_connected(opts.get(CONF_CHARGER_MODE_SENSOR, ""))
        deadline = self._deadline(opts.get(CONF_DEADLINE_ENTITY, ""))
        currency = str(opts.get(CONF_CURRENCY, DEFAULT_CURRENCY))

        plan = helper.compute_plan(
            price_hours=price_hours,
            connected=connected,
            mode=self._mode,
            threshold_start=float(opts.get(CONF_THRESHOLD_START, DEFAULT_THRESHOLD_START)),
            threshold_stop=float(opts.get(CONF_THRESHOLD_STOP, DEFAULT_THRESHOLD_STOP)),
            deadline=deadline,
            charger_max_kw=float(opts.get(CONF_CHARGER_MAX_KW, DEFAULT_CHARGER_MAX_KW)),
            battery_need_kwh=opts.get(CONF_BATTERY_NEED_KWH, DEFAULT_BATTERY_NEED_KWH),
            now=self.now,
            currency=currency,
        )

        if plan.summary != self._last_summary:
            self._logbook(plan)
            self._last_summary = plan.summary

        await self._maybe_act(plan)
        return plan

    async def async_set_mode(self, mode: str) -> None:
        """Set the operating mode and trigger a recompute."""
        if mode not in (MODE_OFF, MODE_PLAN, MODE_LIVE):
            _LOGGER.warning("Unknown mode %r ignored", mode)
            return
        self._mode = mode
        await self.async_request_refresh()

    def _entity_ids(self) -> list[str]:
        """All watched entity IDs from the config."""
        opts = self.options
        return [
            opts.get(CONF_SPOT_PRICES_ENTITY, ""),
            opts.get(CONF_CHARGER_MODE_SENSOR, ""),
            opts.get(CONF_DEADLINE_ENTITY, ""),
            opts.get(CONF_CHARGER_OPERATION_MODE, ""),
            opts.get(CONF_CHARGER_RESUME_BUTTON, ""),
            opts.get(CONF_CHARGER_STOP_BUTTON, ""),
        ]

    def _update_interval_minutes(self) -> int:
        return DEFAULT_UPDATE_INTERVAL_MINUTES
# ------------------------------------------------------------------
    # Input parsing
    # ------------------------------------------------------------------
    def _price_hours(self, entity_id: str) -> list[helper.PriceHour]:
        """Extract hourly prices from the spot prices forecast sensor."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return []
        hours = state.attributes.get("hours") or []
        currency = str(self.options.get(CONF_CURRENCY, DEFAULT_CURRENCY))
        parsed = helper.parse_price_hours(hours, currency=currency)
        if hours and not parsed:
            _LOGGER.warning(
                "No hourly prices parsed from %s with currency %s — "
                "expected prices under 'currency_kwh' or '%s_kwh'",
                entity_id,
                currency,
                currency.lower(),
            )
        return parsed

    def _is_connected(self, entity_id: str) -> bool:
        """Map the charger mode sensor to a boolean 'connected'."""
        if not entity_id:
            # No charger mode sensor configured: assume connected.
            return True
        state = self.hass.states.get(entity_id)
        if state is None:
            return True
        return state.state in CHARGER_CONNECTED_STATES

    def _deadline(self, entity_id: str) -> Optional[datetime]:
        """Read the deadline from an input_datetime / datetime entity."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        value = state.state
        if not value or value in ("unknown", "unavailable"):
            return None
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            return None
        return parsed
# ------------------------------------------------------------------
    # Live actions & logbook
    # ------------------------------------------------------------------
    async def _maybe_act(self, plan: helper.Plan) -> None:
        """In Live mode, drive the charger toward the desired next action."""
        if self._mode != MODE_LIVE:
            return
        na = plan.next_action
        if na is None:
            return

        signature = f"{na.action}|{na.at and na.at.isoformat() or ''}|{na.reason}"
        if signature == self._last_action_signature:
            return
        self._last_action_signature = signature

        opts = self.options
        op_mode_entity = opts.get(CONF_CHARGER_OPERATION_MODE, "")
        resume_button = opts.get(CONF_CHARGER_RESUME_BUTTON, "")
        stop_button = opts.get(CONF_CHARGER_STOP_BUTTON, "")

        if na.action == "resume":
            if op_mode_entity:
                await self.hass.services.async_call(
                    "switch", "turn_on", {"entity_id": op_mode_entity}, blocking=True
                )
            if resume_button:
                await self.hass.services.async_call(
                    "button", "press", {"entity_id": resume_button}, blocking=True
                )
            self._logbook_action("resume_charging")
        elif na.action == "stop":
            if stop_button:
                await self.hass.services.async_call(
                    "button", "press", {"entity_id": stop_button}, blocking=True
                )
            if op_mode_entity:
                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": op_mode_entity}, blocking=True
                )
            self._logbook_action("stop_charging")

    def _logbook(self, plan: helper.Plan) -> None:
        """Write a plan recompute line to the logbook."""
        message = f"PLAN ({self._mode}): {plan.summary}"
        if plan.next_action and plan.next_action.action in ("resume", "stop"):
            at_str = (
                plan.next_action.at.isoformat() if plan.next_action.at else "nu"
            )
            message += (
                f" — {plan.next_action.action} {at_str} "
                f"({plan.next_action.reason})"
            )
        self.hass.bus.async_fire(
            "logbook",
            {
                "name": DOMAIN,
                "message": message,
                "domain": DOMAIN,
            },
        )
        self.hass.create_task(
            self.hass.services.async_call(
                "logbook",
                "log",
                {"name": DOMAIN, "message": message, "domain": DOMAIN},
            )
        )

    def _logbook_action(self, action: str) -> None:
        """Write an action line to the logbook (Live mode only)."""
        self.hass.async_create_task(
            self.hass.services.async_call(
                "logbook",
                "log",
                {
                    "name": DOMAIN,
                    "message": f"ÅTGÄRD: {action}",
                    "domain": DOMAIN,
                },
            )
        )