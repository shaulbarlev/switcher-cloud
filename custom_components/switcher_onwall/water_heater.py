"""Water-heater platform — the On Wall is a boiler."""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.water_heater import (
    STATE_OFF,
    STATE_ON,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwitcherOnwallCoordinator

OPERATION_LIST = [STATE_ON, STATE_OFF]


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: SwitcherOnwallCoordinator = entry.runtime_data
    async_add_entities(
        [SwitcherOnwallWaterHeater(coordinator, did)
         for did in coordinator.data.keys()]
    )


class SwitcherOnwallWaterHeater(
    CoordinatorEntity[SwitcherOnwallCoordinator], WaterHeaterEntity
):
    """Boiler as a water heater: operation on/off, no temp control (cloud API has none)."""

    _attr_operation_list = OPERATION_LIST
    _attr_supported_features = WaterHeaterEntityFeature.OPERATION_MODE
    _attr_name = None

    def __init__(self, coordinator: SwitcherOnwallCoordinator, did: str) -> None:
        super().__init__(coordinator)
        self._did = did
        self._attr_unique_id = f"switcher_onwall_{did}_heater"
        st = coordinator.data[did]
        self._attr_name = st.get("name") or f"Switcher {did}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, did)},
            "name": st.get("name") or f"Switcher {did}",
            "manufacturer": "Switcher",
            "model": st.get("model"),
        }

    @property
    def current_operation(self) -> str | None:
        st = self.coordinator.data.get(self._did)
        if st is None:
            return None
        return STATE_ON if st.get("on") else STATE_OFF

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        st = self.coordinator.data.get(self._did)
        if not st or not st.get("device_pwd"):
            raise HomeAssistantError("device password unavailable")
        try:
            await asyncio.to_thread(
                self.coordinator.client.set_switch,
                st["did_int"], st["device_pwd"], operation_mode == STATE_ON)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
