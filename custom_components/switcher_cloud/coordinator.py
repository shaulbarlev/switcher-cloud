"""DataUpdateCoordinator for Switcher Cloud cloud."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SwitcherApiError, SwitcherAuthError, SwitcherClient
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class SwitcherCloudCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
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
        self._command_locks: dict[str, asyncio.Lock] = {}
        self._refresh_debounce_task: asyncio.Task | None = None
        self._realtime_started = False

    # -- realtime (push) channel, aux-cloud style ---------------------------------
    @callback
    def start_realtime(self) -> None:
        """Start the persistent connection listener after platforms are set up."""
        if self._realtime_started:
            return
        self._realtime_started = True
        loop = self.hass.loop
        self.client.start_listener(
            on_push=lambda push: loop.call_soon_threadsafe(
                self._handle_push, push),
            on_reconnect=lambda: loop.call_soon_threadsafe(
                self._handle_reconnect),
        )

    @callback
    def stop_realtime(self) -> None:
        self._realtime_started = False
        if self._refresh_debounce_task:
            self._refresh_debounce_task.cancel()
            self._refresh_debounce_task = None
        self.client.stop_listener()

    @callback
    def _handle_reconnect(self) -> None:
        _LOGGER.debug("switcher connection re-established")
        self._schedule_debounced_refresh()

    @callback
    def _handle_push(self, push: dict) -> None:
        """0x0103 push from the server (device ack / status relay)."""
        _LOGGER.debug("push: %s", push)
        self._schedule_debounced_refresh()

    @callback
    def _schedule_debounced_refresh(self) -> None:
        """One authoritative refresh shortly after any push/reconnect,
        coalescing bursts (min ~4 s apart) instead of per-event polls."""
        if self._refresh_debounce_task and not self._refresh_debounce_task.done():
            return
        self._refresh_debounce_task = self.config_entry.async_create_task(
            self.hass, self._debounced_refresh(), "switcher_cloud_push_refresh")

    async def _debounced_refresh(self) -> None:
        await asyncio.sleep(4)
        await self.async_request_refresh()

    async def async_set_switch(self, did: str, on: bool) -> None:
        """Optimistic command transaction (pattern from ha-aux-cloud): publish
        the requested value immediately, roll back on failure, then let a
        delayed authoritative poll reconcile (the cloud list can lag)."""
        async with self._command_locks.setdefault(did, asyncio.Lock()):
            st = (self.data or {}).get(did)
            if st is None:
                raise UpdateFailed("device no longer available")
            prev = st.get("on")
            if prev != on:
                st["on"] = on
                self.async_update_listeners()
            try:
                await asyncio.to_thread(
                    self.client.set_switch, st["did_int"], st["device_pwd"], on)
            except (SwitcherApiError, OSError, Exception) as err:  # noqa: BLE001
                if (self.data or {}).get(did, {}).get("on") == on:
                    self.data[did]["on"] = prev
                    self.async_update_listeners()
                if isinstance(err, SwitcherAuthError):
                    raise ConfigEntryAuthFailed(str(err)) from err
                raise HomeAssistantError(str(err)) from err
            # authoritative reconcile shortly after (cloud WorkStatus lags)
            self.config_entry.async_create_task(
                self.hass, self._delayed_reconcile(), "switcher_cloud_reconcile")

    async def _delayed_reconcile(self) -> None:
        await asyncio.sleep(6)
        await self.async_request_refresh()

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
            st["device_type"] = str(dev.get("DeviceType", ""))
            try:
                st["device_pwd"] = await asyncio.to_thread(
                    self.client.device_password, dev)
            except Exception:  # noqa: BLE001 - pwd decrypt must not kill the poll
                st["device_pwd"] = None
            out[str(dev["DID"])] = st
        return out
