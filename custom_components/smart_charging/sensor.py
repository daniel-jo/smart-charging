"""Sensors exposed by the Smart Charging integration."""

from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import helper
from .const import (
    CONF_CURRENCY,
    DEFAULT_CURRENCY,
    DOMAIN,
    MODE_LIVE,
    MODE_PLAN,
    NAME,
    VERSION,
)
from .coordinator import SmartChargingCoordinator

_LOGGER = logging.getLogger(__name__)


def _iso(value) -> str:
    if value is None:
        return ""
    return dt_util.as_local(value).isoformat()


def _session_attrs(sessions: list) -> list[dict]:
    """Convert ChargingSession objects to serialisable attributes."""
    out = []
    for s in sessions:
        out.append(
            {
                "start": _iso(s.start),
                "end": _iso(s.end),
                "power_kw": s.power_kw,
                "avg_price_kwh": s.avg_price_kwh,
                "hours": s.hours,
                "is_boost": s.is_boost,
            }
        )
    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    sensors = [
        SmartChargingPlanSensor(coordinator),
        SmartChargingDecisionSensor(coordinator),
        # Statistics — simulated in Plan mode, measured in Live mode.
        SmartChargingStatsSensor(
            coordinator, "Plan energy", f"{DOMAIN}_plan_energy", MODE_PLAN, "energy"
        ),
        SmartChargingStatsSensor(
            coordinator, "Plan saving", f"{DOMAIN}_plan_saving", MODE_PLAN, "saving"
        ),
        SmartChargingStatsSensor(
            coordinator,
            "Plan average price",
            f"{DOMAIN}_plan_avg_price",
            MODE_PLAN,
            "avg_price",
        ),
        SmartChargingStatsSensor(
            coordinator, "Live energy", f"{DOMAIN}_live_energy", MODE_LIVE, "energy"
        ),
        SmartChargingStatsSensor(
            coordinator, "Live saving", f"{DOMAIN}_live_saving", MODE_LIVE, "saving"
        ),
        SmartChargingStatsSensor(
            coordinator,
            "Live average price",
            f"{DOMAIN}_live_avg_price",
            MODE_LIVE,
            "avg_price",
        ),
    ]
    async_add_entities(sensors)


class SmartChargingPlanSensor(CoordinatorEntity, SensorEntity):
    """The main plan sensor — shows a human-readable summary of the charge plan."""

    _attr_has_entity_name = True
    _attr_name = "Plan"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: SmartChargingCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_plan"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=NAME,
            manufacturer="Community",
            sw_version=VERSION,
        )

    @property
    def native_value(self) -> str:
        plan = self.coordinator.plan
        if plan is None:
            return "Ingen plan"
        return plan.summary

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.plan
        if plan is None:
            return {"sessions": [], "next_action": None, "mode": self.coordinator.mode}
        na = plan.next_action
        next_action = None
        if na is not None:
            next_action = {
                "action": na.action,
                "at": _iso(na.at),
                "reason": na.reason,
            }
        return {
            "planned_sessions": _session_attrs(plan.sessions),
            "planned_hours": helper.planned_hours(plan.sessions),
            "next_action": next_action,
            "mode": self.coordinator.mode,
            "currency": plan.currency,
            "updated": _iso(plan.updated),
            "soc_now": plan.soc_now,
            "min_soc": plan.min_soc,
            "max_soc": plan.max_soc,
            "daily_consumption_pct": plan.daily_consumption_pct,
            "threshold_start": plan.threshold_start,
            "threshold_stop": plan.threshold_stop,
            "boost_scheduled": plan.boost_scheduled,
            "last_full_charge": _iso(plan.last_full_charge),
            "next_boost_after": _iso(plan.next_boost_after),
            "deadline_time": plan.deadline_time,
            "deadline_next": _iso(plan.deadline_next),
            "deadline_restart_at": _iso(plan.deadline_restart_at),
            "day_prices": [
                {
                    "date": d.date,
                    "min_kwh": d.min_kwh,
                    "max_kwh": d.max_kwh,
                    "avg_kwh": d.avg_kwh,
                }
                for d in plan.day_prices
            ],
        }


