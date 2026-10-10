"""DataUpdateCoordinator for Switcher Cloud."""
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
from .const import CONF_ACCOUNT, CONF_PASSWORD, DEFAULT_SCAN_INTERVAL, DOMAIN
from .devices import (
    BOILER_FAMILIES,
    Family,
    body_auto_shutdown,
    body_bath_heater,
    body_light_toggle,
    body_onwall,
    body_plug,
    body_plug_single,
    body_switch,
)

_LOGGER = logging.getLogger(__name__)

CONFIRM_INTERVAL = 0.5     # s between confirm polls after a command
CONFIRM_TIMEOUT = 30.0     # s before giving up and letting the regular poll catch up
ACTIVE_SCAN_INTERVAL = 15  # s while any boiler is on: catches the self-off promptly and
                           # re-syncs the countdown seed (the cloud updates it rarely)

type SwitcherCloudConfigEntry = ConfigEntry["SwitcherCloudCoordinator"]


class SwitcherCloudCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the cloud for device list + WorkStatus; pushes trigger refreshes.

    Data: {did_str: state dict from SwitcherClient.device_state + device_pwd}
    """

    config_entry: SwitcherCloudConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SwitcherCloudConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} coordinator",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = SwitcherClient(entry.data[CONF_ACCOUNT], entry.data[CONF_PASSWORD])
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
            on_push=lambda push: loop.call_soon_threadsafe(self._handle_push, push),
            on_reconnect=lambda: loop.call_soon_threadsafe(self._handle_reconnect),
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

    # -- commands (confirmed, non-optimistic) ------------------------------------
    async def async_set_switch(self, did: str, on: bool, gang: int | None = None,
                               run_seconds: int = 0) -> None:
        """Send on/off for the device's family, then poll until the device
        reports the new state. The UI only flips once the device really changed.
        `gang` selects a line on multi-gang wall switches (0/None = all).
        `run_seconds` > 0 = timed run (boost): the device turns itself off after
        that many seconds (S216/bath heater isdelay/delayTime; plug isdelay)."""
        async with self._command_locks.setdefault(did, asyncio.Lock()):
            st = (self.data or {}).get(did)
            if st is None:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_unavailable")
            fam = Family(st.get("family", "unknown"))
            pwd_plain = st.get("device_pwd") or ""
            pwd = self.client.pwd_block(pwd_plain)
            uid = self.client.uid
            d = st["did_int"]
            secs = max(0, int(run_seconds)) if on else 0
            single = None
            if fam is Family.ON_WALL:
                body = body_onwall(d, uid, pwd, on, isdelay=secs)
            elif fam is Family.BATH_HEATER:
                body = body_bath_heater(d, uid, pwd, on, delay=secs)
            elif fam in (Family.PLUG, Family.MIX):
                ln, lt = st.get("line_no", 0), st.get("line_type", 0)
                if ln == 0 and lt == 0:
                    # single-line plug: app uses the key2_50-wrapped body
                    body = body_plug_single(d, uid, pwd_plain, on, isdelay=secs)
                    single = d
                else:
                    body = body_plug(d, uid, pwd, on, ln, lt, isdelay=secs,
                                     autoclosing=st.get("autoclosing") or 0)
            elif fam is Family.SWITCH:
                body = body_switch(d, uid, pwd, gang or 0, on)
            elif fam is Family.LIGHT:
                body = body_light_toggle(d, uid, pwd, on)
            else:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unsupported",
                                         translation_placeholders={"family": fam.value})
            await self._send_and_confirm(did, body, lambda s: self._gang_state(s, gang) == on,
                                         single_did=single)

    async def async_set_auto_shutdown(self, did: str, seconds: int) -> None:
        """Set the device's auto-shutdown limit (S216 parameter 0x0C14, 0x0E01 write)."""
        async with self._command_locks.setdefault(did, asyncio.Lock()):
            st = (self.data or {}).get(did)
            if st is None:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_unavailable")
            body = body_auto_shutdown(st["did_int"], self.client.uid,
                                      self.client.pwd_block(st.get("device_pwd") or ""), seconds)
            try:
                await self.hass.async_add_executor_job(self.client.send_write_params, body)
            except SwitcherAuthError as err:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="invalid_auth") from err
            except Exception as err:  # noqa: BLE001
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_failed",
                                         translation_placeholders={"error": str(err)}) from err

    async def async_control(self, did: str, body: bytes, done=None) -> None:
        """Generic control (covers, lights): send + confirm via `done(state)`."""
        async with self._command_locks.setdefault(did, asyncio.Lock()):
            await self._send_and_confirm(did, body, done)

    @staticmethod
    def _gang_state(st: dict, gang: int | None) -> bool | None:
        if gang and st.get("gangs"):
            g = st["gangs"]
            return g[gang] if gang < len(g) else None
        return st.get("on")

    async def _send_and_confirm(self, did: str, body: bytes, done,
                                single_did: int | None = None) -> None:
        try:
            if single_did is not None:
                await self.hass.async_add_executor_job(
                    self.client.send_control_single, single_did, body)
            else:
                await self.hass.async_add_executor_job(self.client.send_control, body)
        except SwitcherAuthError as err:
            # a service call can't trigger reauth itself; the next poll will.
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="invalid_auth") from err
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_failed",
                                     translation_placeholders={"error": str(err)}) from err
        # Confirm with a private fetch (does not touch last_update_success or
        # the scheduled poll), publishing once when the requested state shows.
        deadline = self.hass.loop.time() + CONFIRM_TIMEOUT
        while self.hass.loop.time() < deadline:
            await asyncio.sleep(CONFIRM_INTERVAL)
            try:
                data = await self.hass.async_add_executor_job(self.client.poll)
            except Exception as err:  # noqa: BLE001 - transient; keep trying
                _LOGGER.debug("confirm poll failed: %s", err)
                continue
            self._apply_scan_interval(data)
            self.async_set_updated_data(data)
            if done is None or done(data.get(did, {})):
                break

    # -- scheduled poll ------------------------------------------------------------
    @callback
    def _apply_scan_interval(self, data: dict[str, dict[str, Any]]) -> None:
        active = any(st.get("on") and Family(st.get("family", "unknown")) in BOILER_FAMILIES
                     for st in data.values())
        self.update_interval = timedelta(
            seconds=ACTIVE_SCAN_INTERVAL if active else DEFAULT_SCAN_INTERVAL)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            data = await self.hass.async_add_executor_job(self.client.poll)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                for k, st in data.items():
                    _LOGGER.debug("poll %s on=%s ws=%s", k, st.get("on"), st.get("workstatus_hex"))
            # Poll fast while a boiler is running so the remaining-time countdown
            # ticks; drop back to the slow interval once everything is off.
            self._apply_scan_interval(data)
            return data
        except SwitcherAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:  # noqa: BLE001 - any transport/parse error = failed poll
            raise UpdateFailed(str(err)) from err
