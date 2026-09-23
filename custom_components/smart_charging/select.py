"""Select entity for the Smart Charging integration — operating mode."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODE_LABELS, MODE_OPTIONS, NAME, VERSION
from .coordinator import SmartChargingCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SmartChargingModeSelect(coordinator)])


class SmartChargingModeSelect(CoordinatorEntity, SelectEntity):
    """Select to switch between Av / Planläge (test) / Live."""

    _attr_has_entity_name = True
    _attr_name = "Mode"
    _attr_options = list(MODE_LABELS.values())
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator: SmartChargingCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=NAME,
            manufacturer="Community",
            sw_version=VERSION,
        )
        # Current mode from coordinator (set by config entry data or previous state).
        self._attr_current_option = MODE_LABELS.get(coordinator.mode, MODE_LABELS["off"])

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update the displayed option when the coordinator refreshes."""
        mode = self.coordinator.mode  # type: ignore[union-attr]
        self._attr_current_option = MODE_LABELS.get(mode, MODE_LABELS["off"])
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Handle the user picking a new mode via the UI."""
        # Reverse-lookup: label -> internal value
        rev = {v: k for k, v in MODE_LABELS.items()}
        mode = rev.get(option, "off")
        await self.coordinator.async_set_mode(mode)
        self._attr_current_option = option
        self.async_write_ha_state()