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
import re
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
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGER_MAX_KW,
    CONF_CHARGER_MODE_SENSOR,
    CONF_CHARGER_OPERATION_MODE,
    CONF_CHARGER_RESUME_BUTTON,
    CONF_CHARGER_STOP_BUTTON,
    CONF_CURRENCY,
    CONF_DAILY_CONSUMPTION_PCT,
    CONF_DEADLINE_ENTITY,
    CONF_DEADLINE_RESTART_MINUTES,
    CONF_DEADLINE_TIME,
    CONF_LAST_FULL_CHARGE,
    CONF_MAX_SOC,
    CONF_MIN_DAYS_BETWEEN_FULL,
    CONF_MIN_SOC,
    CONF_SOC_ENTITY,
    CONF_SPOT_PRICES_ENTITY,
    CONF_WEEKLY_FULL_CHARGE,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_CHARGER_MAX_KW,
    DEFAULT_CURRENCY,
    DEFAULT_DAILY_CONSUMPTION_PCT,
    DEFAULT_DEADLINE_RESTART_MINUTES,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_DAYS_BETWEEN_FULL,
    DEFAULT_MIN_SOC,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DEFAULT_WEEKLY_FULL_CHARGE,
    DOMAIN,
    MODE_LIVE,
    MODE_OFF,
    MODE_PLAN,
)

_LOGGER = logging.getLogger(__name__)


def _to_bool(value: Any, default: bool) -> bool:
    """Tolerant boolean parsing (config values may arrive as strings)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "ja")
    return default


def _as_float(value: Any, default: float) -> float:
    """Tolerant float parsing (config values may arrive as strings)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def resolve_options(entry: ConfigEntry) -> dict:
    """Resolve effective options (entry data first, options override)."""
    options = {
        CONF_SPOT_PRICES_ENTITY: entry.data.get(CONF_SPOT_PRICES_ENTITY, ""),
        CONF_SOC_ENTITY: entry.data.get(CONF_SOC_ENTITY, ""),
        CONF_CHARGER_OPERATION_MODE: entry.data.get(CONF_CHARGER_OPERATION_MODE, ""),
        CONF_CHARGER_RESUME_BUTTON: entry.data.get(CONF_CHARGER_RESUME_BUTTON, ""),
        CONF_CHARGER_STOP_BUTTON: entry.data.get(CONF_CHARGER_STOP_BUTTON, ""),
        CONF_CHARGER_MODE_SENSOR: entry.data.get(CONF_CHARGER_MODE_SENSOR, ""),
        CONF_DEADLINE_ENTITY: entry.data.get(CONF_DEADLINE_ENTITY, ""),
        CONF_DEADLINE_TIME: entry.data.get(CONF_DEADLINE_TIME, ""),
        CONF_DEADLINE_RESTART_MINUTES: entry.data.get(
            CONF_DEADLINE_RESTART_MINUTES, DEFAULT_DEADLINE_RESTART_MINUTES
        ),
        CONF_MIN_SOC: entry.data.get(CONF_MIN_SOC, DEFAULT_MIN_SOC),
        CONF_MAX_SOC: entry.data.get(CONF_MAX_SOC, DEFAULT_MAX_SOC),
        CONF_BATTERY_CAPACITY_KWH: entry.data.get(
            CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH
        ),
        CONF_DAILY_CONSUMPTION_PCT: entry.data.get(
            CONF_DAILY_CONSUMPTION_PCT, DEFAULT_DAILY_CONSUMPTION_PCT
        ),
        CONF_CHARGER_MAX_KW: entry.data.get(CONF_CHARGER_MAX_KW, DEFAULT_CHARGER_MAX_KW),
        CONF_WEEKLY_FULL_CHARGE: entry.data.get(
            CONF_WEEKLY_FULL_CHARGE, DEFAULT_WEEKLY_FULL_CHARGE
        ),
        CONF_MIN_DAYS_BETWEEN_FULL: entry.data.get(
            CONF_MIN_DAYS_BETWEEN_FULL, DEFAULT_MIN_DAYS_BETWEEN_FULL
        ),
        CONF_CURRENCY: entry.data.get(CONF_CURRENCY, DEFAULT_CURRENCY),
    }
    options.update(entry.options)
    return options


