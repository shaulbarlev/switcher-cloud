"""Switch platform: power for every switchable family, one per gang on wall
switches, plus the Runner child lock (as switcher_kis does)."""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import AUTO_SHUTDOWN_WRITABLE, SWITCHABLE, Family, body_curtain_lock
from .entity import SwitcherCloudEntity, async_add_new_devices

PARALLEL_UPDATES = 0

COMMAND_CONTEXT_TTL = 60      # s; a confirm never takes longer than CONFIRM_TIMEOUT (30 s)

# Same names, fields and ranges as switcher_kis (services.py there), so an
# automation ports by changing the domain.
SERVICE_SET_AUTO_OFF = "set_auto_off"
SERVICE_TURN_ON_WITH_TIMER = "turn_on_with_timer"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    def factory(did: str, st: dict[str, Any]) -> list[SwitchEntity]:
        fam = Family(st.get("family", "unknown"))
        entities: list[SwitchEntity] = []
        if fam is Family.CURTAIN or (fam is Family.MIX and st.get("line_type") == 12):
            entities.append(SwitcherCloudChildLock(coordinator, did))
        if fam not in SWITCHABLE or fam is Family.MIX:
            return entities                 # mix lines: cover or light platform
        if fam is Family.SWITCH:
            entities.extend(SwitcherCloudSwitch(coordinator, did, gang=i)
                            for i in range(1, gang_count(st) + 1))
        else:
            entities.append(SwitcherCloudSwitch(coordinator, did))
        return entities

    async_add_new_devices(coordinator, entry, async_add_entities, factory)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_AUTO_OFF,
        {vol.Required("auto_off"): cv.time_period_str},
        "async_set_auto_off_service",
    )
    platform.async_register_entity_service(
        SERVICE_TURN_ON_WITH_TIMER,
        {vol.Required("timer_minutes"): vol.All(cv.positive_int, vol.Range(min=1, max=150))},
        "async_turn_on_with_timer_service",
    )


def gang_count(st: dict[str, Any]) -> int:
    """Number of gangs on a wall switch. The 0x1343 JSON carries no explicit
    count; use the highest gang the device has reported on, else 1."""
    gangs = st.get("gangs") or []
    active = [i for i in range(1, min(8, len(gangs))) if gangs[i]]
    return max(active) if active else 1


class SwitcherCloudSwitch(SwitcherCloudEntity, SwitchEntity):
    """Power switch keyed on the cloud device id (+ gang for wall switches)."""

    _attr_device_class = SwitchDeviceClass.SWITCH   # switcher_kis boiler class; services target it
    # raw hex is for bug reports, not for the recorder on every state change
    _unrecorded_attributes = frozenset({"work_status"})

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 gang: int | None = None) -> None:
        super().__init__(coordinator, did)
        self._gang = gang
        if gang:
            self._attr_unique_id = f"switcher_cloud_{did}_gang{gang}"
            self._attr_translation_key = "gang"
            self._attr_translation_placeholders = {"n": str(gang)}
        else:
            self._attr_unique_id = f"switcher_cloud_{did}_power"
            self._attr_name = None          # main entity: takes the device name
        if coordinator.data[did].get("family") == Family.PLUG.value \
                and not coordinator.data[did].get("boiler"):
            self._attr_device_class = SwitchDeviceClass.OUTLET   # 01A8 Power Plug

    @property
    def is_on(self) -> bool | None:
        # Optimistic: a commanded state shows at once, until the device confirms or the
        # command fails. Only the switch does this; power and timers wait for the device.
        pending = self.coordinator.pending.get((self._did, self._gang or 0))
        if pending is not None:
            return pending
        st = self._st()
        if self._gang and st.get("gangs"):
            return bool(st["gangs"][self._gang])
        on = st.get("on")
        return None if on is None else bool(on)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # raw device status, for bug reports on unverified families
        return {"work_status": self._st().get("workstatus_hex")}

    @callback
    def _handle_coordinator_update(self) -> None:
        # A confirmed on/off that was asked for through another entity (run
        # timer number, water heater) or whose confirmation took longer than
        # HA keeps a service context alive (5 s): write it with the context of
        # that service call, so the state change records the user who did it.
        pending = self.coordinator.command_context.get(self._did)
        if pending and self.hass and self.hass.states.get(self.entity_id) is not None:
            ctx, at = pending
            was_on = self.hass.states.get(self.entity_id).state == "on"
            if self.is_on != was_on and time.monotonic() - at < COMMAND_CONTEXT_TTL:
                self.async_set_context(ctx)
                self.coordinator.command_context.pop(self._did, None)
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, True, self._gang, context=self._context)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, False, self._gang, context=self._context)

    async def async_turn_on_with_timer_service(self, timer_minutes: int) -> None:
        if not self._st().get("timed"):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_timed")
        await self.coordinator.async_set_switch(self._did, True, run_seconds=timer_minutes * 60,
                                                context=self._context)

    async def async_set_auto_off_service(self, auto_off: timedelta) -> None:
        if Family(self._st().get("family", "unknown")) not in AUTO_SHUTDOWN_WRITABLE:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_auto_off")
        await self.coordinator.async_set_auto_shutdown(self._did, int(auto_off.total_seconds()))
        await self.coordinator.async_request_refresh()


class SwitcherCloudChildLock(SwitcherCloudEntity, SwitchEntity):
    """Runner child lock: sub 0x07 (OgeCurtainModel.getControlLockCMD, aioswitcher
    set_shutter_child_lock). State from switchData[1]."""

    _attr_translation_key = "child_lock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"switcher_cloud_{did}_child_lock"
        # the base names mix-panel lines "Line n"; this one is "Line n child lock"
        self._attr_translation_key = "line_child_lock" if self._st().get("line_no") else "child_lock"

    @property
    def is_on(self) -> bool | None:
        return self._st().get("locked")

    async def _set(self, locked: bool) -> None:
        st = self._st()
        body = body_curtain_lock(st["did_int"], self.coordinator.client.uid,
                                 self.coordinator.client.pwd_block(st.get("device_pwd") or ""),
                                 locked, st.get("line_no", 0))
        await self.coordinator.async_control(self._did, body, lambda s: s.get("locked") == locked)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
