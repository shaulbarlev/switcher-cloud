"""Number platform: a timed run ("boost") for boilers.

Setting the number to N minutes turns the device ON with isdelay = N*60 s, so
the device itself switches off after N minutes (OgeSPlugS216Model/ei.b.w:
sub 0x23 with isdelay seconds; app UI a1/e2.java:437-456, 10 s granularity).
Setting 0 just turns it off. While the device runs, the number shows the
minutes left (from the remaining seconds the device reports in WorkStatus,
refreshed every poll), so it counts down and reads 0 once the device is off.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import (AUTO_SHUTDOWN_WRITABLE, ON_WALL_AUTO_SHUTDOWN_RANGE,
                      PLUG_AUTO_SHUTDOWN_RANGE, Family)
from .entity import SwitcherCloudEntity, async_add_new_devices

PARALLEL_UPDATES = 0

MAX_MINUTES = 90          # the app's timer picker stops at 90 minutes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    def factory(did: str, st: dict[str, Any]) -> list[NumberEntity]:
        if not st.get("timed"):
            return []
        entities: list[NumberEntity] = [SwitcherCloudRunTimer(coordinator, did)]
        if Family(st.get("family", "unknown")) in AUTO_SHUTDOWN_WRITABLE:
            entities.append(SwitcherCloudAutoShutdown(coordinator, did))
        return entities

    async_add_new_devices(coordinator, entry, async_add_entities, factory)


class SwitcherCloudRunTimer(SwitcherCloudEntity, NumberEntity):
    _attr_translation_key = "run_timer"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_MINUTES
    _attr_native_step = 1            # the value is a countdown, so any minute is valid
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"switcher_cloud_{did}_run_timer"
        self._requested: int = 0

    @callback
    def _handle_coordinator_update(self) -> None:
        # Once the device is off the request is spent.
        if self._requested and not self._st().get("on"):
            self._requested = 0
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        """Minutes left on the current run, counting down with each poll (the
        device's own countdown, so it is right after a restart and for runs
        started from the app). 0 when off. Falls back to the requested length
        until the first poll reports a remaining time."""
        st = self._st()
        if not st.get("on"):
            return 0
        remaining = st.get("remaining") or 0
        return -(-remaining // 60) if remaining > 0 else self._requested

    async def async_set_native_value(self, value: float) -> None:
        minutes = int(value)
        if minutes <= 0:
            self._requested = 0
            await self.coordinator.async_set_switch(self._did, False)
        else:
            await self.coordinator.async_set_switch(
                self._did, True, run_seconds=minutes * 60)
            self._requested = minutes       # set after confirm so the reset can't race
        self.async_write_ha_state()


class SwitcherCloudAutoShutdown(SwitcherCloudEntity, NumberEntity):
    """The device's own auto-shutdown limit, the same setting as in the app.

    S216: read every poll via a 0x0E02 parameter read (param 0x0C14), written
    with 0x0E01. Capture-verified 2026-10-10. V2/Touch/V4/Mini: read from
    WorkStatus (autoclosing i32 @22), written with sub 0x04, the same packet
    aioswitcher's set_auto_shutdown sends over the LAN.
    In minutes. S216: 61 to 1439, the app's own range (the app cannot turn
    auto shutdown off, so there is no 0). V2/Touch/V4/Mini: 60 to 1439, the
    range aioswitcher sends. Anything else is rejected before the write.
    """

    _attr_translation_key = "auto_shutdown"
    _attr_mode = NumberMode.BOX
    _attr_native_max_value = 24 * 60 - 1
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"switcher_cloud_{did}_auto_shutdown"
        fam = Family(self._st().get("family", "unknown"))
        lo = PLUG_AUTO_SHUTDOWN_RANGE[0] if fam is Family.PLUG else ON_WALL_AUTO_SHUTDOWN_RANGE[0]
        self._attr_native_min_value = lo // 60

    @property
    def native_value(self) -> float | None:
        s = self._st().get("auto_shutdown_s")
        return None if s is None else round(s / 60)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_auto_shutdown(self._did, int(value) * 60)
        await self.coordinator.async_request_refresh()