class SmartChargingCoordinator(DataUpdateCoordinator):
    """Recompute the charging plan and (in Live mode) act on it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        # NB: this must NOT be called `_listeners` — DataUpdateCoordinator
        # already uses that name as a dict for CoordinatorEntity subscriptions.
        self._listener_unsubs: list[CALLBACK_TYPE] = []
        self._last_action_signature: Optional[str] = None
        self._last_summary: Optional[str] = None
        self._mode: str = str(entry.data.get("mode", MODE_PLAN))
        self._deadline_timer: Optional[CALLBACK_TYPE] = None
        self._last_full_charge: Optional[datetime] = self._parse_ts(
            entry.data.get(CONF_LAST_FULL_CHARGE)
        )
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
    def entry(self) -> ConfigEntry:
        """The config entry this coordinator is bound to."""
        return self._entry

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
                self._listener_unsubs.append(
                    ha_event.async_track_state_change_event(
                        self.hass,
                        [entity_id],
                        self._on_entity_change,
                    )
                )
        self._listener_unsubs.append(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_START,
                lambda _: self.hass.async_create_task(self.async_request_refresh()),
            )
        )
        # Note: older HA core used async_set_update_interval() here, but that
        # method no longer exists — update_interval is a settable property.
        self.update_interval = timedelta(minutes=self._update_interval_minutes())

    def async_unload(self) -> None:
        """Remove listeners."""
        if self._deadline_timer is not None:
            self._deadline_timer()
            self._deadline_timer = None
        for listener in self._listener_unsubs:
            listener()
        self._listener_unsubs = []

    @callback
    def _on_entity_change(self, _event: Any) -> None:
        """Queue a recompute when one of the watched entities changes."""
        self.hass.async_create_task(self.async_request_refresh())

    def _schedule_deadline_timer(self, plan: helper.Plan) -> None:
        """Wake at the next deadline boundary so stop/rearm happen on the minute."""
        if self._deadline_timer is not None:
            self._deadline_timer()
            self._deadline_timer = None
        instants = []
        if plan.deadline_restart_at is not None:
            instants.append(plan.deadline_restart_at)
        if plan.deadline_next is not None:
            instants.append(plan.deadline_next)
        future_instants = [
            dt_util.as_utc(t)
            for t in instants
            if t is not None and t > self.now
        ]
        if not future_instants:
            return
        when = min(future_instants)
        self._deadline_timer = ha_event.async_track_point_in_utc_time(
            self.hass, self._on_deadline_boundary, when
        )

    @callback
    def _on_deadline_boundary(self, _point_in_time: Any) -> None:
        """A deadline boundary passed — recompute so the plan flips state."""
        self._deadline_timer = None
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
        day_prices = self._day_prices(opts.get(CONF_SPOT_PRICES_ENTITY, ""))
        connected = self._is_connected(opts.get(CONF_CHARGER_MODE_SENSOR, ""))
        deadline_time = self._deadline_time(opts)
        deadline_restart_minutes = _as_float(
            opts.get(CONF_DEADLINE_RESTART_MINUTES, DEFAULT_DEADLINE_RESTART_MINUTES),
            0.0,
        )
        deadline_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        if deadline_tz is None:
            deadline_tz = dt_util.UTC
        soc_now = self._soc(opts.get(CONF_SOC_ENTITY, ""))
        currency = str(opts.get(CONF_CURRENCY, DEFAULT_CURRENCY))

        plan = helper.compute_plan(
            price_hours=price_hours,
            day_prices=day_prices,
            connected=connected,
            mode=self._mode,
            soc_now=soc_now,
            min_soc=float(opts.get(CONF_MIN_SOC, DEFAULT_MIN_SOC)),
            max_soc=float(opts.get(CONF_MAX_SOC, DEFAULT_MAX_SOC)),
            daily_consumption_pct=float(
                opts.get(CONF_DAILY_CONSUMPTION_PCT, DEFAULT_DAILY_CONSUMPTION_PCT)
            ),
            charger_max_kw=float(opts.get(CONF_CHARGER_MAX_KW, DEFAULT_CHARGER_MAX_KW)),
            battery_capacity_kwh=float(
                opts.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH)
            ),
            weekly_full_charge=_to_bool(
                opts.get(CONF_WEEKLY_FULL_CHARGE, DEFAULT_WEEKLY_FULL_CHARGE),
                DEFAULT_WEEKLY_FULL_CHARGE,
            ),
            last_full_charge=self._last_full_charge,
            min_days_between_full=float(
                opts.get(CONF_MIN_DAYS_BETWEEN_FULL, DEFAULT_MIN_DAYS_BETWEEN_FULL)
            ),
            deadline_time=deadline_time,
            deadline_restart_minutes=deadline_restart_minutes,
            deadline_timezone=deadline_tz,
            now=self.now,
            currency=currency,
        )

        # The car reports ~100 %: the week's boost has (or just) finished.
        # Remember it so the cooldown survives restarts.
        if soc_now is not None and soc_now >= 99.5:
            if self._last_full_charge is None or self.now - self._last_full_charge > (
                timedelta(minutes=1)
            ):
                self._persist_last_full_charge(self.now)
                plan.last_full_charge = self._last_full_charge

        if plan.summary != self._last_summary:
            self._logbook(plan)
            self._last_summary = plan.summary

        await self._maybe_act(plan)
        self._schedule_deadline_timer(plan)
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
            opts.get(CONF_SOC_ENTITY, ""),
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
        """Extract hourly prices from the spot-price forecast sensor.

        The sensor's ``hours`` attribute uses the compact format
        ``{"s": <unix epoch seconds|ms>, "p": <price per kWh>}``.
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return []
        hours = state.attributes.get("hours") or []
        parsed = helper.parse_price_hours(hours)
        if hours and not parsed:
            _LOGGER.warning(
                "No hourly prices parsed from %s — expected compact entries "
                "{'s': <unix epoch>, 'p': <price>} under the 'hours' attribute",
                entity_id,
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

    def _deadline_time(self, opts: dict) -> Optional[str]:
        """The time-of-day deadline (``"HH:MM"``), or ``None``.

        Prefers the native ``deadline_time`` option; falls back to the legacy
        ``deadline_entity`` (only its clock time is read — the date is ignored,
        the deadline recurs daily).
        """
        native = str(opts.get(CONF_DEADLINE_TIME, "")).strip()
        if native:
            return native
        entity = opts.get(CONF_DEADLINE_ENTITY, "")
        if not entity:
            return None
        state = self.hass.states.get(entity)
        if state is None:
            return None
        value = state.state
        if not value or value in ("unknown", "unavailable"):
            return None
        return self._extract_time(value)

    @staticmethod
    def _extract_time(value: str) -> Optional[str]:
        """Pull a ``"HH:MM"`` clock time out of an input_datetime state string.

        Accepts ``"05:45"``, ``"05:45:00"`` and ``"2026-09-24 05:45:00"``.
        """
        match = re.search(r"(\d{1,2}):(\d{2})", str(value))
        if not match:
            return None
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    def _day_prices(self, entity_id: str) -> list[helper.DayPrice]:
        """Best-effort per-day price summaries from the ``days`` attribute."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return []
        return helper.parse_days(state.attributes.get("days") or [])

    def _soc(self, entity_id: str) -> Optional[float]:
        """Read the battery SOC in percent (0-100) from the car sensor."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        if value < 0 or value > 100:
            return None
        return value

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        """Parse a persisted ISO timestamp (or None)."""
        if not value:
            return None
        parsed = dt_util.parse_datetime(str(value))
        if parsed is None:
            return None
        return dt_util.as_utc(parsed)

    def _persist_last_full_charge(self, when: datetime) -> None:
        """Remember the last 100 % charge (memory + config entry)."""
        self._last_full_charge = when
        data = dict(self._entry.data)
        data[CONF_LAST_FULL_CHARGE] = dt_util.as_utc(when).isoformat()
        self.hass.async_create_task(
            self.hass.config_entries.async_update_entry(self._entry, data=data)
        )

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