"""DataUpdateCoordinator for Switcher On Wall cloud."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SwitcherApiError, SwitcherAuthError, SwitcherClient
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class SwitcherOnwallCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the cloud for device list + WorkStatus.

    Data: {did_str: {name, on, secs_on, online, dmac, model, did_int, device_pwd}}
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} coordinator",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = SwitcherClient(
            entry.data["account"], entry.data["password"])

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            devices = await asyncio.to_thread(self.client.get_devices)
        except SwitcherAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (SwitcherApiError, OSError) as err:
            raise UpdateFailed(str(err)) from err
        out: dict[str, dict[str, Any]] = {}
        for dev in devices:
            st = SwitcherClient.device_state(dev)
            if not st:
                continue
            st["did_int"] = int(dev["DID"])
            try:
                st["device_pwd"] = await asyncio.to_thread(
                    self.client.device_password, dev)
            except Exception:  # noqa: BLE001 - pwd decrypt must not kill the poll
                st["device_pwd"] = None
            out[str(dev["DID"])] = st
        return out
