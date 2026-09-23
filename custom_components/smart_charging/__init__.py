"""Smart Charging custom integration for Home Assistant.

The integration is the *brain* that plans and (in Live mode) executes EV
charging decisions based on spot prices and configurable thresholds.

Contracts (never break):
- Entity IDs: ``sensor.smart_charging_plan``, ``sensor.smart_charging_decision``,
  ``calendar.smart_charging_plan``, ``select.smart_charging_mode``.
- **Writes**: only ``switch.*_charger_operation_mode`` (turn_on/off) and
  ``button.*_resume_charging`` / ``button.*_stop_charging_final`` (press).
- **Never writes**: ``number.*_available_current`` — that is the sole domain of
  the load-balancing blueprint.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import SmartChargingCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Smart Charging from a config entry."""
    coordinator = SmartChargingCoordinator(hass, entry)

    # First refresh — on failure the plan stays empty (no data) rather than
    # raising, so setup always succeeds.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Subscribe to entity changes now that all platforms are set up.
    await coordinator.async_setup()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: SmartChargingCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_unload()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.setdefault(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok