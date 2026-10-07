"""Coordinator for the Smart Charging integration.

Event-driven: listens to state changes on the configured input entities
(spot prices forecast, charger mode, deadline, mode select) plus a periodic
tick, throttles recomputes, and updates the plan. In Live mode it calls the
charger switch/button services when the desired next action changes.

Only services written: switch.turn_on/off on the operation-mode entity and
button.press on the resume/stop buttons. The integration NEVER writes
available_current. It also registers its own ``smart_charging.reset_statistics``
service for the accumulated statistics (Plan-mode simulation / Live-mode
measurement).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import event as ha_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import helper
from .const import (
    CHARGER_CONNECTED_STATES,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGER_ENERGY_SENSOR,
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
    CONF_USAGE_AWAY_END,
    CONF_USAGE_AWAY_START,
    CONF_USAGE_DAYS,
    CONF_USAGE_ENABLED,
    CONF_WEEKLY_FULL_CHARGE,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_CHARGER_MAX_KW,
    DEFAULT_CURRENCY,
    DEFAULT_DAILY_CONSUMPTION_PCT,
    DEFAULT_DEADLINE_RESTART_MINUTES,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_DAYS_BETWEEN_FULL,
    DEFAULT_MIN_SOC,
    DEFAULT_STATS_SAMPLE_SECONDS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DEFAULT_USAGE_DAYS,
    DEFAULT_USAGE_ENABLED,
    DEFAULT_WEEKLY_FULL_CHARGE,
    DOMAIN,
    MODE_LIVE,
    MODE_OFF,
    MODE_PLAN,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_RESET_STATISTICS = "reset_statistics"
SERVICE_RESET_STATISTICS_SCHEMA = vol.Schema(
    {vol.Optional("mode", default="all"): vol.In(["all", MODE_PLAN, MODE_LIVE])}
)


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
        CONF_CHARGER_ENERGY_SENSOR: entry.data.get(CONF_CHARGER_ENERGY_SENSOR, ""),
        CONF_DEADLINE_ENTITY: entry.data.get(CONF_DEADLINE_ENTITY, ""),
        CONF_DEADLINE_TIME: entry.data.get(CONF_DEADLINE_TIME, ""),
        CONF_DEADLINE_RESTART_MINUTES: entry.data.get(
            CONF_DEADLINE_RESTART_MINUTES, DEFAULT_DEADLINE_RESTART_MINUTES
        ),
        CONF_USAGE_ENABLED: entry.data.get(CONF_USAGE_ENABLED, DEFAULT_USAGE_ENABLED),
        CONF_USAGE_DAYS: entry.data.get(CONF_USAGE_DAYS, DEFAULT_USAGE_DAYS),
        CONF_USAGE_AWAY_START: entry.data.get(CONF_USAGE_AWAY_START, ""),
        CONF_USAGE_AWAY_END: entry.data.get(CONF_USAGE_AWAY_END, ""),
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
        # Home Assistant start listener — tracked separately (not in
        # _listener_unsubs) so it can unsubscribe itself exactly once, which
        # makes reload/unload idempotent after HOMEASSISTANT_START has fired.
        self._hass_start_unsub: Optional[CALLBACK_TYPE] = None
        self._last_action_signature: Optional[str] = None
        self._last_summary: Optional[str] = None
        self._mode: str = str(entry.data.get("mode", MODE_PLAN))
        self._deadline_timer: Optional[CALLBACK_TYPE] = None
        self._last_full_charge: Optional[datetime] = self._parse_ts(
            entry.data.get(CONF_LAST_FULL_CHARGE)
        )
        # --- Charging statistics (simulated in Plan mode, measured in Live) ---
        # Persisted via the HA storage helper so the totals survive restarts.
        self._stats_store: Optional[Store] = Store(
            hass, 1, f"{DOMAIN}.stats.{entry.entry_id}"
        )
        self._stats_loaded = False
        self._stats: dict[str, helper.ChargingStats] = {
            MODE_PLAN: helper.ChargingStats(),
            MODE_LIVE: helper.ChargingStats(),
        }
        # Fully-accrued planned hour keys + currently-partial hour bookkeeping
        # (prevent double counting across recomputes and restarts).
        self._stats_accrued_hours: set[str] = set()
        self._stats_partial: dict[str, dict] = {}
        self._stats_active_modes: set[str] = set()
        # Live window sampling state (energy sensor / SOC fallback).
        self._live_window = False
        self._live_prev: Optional[float] = None
        self._live_prev_at: Optional[datetime] = None
        self._live_sample_job: Optional[CALLBACK_TYPE] = None
        self._service_unsub: Optional[CALLBACK_TYPE] = None
        # Last forecast, needed by the off-recompute live sampling loop.
        self._last_price_hours: list[helper.PriceHour] = []
        self._last_day_prices: list[helper.DayPrice] = []
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

    @property
    def stats(self) -> dict[str, helper.ChargingStats]:
        """The accumulated statistics ledgers (``plan`` and ``live``)."""
        return self._stats

    def stats_source(self, mode: str) -> str:
        """Where the statistics for a mode are measured from."""
        if mode == MODE_PLAN:
            return "simulation"
        opts = self.options
        if opts.get(CONF_CHARGER_ENERGY_SENSOR, ""):
            return "energy_sensor"
        if opts.get(CONF_SOC_ENTITY, ""):
            return "soc"
        return "none"

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
        self._hass_start_unsub = self.hass.bus.async_listen(
            EVENT_HOMEASSISTANT_START, self._async_first_refresh
        )
        # Note: older HA core used async_set_update_interval() here, but that
        # method no longer exists — update_interval is a settable property.
        self.update_interval = timedelta(minutes=self._update_interval_minutes())
        # Statistics reset via a service (developer tools / automation).
        self._service_unsub = self.hass.services.async_register(
            DOMAIN,
            SERVICE_RESET_STATISTICS,
            self._async_reset_statistics,
            schema=SERVICE_RESET_STATISTICS_SCHEMA,
        )

    def async_unload(self) -> None:
        """Remove listeners."""
        # The start listener may already have removed itself (event fired) —
        # the None guard makes this safe to call at any point after setup.
        if self._hass_start_unsub is not None:
            self._hass_start_unsub()
            self._hass_start_unsub = None
        if self._deadline_timer is not None:
            self._deadline_timer()
            self._deadline_timer = None
        self._close_live_window()
        if self._service_unsub is not None:
            self._service_unsub()
            self._service_unsub = None
        for listener in self._listener_unsubs:
            listener()
        self._listener_unsubs = []
        # Flush any pending delayed stats save.
        if self._stats_store is not None and self._stats_loaded:
            self.hass.async_create_task(
                self._stats_store.async_save(self._stats_snapshot())
            )

    async def _async_first_refresh(self, _event: Any) -> None:
        """Recompute once HA has fully started (HOMEASSISTANT_START).

        An async coroutine so the refresh is awaited directly on the event
        loop — HA may otherwise invoke a sync listener off-loop and
        ``hass.async_create_task`` there is thread-unsafe (a hard error in
        newer HA).

        Unsubscribes itself first so a later config-entry reload (which runs
        while HA is already started) never tries to remove this listener twice.
        """
        if self._hass_start_unsub is not None:
            self._hass_start_unsub()
            self._hass_start_unsub = None
        await self.async_request_refresh()

    async def _on_entity_change(self, _event: Any) -> None:
        """Recompute when one of the watched entities changes."""
        await self.async_request_refresh()

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
        if plan.usage_next is not None:
            instants.append(plan.usage_next)
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

    async def _on_deadline_boundary(self, _point_in_time: Any) -> None:
        """A deadline boundary passed — recompute so the plan flips state."""
        self._deadline_timer = None
        await self.async_request_refresh()

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
            usage_enabled=_to_bool(
                opts.get(CONF_USAGE_ENABLED, DEFAULT_USAGE_ENABLED),
                DEFAULT_USAGE_ENABLED,
            ),
            usage_days=str(opts.get(CONF_USAGE_DAYS, DEFAULT_USAGE_DAYS)),
            usage_away_start=str(opts.get(CONF_USAGE_AWAY_START, "")),
            usage_away_end=str(opts.get(CONF_USAGE_AWAY_END, "")),
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

        # Remember the forecast for the off-recompute live sampling loop, then
        # accrue statistics for the active mode.
        self._last_price_hours = price_hours
        self._last_day_prices = day_prices
        await self._ensure_stats_loaded()
        self._update_statistics(plan, connected)
        return plan

    async def async_set_mode(self, mode: str) -> None:
        """Set the operating mode and trigger a recompute."""
        if mode not in (MODE_OFF, MODE_PLAN, MODE_LIVE):
            _LOGGER.warning("Unknown mode %r ignored", mode)
            return
        if mode != self._mode:
            # Mode switch ends the current plan-mode test run / live window so
            # the next engagement counts as a new "session".
            self._stats_active_modes.clear()
            self._close_live_window()
        self._mode = mode
        await self.async_request_refresh()

    def _entity_ids(self) -> list[str]:
        """All watched entity IDs from the config.

        The charging energy sensor is deliberately NOT watched — it updates far
        too often and would spam recomputes. It is sampled by a dedicated loop
        while live charging is active instead.
        """
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
    # Charging statistics (simulated in Plan mode, measured in Live mode)
    # ------------------------------------------------------------------
    async def _ensure_stats_loaded(self) -> None:
        """Load the persisted statistics ledgers exactly once."""
        if self._stats_loaded or self._stats_store is None:
            return
        self._stats_loaded = True
        data = await self._stats_store.async_load()
        if not isinstance(data, dict):
            return
        self._stats[MODE_PLAN] = helper.stats_from_dict(data.get(MODE_PLAN))
        self._stats[MODE_LIVE] = helper.stats_from_dict(data.get(MODE_LIVE))
        self._stats_accrued_hours = set(data.get("accrued_hours") or [])
        partial = data.get("partial_hours")
        self._stats_partial = partial if isinstance(partial, dict) else {}

    def _stats_snapshot(self) -> dict:
        """Serialise the statistics state for the HA storage helper."""
        return {
            MODE_PLAN: helper.stats_to_dict(self._stats[MODE_PLAN]),
            MODE_LIVE: helper.stats_to_dict(self._stats[MODE_LIVE]),
            "accrued_hours": sorted(self._stats_accrued_hours),
            "partial_hours": self._stats_partial,
        }

    def _save_stats(self) -> None:
        """Queue a debounced write of the statistics state."""
        if self._stats_store is not None:
            self._stats_store.async_delay_save(self._stats_snapshot, 5)

    def _update_statistics(self, plan: helper.Plan, connected: bool) -> None:
        """Accrue statistics for the active mode (and stop a stray live window)."""
        now = self.now
        if self._mode == MODE_PLAN:
            self._accrue_plan(plan, now)
            self._close_live_window()
        elif self._mode == MODE_LIVE:
            active = connected and self._charging_active(plan)
            if active and not self._live_window:
                self._begin_live_window(plan)
            elif active:
                self._sample_live(plan, now)
            elif self._live_window:
                self._sample_live(plan, now)
                self._close_live_window()
        else:  # MODE_OFF
            self._close_live_window()

    # ------------------------------------------------------------------
    # Plan-mode simulation
    # ------------------------------------------------------------------
    def _accrue_plan(self, plan: helper.Plan, now: datetime) -> None:
        """Simulated accrual: count planned hours as they pass (Plan mode).

        The hour-across-time logic lives in ``helper.accrue_plan_hours``
        (pure, unit-tested); this wrapper tracks the mode's session counter and
        persists the result.
        """
        if plan is None or not plan.sessions:
            return
        before_kwh = self._stats[MODE_PLAN].kwh
        fallback_ref = self._mean_price(self._last_price_hours)
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        stats = helper.accrue_plan_hours(
            self._stats[MODE_PLAN],
            plan.sessions,
            now,
            self._last_day_prices,
            fallback_ref,
            self._stats_accrued_hours,
            self._stats_partial,
            local_tz,
        )
        if stats.kwh > before_kwh:
            if MODE_PLAN not in self._stats_active_modes:
                stats = helper.start_stat_session(stats)
                self._stats_active_modes.add(MODE_PLAN)
            self._stats[MODE_PLAN] = stats
            self._save_stats()

    # ------------------------------------------------------------------
    # Live-mode measurement
    # ------------------------------------------------------------------
    def _charging_active(self, plan: helper.Plan) -> bool:
        """True while a planned session covers the current moment (Live mode)."""
        if plan is None or not plan.sessions:
            return False
        now = self.now
        return any(sess.start <= now < sess.end for sess in plan.sessions)

    def _begin_live_window(self, plan: helper.Plan) -> None:
        """Mark the start of a measured charging window (Live mode)."""
        now = self.now
        self._live_window = True
        if self._live_prev is not None and self._live_prev_at is not None:
            # Keep a recently primed baseline (e.g. from the resume moment) so
            # the energy consumed between resume and this first active sample is
            # not lost. Only an absurdly old reading is discarded.
            if now - self._live_prev_at > timedelta(hours=12):
                self._live_prev = None
                self._live_prev_at = None
        else:
            self._live_prev = None
            self._live_prev_at = None
        self._stats[MODE_LIVE] = helper.start_stat_session(self._stats[MODE_LIVE])
        self._sample_live(plan, now)  # baseline read (or first delta after resume)
        self._schedule_live_sample()
        self._save_stats()

    def _prime_live_energy(self) -> None:
        """Sample the live energy source without accruing (resume/stop baseline)."""
        opts = self.options
        value: Optional[float] = None
        energy_entity = opts.get(CONF_CHARGER_ENERGY_SENSOR, "")
        soc_entity = opts.get(CONF_SOC_ENTITY, "")
        if energy_entity:
            value, _is_power = self._energy_entity_value(energy_entity)
        elif soc_entity:
            capacity = _as_float(
                opts.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH),
                DEFAULT_BATTERY_CAPACITY_KWH,
            )
            soc = self._soc(soc_entity)
            if soc is not None:
                value = soc * capacity / 100.0  # SOC % -> battery kWh
        if value is not None or self._live_prev is None:
            self._live_prev = value
        self._live_prev_at = self.now

    def _sample_live(self, plan: helper.Plan, now: datetime) -> None:
        """Read the energy source and accrue energy measured since the last read.

        The price used for the measured kWh is the forecast price of the clock
        hour the energy was measured in (fallback: the active session's average
        price). The savings reference is the day average of that date.
        """
        stats = self._stats[MODE_LIVE]
        opts = self.options
        value: Optional[float] = None
        is_power = False

        energy_entity = opts.get(CONF_CHARGER_ENERGY_SENSOR, "")
        soc_entity = opts.get(CONF_SOC_ENTITY, "")
        if energy_entity:
            value, is_power = self._energy_entity_value(energy_entity)
        elif soc_entity:
            capacity = _as_float(
                opts.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH),
                DEFAULT_BATTERY_CAPACITY_KWH,
            )
            soc = self._soc(soc_entity)
            if soc is not None:
                value = soc * capacity / 100.0  # SOC % -> battery kWh

        delta_kwh = 0.0
        if value is not None:
            if self._live_prev is None:
                delta_kwh = 0.0
            elif is_power:
                dt_h = (
                    (now - self._live_prev_at).total_seconds() / 3600.0
                    if self._live_prev_at is not None
                    else 0.0
                )
                if dt_h > 0:
                    delta_kwh = max(0.0, value * dt_h)
            elif value >= self._live_prev:
                delta_kwh = value - self._live_prev
            self._live_prev = value
        self._live_prev_at = now

        if delta_kwh > 1e-9:
            price = helper.price_at(self._last_price_hours, now)
            if price is None:
                price = self._session_avg_price(plan, now)
            fallback_ref = self._mean_price(self._last_price_hours)
            local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
            ref = helper.reference_price_kwh(
                self._last_day_prices, now, fallback_ref, local_tz
            )
            self._stats[MODE_LIVE] = helper.accrue_charging(
                stats, delta_kwh, price or 0.0, ref or 0.0, at=now
            )
            self._save_stats()

    def _energy_entity_value(self, entity_id: str) -> tuple[Optional[float], bool]:
        """Read a charging energy (kWh) or power (kW / W) sensor.

        Returns ``(value, is_power)`` — ``value`` is in kWh for cumulative
        energy sensors and in kW for power sensors (``W`` is converted).
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return None, False
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None, False
        unit = str(state.attributes.get("unit_of_measurement") or "").lower()
        is_power = unit in ("w", "kw")
        if is_power and unit == "w":
            value /= 1000.0
        return value, is_power

    @staticmethod
    def _session_avg_price(plan: helper.Plan, now: datetime) -> Optional[float]:
        """Fallback price: the average of the planned session covering ``now``."""
        if plan is not None:
            for sess in plan.sessions:
                if sess.start <= now < sess.end:
                    return sess.avg_price_kwh
        return None

    def _schedule_live_sample(self) -> None:
        """Wake periodically while a live charging window is active."""
        if self._live_sample_job is not None:
            return
        self._live_sample_job = ha_event.async_track_point_in_utc_time(
            self.hass,
            self._live_sample_tick,
            self.now + timedelta(seconds=DEFAULT_STATS_SAMPLE_SECONDS),
        )

    async def _live_sample_tick(self, _point_in_time: Any) -> None:
        """Periodic energy read while live charging is active."""
        self._live_sample_job = None
        if self._mode != MODE_LIVE or not self._live_window:
            return
        await self._ensure_stats_loaded()
        plan = self.plan
        if plan is None:
            self._close_live_window()
            return
        self._sample_live(plan, self.now)
        if self._live_window:
            self._schedule_live_sample()

    def _close_live_window(self) -> None:
        """Stop a measured live charging window (timer + sampling state)."""
        self._live_window = False
        if self._live_sample_job is not None:
            self._live_sample_job()
            self._live_sample_job = None
        self._live_prev = None
        self._live_prev_at = None

    async def _async_reset_statistics(self, call: Any) -> None:
        """Reset statistics for one mode (``plan`` / ``live`` / ``all``)."""
        mode = call.data.get("mode", "all")
        if mode in (MODE_PLAN, MODE_LIVE):
            self._stats[mode] = helper.ChargingStats()
            if mode == MODE_PLAN:
                # Keep consumed hour keys so a reset does not re-accrue history.
                self._stats_partial = {}
        else:
            self._stats[MODE_PLAN] = helper.ChargingStats()
            self._stats[MODE_LIVE] = helper.ChargingStats()
            self._stats_accrued_hours = set()
            self._stats_partial = {}
        self._save_stats()
        self.async_update_listeners()

    @staticmethod
    def _mean_price(price_hours: list[helper.PriceHour]) -> Optional[float]:
        """Mean forecast price (savings-reference fallback)."""
        if not price_hours:
            return None
        return sum(h.price_kwh for h in price_hours) / len(price_hours)
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
            # Baseline the energy source now so the first live sample after the
            # charger starts (up to one recompute period later) can accrue the
            # energy consumed in between.
            self._prime_live_energy()
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
            # No priming here: the final sample in ``_update_statistics`` (right
            # after this) captures the interval since the last timer read.
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