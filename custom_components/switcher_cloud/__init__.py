"""Switcher Cloud integration."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, PLATFORMS
from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: SwitcherCloudConfigEntry) -> bool:
    coordinator = SwitcherCloudCoordinator(hass, entry)
    # Stop the listener thread on unload and on failed setup. HA runs
    # on_unload callbacks in both cases, so a failed setup can't leak a
    # reconnect loop.
    entry.async_on_unload(coordinator.stop_realtime)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start_realtime()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SwitcherCloudConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SwitcherCloudConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device from the UI once it is gone from the account."""
    known = set((entry.runtime_data.data or {}).keys())
    return not any(
        ident[0] == DOMAIN and ident[1] in known for ident in device.identifiers
    )