class SmartChargingDecisionSensor(CoordinatorEntity, SensorEntity):
    """Minimal sensor showing the desired next action (resume/stop/none)."""

    _attr_has_entity_name = True
    _attr_name = "Decision"
    _attr_icon = "mdi:lightning-bolt-outline"

    def __init__(self, coordinator: SmartChargingCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_decision"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=NAME,
            manufacturer="Community",
            sw_version=VERSION,
        )

    @property
    def native_value(self) -> str:
        plan = self.coordinator.plan
        if plan is None or plan.next_action is None:
            return "none"
        return plan.next_action.action

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.plan
        if plan is None or plan.next_action is None:
            return {"reason": ""}
        return {"reason": plan.next_action.reason, "at": _iso(plan.next_action.at)}


class SmartChargingStatsSensor(CoordinatorEntity, SensorEntity):
    """A per-mode statistics sensor (energy / saving / average price).

    Each mode (``plan`` = simulated, ``live`` = measured) has its own triad so
    the test-mode overview never mixes with real charging data. The
    ``device_class`` + ``state_class`` let Home Assistant's recorder build the
    history and long-term statistics natively from the sensor.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:ev-station"

    def __init__(
        self,
        coordinator: SmartChargingCoordinator,
        name: str,
        unique_id: str,
        stats_key: str,
        kind: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=NAME,
            manufacturer="Community",
            sw_version=VERSION,
        )
        self._stats_key = stats_key
        self._kind = kind  # "energy" | "saving" | "avg_price"

    @property
    def _stats(self) -> Optional[helper.ChargingStats]:
        return self.coordinator.stats.get(self._stats_key)  # type: ignore[union-attr]

    @property
    def device_class(self) -> Optional[str]:
        if self._kind == "energy":
            return SensorDeviceClass.ENERGY
        # ``saving`` (and ``avg_price``) deliberately expose no device class: a
        # ``monetary`` device class only permits a ``total``/``None`` state
        # class, while savings may tick down (a forced expensive hour posts a
        # negative contribution) — so it must stay ``measurement``. The currency
        # is still shown via native_unit_of_measurement.
        return None

    @property
    def state_class(self) -> Optional[str]:
        if self._kind == "saving":
            # Can tick down when a forced expensive hour (battery floor) posts a
            # negative contribution — must not be marked total_increasing.
            return SensorStateClass.MEASUREMENT
        if self._kind == "avg_price":
            return SensorStateClass.MEASUREMENT
        return SensorStateClass.TOTAL_INCREASING

    @property
    def native_unit_of_measurement(self) -> str:
        currency = self.coordinator.options.get(CONF_CURRENCY, DEFAULT_CURRENCY)
        if self._kind == "energy":
            return "kWh"
        if self._kind == "saving":
            return currency
        return f"{currency}/kWh"

    @property
    def native_value(self) -> Optional[float]:
        stats = self._stats
        if stats is None or stats.kwh <= 1e-9:
            return None
        if self._kind == "energy":
            return round(stats.kwh, 3)
        if self._kind == "saving":
            saved = stats.saved
            return round(saved, 2) if saved is not None else None
        avg = stats.avg_price_kwh
        return round(avg, 4) if avg is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self._stats
        if stats is None:
            return {}
        return {
            "mode": self._stats_key,
            "source": self.coordinator.stats_source(self._stats_key),
            "sessions": stats.sessions,
            "paid": round(stats.cost, 2) if stats.kwh > 1e-9 else None,
            "reference_cost": round(stats.cost_at_ref, 2) if stats.kwh > 1e-9 else None,
            "updated": _iso(stats.last_at),
        }