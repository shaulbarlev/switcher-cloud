"""DataUpdateCoordinator for Switcher Cloud."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any, NoReturn

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SwitcherApiError, SwitcherAuthError, SwitcherClient
from .const import CONF_ACCOUNT, CONF_PASSWORD, DEFAULT_SCAN_INTERVAL, DOMAIN
from .devices import (
    Family,
    auto_shutdown_valid,
    body_auto_shutdown,
    body_bath_heater,
    body_light_toggle,
    body_onwall,
    body_plug,
    body_plug_auto_shutdown,
    body_plug_single,
    body_switch,
)

_LOGGER = logging.getLogger(__name__)

CONFIRM_INTERVAL = 0.4     # s between confirm polls while the device is about to change.
                           # Measured on the On Wall: the server acks a command in ~0.4 s
                           # (0x0201 push), but its status still shows the old state for
                           # up to ~2 s after, and no second push comes when it changes.
                           # Waiting 2 s per poll made a toggle take ~2.6 s; polling at
                           # 0.4 s catches the change within one interval of it happening.
CONFIRM_FAST_FOR = 8.0     # s of fast polling; after that, CONFIRM_SLOW_INTERVAL
CONFIRM_SLOW_INTERVAL = 2.0
POWER_WAIT = 8.0           # s of fast polling after a confirmed switch-on for the first
                           # power reading (On Wall: it lands ~3.5 s after the confirm)
CONFIRM_TIMEOUT = 30.0     # s before giving up and letting the regular poll catch up
RESET_AFTER_FAILURES = 3   # consecutive failed polls before a hard client reset
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
        # did -> event set by any 0x0103 push while a confirm loop waits on it
        self._push_events: dict[str, asyncio.Event] = {}
        self._failures = 0
        # did -> (seconds, monotonic time) of the last timed run we requested;
        # the countdown sensor seeds from it so an extended run is not clipped
        # by a stale cloud value.
        self.run_hint: dict[str, tuple[int, float]] = {}
        # did -> (HA context of the service call that last switched the device,
        # monotonic time). The switch entity stamps its confirmed on/off state
        # write with it, so "who turned the boiler on" survives a command that
        # came through the run timer number or the water heater rather than the
        # switch itself (HA only hands the context to the entity it targeted).
        self.command_context: dict[str, tuple[Context, float]] = {}
        # (did, gang) -> the on/off a command asked for, until the device
        # reports it or the command fails. The switch shows it at once; the
        # readings (power, timers) stay the device's own and arrive with the
        # confirm, so a quick flip of the switch never fakes a measurement.
        self.pending: dict[tuple[str, int], bool] = {}
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
        # the push body carries no DID, so wake every waiting confirm loop;
        # each one polls and checks its own device
        for ev in self._push_events.values():
            ev.set()
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
    def _raise_command_error(self, err: Exception) -> NoReturn:
        if isinstance(err, SwitcherAuthError):
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="invalid_auth") from err
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_failed",
                                 translation_placeholders={"error": str(err)}) from err

    async def async_set_switch(self, did: str, on: bool, gang: int | None = None,
                               run_seconds: int = 0, context: Context | None = None) -> None:
        """Send on/off for the device's family, then poll until the device
        reports the new state. The UI only flips once the device really changed.
        `gang` selects a line on multi-gang wall switches (0/None = all).
        `run_seconds` > 0 = timed run (boost): the device turns itself off after
        that many seconds (S216/bath heater isdelay/delayTime; plug isdelay).
        `context` is the HA context of the calling service, see command_context."""
        if context is not None:
            self.command_context[did] = (context, time.monotonic())
        key = (did, gang or 0)
        self.pending[key] = on
        self.async_update_listeners()
        try:
            await self._set_switch(did, on, gang, run_seconds)
        finally:
            # Confirmed, failed or timed out: from here the device's own state rules.
            if self.pending.pop(key, None) is not None:
                self.async_update_listeners()

    async def _set_switch(self, did: str, on: bool, gang: int | None, run_seconds: int) -> None:
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
            if secs:
                self.run_hint[did] = (secs, time.monotonic())
            elif not on:
                self.run_hint.pop(did, None)
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
        """Set the device's auto-shutdown limit.
        S216: parameter 0x0C14 via 0x0E01 (capture-verified).
        V2/Touch/V4/Mini (PLUG family boilers): sub 0x04 on the single-plug path
        (OgeSPlugModel.getAutoTimeCMD, same tail as aioswitcher set_auto_shutdown)."""
        async with self._command_locks.setdefault(did, asyncio.Lock()):
            st = (self.data or {}).get(did)
            if st is None:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_unavailable")
            pwd_plain = st.get("device_pwd") or ""
            fam = Family(st.get("family", "unknown"))
            if not auto_shutdown_valid(fam, seconds):
                raise ServiceValidationError(translation_domain=DOMAIN,
                                             translation_key="auto_off_range")
            if fam is Family.PLUG:
                body = body_plug_auto_shutdown(st["did_int"], self.client.uid, pwd_plain, seconds)
                job, args = self.client.send_control_single, (st["did_int"], body)
            else:
                body = body_auto_shutdown(st["did_int"], self.client.uid,
                                          self.client.pwd_block(pwd_plain), seconds)
                job, args = self.client.send_write_params, (body,)
            try:
                await self.hass.async_add_executor_job(job, *args)
            except (SwitcherApiError, OSError, TimeoutError) as err:
                self._raise_command_error(err)

    async def async_control(self, did: str, body: bytes,
                            done: Callable[[dict[str, Any]], bool] | None = None) -> None:
        """Generic control (covers, lights): send + confirm via `done(state)`."""
        async with self._command_locks.setdefault(did, asyncio.Lock()):
            await self._send_and_confirm(did, body, done)

    @staticmethod
    def _gang_state(st: dict, gang: int | None) -> bool | None:
        if gang and st.get("gangs"):
            g = st["gangs"]
            return g[gang] if gang < len(g) else None
        return st.get("on")

    async def _send_and_confirm(self, did: str, body: bytes,
                                done: Callable[[dict[str, Any]], bool] | None,
                                single_did: int | None = None) -> None:
        event = self._push_events.setdefault(did, asyncio.Event())
        event.clear()
        try:
            if single_did is not None:
                await self.hass.async_add_executor_job(
                    self.client.send_control_single, single_did, body)
            else:
                await self.hass.async_add_executor_job(self.client.send_control, body)
        except (SwitcherApiError, OSError, TimeoutError) as err:
            self._raise_command_error(err)
        # Confirm with a private fetch (does not touch last_update_success or
        # the scheduled poll), publishing once when the requested state shows.
        # The device ack arrives as a 0x0103 push, so wait for that and fall
        # back to a poll every CONFIRM_INTERVAL if it does not come.
        start = self.hass.loop.time()
        deadline = start + CONFIRM_TIMEOUT
        power_wait = False
        while self.hass.loop.time() < deadline:
            fast = self.hass.loop.time() - start < CONFIRM_FAST_FOR
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(),
                                       CONFIRM_INTERVAL if fast else CONFIRM_SLOW_INTERVAL)
            event.clear()
            try:
                data = await self.hass.async_add_executor_job(self.client.poll)
            except (SwitcherApiError, OSError, TimeoutError) as err:  # transient; keep trying
                _LOGGER.debug("confirm poll failed: %s", err)
                continue
            self._apply_scan_interval(data)
            self.async_set_updated_data(data)
            st = data.get(did, {})
            if done is None or done(st):
                # Switched on, but the snapshot that confirmed it was taken before
                # the device measured anything (power 0). The reading follows a few
                # seconds later; keep the fast polls going until it shows rather
                # than leave it to the regular poll. Capped: 0 can also be real,
                # when the boiler's thermostat is open.
                if st.get("on") and st.get("power_raw") == 0 and not power_wait:
                    power_wait = True
                    deadline = min(deadline, self.hass.loop.time() + POWER_WAIT)
                    continue
                if power_wait and st.get("power_raw") == 0:
                    continue
                break

    # -- scheduled poll ------------------------------------------------------------
    @callback
    def _apply_scan_interval(self, data: dict[str, dict[str, Any]]) -> None:
        active = any(st.get("on") and st.get("timed") for st in data.values())
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
            self._failures = 0
            return data
        except SwitcherAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:  # noqa: BLE001 - any transport/parse error = failed poll
            # The client reconnects on its own; if polls still fail several
            # times in a row something is wedged, so force a fresh socket
            # and listener. A reload did exactly this by hand once.
            self._failures += 1
            if self._failures % RESET_AFTER_FAILURES == 0:
                _LOGGER.warning("%s polls failed in a row (%s); resetting the cloud connection",
                                self._failures, err)
                await self.hass.async_add_executor_job(self.client.reset)
            raise UpdateFailed(str(err)) from err
