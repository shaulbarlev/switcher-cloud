"""Number platform — timed run ("boost") for boiler-type devices.

Setting the number to N minutes turns the device ON with isdelay = N*60 s, so
the device itself switches off after N minutes (OgeSPlugS216Model/ei.b.w:
sub 0x23 with isdelay seconds; app UI a1/e2.java:437-456, 10 s granularity).
Setting 0 just turns it off. The device's remaining countdown is not reported
in WorkStatus (only via 0x0402 reports), so the number reflects the last
requested value until the device turns off.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import BOILER_FAMILIES, Family
from .entity import SwitcherCloudEntity

PARALLEL_UPDATES = 0

MAX_MINUTES = 180


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        SwitcherCloudRunTimer(coordinator, did)
        for did, st in coordinator.data.items()
        if Family(st.get("family", "unknown")) in BOILER_FAMILIES
    )


class SwitcherCloudRunTimer(SwitcherCloudEntity, NumberEntity):
    _attr_translation_key = "run_timer"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_MINUTES
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-play-outline"

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"switcher_cloud_{did}_run_timer"
        self._requested: int = 0

    @property
    def native_value(self) -> float | None:
        # once the device reports off, the timer is spent
        if not self._st().get("on"):
            self._requested = 0
        return self._requested

    async def async_set_native_value(self, value: float) -> None:
        minutes = int(value)
        self._requested = minutes
        if minutes <= 0:
            await self.coordinator.async_set_switch(self._did, False)
        else:
            await self.coordinator.async_set_switch(
                self._did, True, run_seconds=minutes * 60)
        self.async_write_ha_state()
