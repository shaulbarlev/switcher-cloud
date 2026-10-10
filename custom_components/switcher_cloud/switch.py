"""Switch platform — power for every switchable family; per-gang for wall switches."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwitcherCloudCoordinator
from .devices import SWITCHABLE, VERIFIED, Family

_LOGGER = logging.getLogger(__name__)


def device_info(st: dict, did: str) -> dict:
    fam = Family(st.get("family", "unknown"))
    model = st.get("model") or fam.value
    if fam not in VERIFIED:
        model = f"{model} — beta, unverified"
    return {
        "identifiers": {(DOMAIN, did)},
        "name": st.get("name") or f"Switcher {did}",
        "manufacturer": "Switcher",
        "model": model,
    }


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: SwitcherCloudCoordinator = entry.runtime_data
    entities: list[SwitchEntity] = []
    for did, st in coordinator.data.items():
        fam = Family(st.get("family", "unknown"))
        if fam not in SWITCHABLE:
            continue
        if fam is Family.SWITCH and st.get("gangs"):
            # one entity per populated gang (index 1..7); the firmware reports
            # 8 bytes, we expose gangs that the device has ever reported on OR
            # that are non-zero; fall back to gang 1..2 when nothing is on.
            gangs = st["gangs"]
            active = [i for i in range(1, 8) if gangs[i]]
            count = max(active) if active else 1
            for i in range(1, count + 1):
                entities.append(SwitcherCloudSwitch(coordinator, did, gang=i))
        else:
            entities.append(SwitcherCloudSwitch(coordinator, did))
    async_add_entities(entities)


class SwitcherCloudSwitch(CoordinatorEntity[SwitcherCloudCoordinator], SwitchEntity):
    """Power switch keyed on the cloud device id (+ gang for wall switches)."""

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 gang: int | None = None) -> None:
        super().__init__(coordinator)
        self._did = did
        self._gang = gang
        st = coordinator.data[did]
        if gang:
            self._attr_unique_id = f"switcher_cloud_{did}_gang{gang}"
            self._attr_name = f"Switch {gang}"
        else:
            self._attr_unique_id = f"switcher_cloud_{did}_power"
            self._attr_name = None
        self._attr_device_info = device_info(st, did)

    @property
    def available(self) -> bool:
        st = self.coordinator.data.get(self._did)
        return super().available and bool(st) and st.get("online", True)

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data.get(self._did)
        if st is None:
            return None
        if self._gang and st.get("gangs"):
            return bool(st["gangs"][self._gang])
        return None if st.get("on") is None else bool(st.get("on"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.coordinator.data.get(self._did) or {}
        return {k: st.get(k) for k in ("family", "main_type", "sub_type", "line_no", "dmac")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, True, self._gang)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, False, self._gang)
