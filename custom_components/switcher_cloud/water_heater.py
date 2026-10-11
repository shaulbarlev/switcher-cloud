"""Water heater platform for boilers: On Wall and the V2/Touch/V4/Mini
(aioswitcher DeviceCategory.WATER_HEATER). The Heater (031F) is a separate
category there and a plain switch in switcher_kis, so it gets none.
On/off only, the cloud has no temperature."""
from __future__ import annotations

from typing import Any

from homeassistant.components.water_heater import (
    STATE_ELECTRIC,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .entity import SwitcherCloudEntity, async_add_new_devices

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_new_devices(
        coordinator, entry, async_add_entities,
        lambda did, st: [SwitcherCloudWaterHeater(coordinator, did)] if st.get("boiler") else [])


class SwitcherCloudWaterHeater(SwitcherCloudEntity, WaterHeaterEntity):
    """Boiler as a water heater: on/off only (the cloud API has no temperature)."""

    _attr_translation_key = "boiler"
    _attr_operation_list = [STATE_ELECTRIC, STATE_OFF]
    _attr_supported_features = (
        WaterHeaterEntityFeature.OPERATION_MODE | WaterHeaterEntityFeature.ON_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS   # required by the base class

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"switcher_cloud_{did}_heater"

    @property
    def current_operation(self) -> str | None:
        on = self._st().get("on")
        return None if on is None else (STATE_ELECTRIC if on else STATE_OFF)

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        await self.coordinator.async_set_switch(self._did, operation_mode != STATE_OFF, context=self._context)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, True, context=self._context)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, False, context=self._context)
