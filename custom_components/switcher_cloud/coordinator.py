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
        self._schedule_push_debounced_refresh()

    @callback
    def _handle_push(self, push: dict) -> None:
        """0x0103 push from the server (device ack / status relay)."""
        _LOGGER.debug("push: %s", push)
        self._schedule_push_debounced_refresh()

    @callback
    def _schedule_push_debounced_refresh(self) -> None:
        """One authoritative refresh shortly after any push/reconnect,
        coalescing bursts (min ~4 s apart) instead of per-event polls."""
        if self._refresh_debounce_task and not self._refresh_debounce_task.done():
            return
        self._refresh_debounce_task = self.config_entry.async_create_task(
            self.hass, self._push_debounced_refresh(), "switcher_cloud_push_refresh")

    async def _push_debounced_refresh(self) -> None:
        await asyncio.sleep(4)
        await self.async_request_refresh()

    async def async_set_switch(self, did: str, on: bool, gang: int | None = None) -> None:
        """Confirmed (non-optimistic) command: send, wait for the cloud ack,
        then poll until the device reports the new state. The UI only flips
        once the device has actually changed — a visible 'on' means it worked.
        `gang` selects a line on multi-gang wall switches (0/None = all)."""
        from .devices import (Family, body_bath_heater, body_light_toggle,
                              body_onwall, body_plug, body_switch)
        async with self._command_locks.setdefault(did, asyncio.Lock()):
            st = (self.data or {}).get(did)
            if st is None:
                raise UpdateFailed("device no longer available")
            fam = Family(st.get("family", "unknown"))
            pwd = self.client.pwd_block(st["device_pwd"] or "")
            uid = self.client.uid
            d = st["did_int"]
            if fam is Family.ON_WALL:
                body = body_onwall(d, uid, pwd, on)
            elif fam is Family.BATH_HEATER:
                body = body_bath_heater(d, uid, pwd, on)
            elif fam in (Family.PLUG, Family.SOCKET):
                body = body_plug(d, uid, pwd, on, st.get("line_no", 0), st.get("line_type", 0))
            elif fam is Family.SWITCH:
                body = body_switch(d, uid, pwd, gang or 0, on)
            elif fam is Family.LIGHT:
                body = body_light_toggle(d, uid, pwd, on)
            else:
                raise HomeAssistantError(f"{fam.value}: on/off not supported")
            await self._send_and_confirm(did, body, lambda s: self._gang_state(s, gang) == on)

    async def async_control(self, did: str, body: bytes, done=None) -> None:
        """Generic control (covers etc.): send + confirm via `done(state)` or just refresh."""
        async with self._command_locks.setdefault(did, asyncio.Lock()):
            await self._send_and_confirm(did, body, done)

    @staticmethod
    def _gang_state(st: dict, gang: int | None) -> bool | None:
        if gang and st.get("gangs"):
            g = st["gangs"]
            return g[gang] if gang < len(g) else None
        return st.get("on")

    async def _send_and_confirm(self, did: str, body: bytes, done) -> None:
        try:
            await asyncio.to_thread(self.client.send_control, body)
        except SwitcherAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (SwitcherApiError, OSError, Exception) as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err
        # confirm: poll quickly (every 0.5 s) until the device reports the
        # requested state, giving up after 30 s and letting the regular
        # poll/push catch up.
        deadline = self.hass.loop.time() + 30
        while self.hass.loop.time() < deadline:
            await asyncio.sleep(0.5)
            await self.async_refresh()     # direct, not debounced
            st = (self.data or {}).get(did, {})
            if done is None or done(st):
                break

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
