"""Calendar entity for the Smart Charging integration.

Shows planned charging sessions as calendar events in HA's built-in Calendar
view. Uses the modern CalendarEntity API (2024.12+).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION
from .coordinator import SmartChargingCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SmartChargingPlanCalendar(coordinator)])


class SmartChargingPlanCalendar(CoordinatorEntity, CalendarEntity):
    """Calendar entity showing planned charging sessions."""

    _attr_has_entity_name = True
    _attr_name = "Charge plan"
    _attr_icon = "mdi:car-electric"

    def __init__(self, coordinator: SmartChargingCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_calendar"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=NAME,
            manufacturer="Community",
            sw_version=VERSION,
        )

    @property
    def event(self) -> Optional[CalendarEvent]:
        """Return the next upcoming charging session, or None."""
        plan = self.coordinator.plan
        if plan is None or not plan.sessions:
            return None
        # Find the first session that hasn't ended yet.
        now = self.coordinator.now  # type: ignore[union-attr]
        for sess in plan.sessions:
            if sess.end > now:
                start_local = sess.start.strftime("%H:%M")
                end_local = sess.end.strftime("%H:%M")
                summary = (
                    f"EV-laddning {start_local}–{end_local} "
                    f"({sess.power_kw:.0f} kW, {sess.avg_price_sek_kwh:.2f} kr/kWh)"
                )
                return CalendarEvent(
                    start=sess.start,
                    end=sess.end,
                    summary=summary,
                )
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return all planned sessions within the given window."""
        plan = self.coordinator.plan
        if plan is None or not plan.sessions:
            return []
        events: list[CalendarEvent] = []
        for sess in plan.sessions:
            if sess.start < end_date and sess.end > start_date:
                start_local = sess.start.strftime("%H:%M")
                end_local = sess.end.strftime("%H:%M")
                summary = (
                    f"EV-laddning {start_local}–{end_local} "
                    f"({sess.power_kw:.0f} kW, {sess.avg_price_sek_kwh:.2f} kr/kWh)"
                )
                events.append(
                    CalendarEvent(
                        start=sess.start,
                        end=sess.end,
                        summary=summary,
                    )
                )
        return events