"""Switch platform — power for every switchable family; per-gang for wall switches."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import SWITCHABLE, Family
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
        if fam not in SWITCHABLE:
            continue
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

    @property
    def is_on(self) -> bool | None:
        st = self._st()
        if self._gang and st.get("gangs"):
            return bool(st["gangs"][self._gang])
        on = st.get("on")
        return None if on is None else bool(on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, True, self._gang)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, False, self._gang)
