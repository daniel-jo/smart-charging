"""Sensors exposed by the Smart Charging integration."""

from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import helper
from .const import DOMAIN, NAME, VERSION
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
    ]
    async_add_entities(sensors)


class SmartChargingPlanSensor(CoordinatorEntity, SensorEntity):
    """The main plan sensor — shows a human-readable summary of the charge plan."""

    _attr_has_entity_name = True
    _attr_name = "Charge plan"
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
    _attr_name = "Charge decision"
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