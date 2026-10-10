"""Switch platform: power for every switchable family, one per gang on wall
switches, plus the Runner child lock (as switcher_kis does)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import SWITCHABLE, Family, body_curtain_lock
from .entity import SwitcherCloudEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = []
    for did, st in coordinator.data.items():
        fam = Family(st.get("family", "unknown"))
        if fam is Family.CURTAIN or (fam is Family.MIX and st.get("line_type") == 12):
            entities.append(SwitcherCloudChildLock(coordinator, did))
        if fam not in SWITCHABLE or fam is Family.MIX:
            continue                        # mix lines: cover or light platform
        if fam is Family.SWITCH:
            for i in range(1, gang_count(st) + 1):
                entities.append(SwitcherCloudSwitch(coordinator, did, gang=i))
        else:
            entities.append(SwitcherCloudSwitch(coordinator, did))
    async_add_entities(entities)


def gang_count(st: dict[str, Any]) -> int:
    """Number of gangs on a wall switch. The 0x1343 JSON carries no explicit
    count; use the highest gang the device has reported on, else 1."""
    gangs = st.get("gangs") or []
    active = [i for i in range(1, min(8, len(gangs))) if gangs[i]]
    return max(active) if active else 1


class SwitcherCloudSwitch(SwitcherCloudEntity, SwitchEntity):
    """Power switch keyed on the cloud device id (+ gang for wall switches)."""

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
        st = self._st()
        if self._gang and st.get("gangs"):
            return bool(st["gangs"][self._gang])
        on = st.get("on")
        return None if on is None else bool(on)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # raw device status, for bug reports on unverified families
        return {"work_status": self._st().get("workstatus_hex")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, True, self._gang)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, False, self._gang)


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
