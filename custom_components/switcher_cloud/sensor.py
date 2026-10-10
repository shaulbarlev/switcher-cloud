"""Sensor platform: current, energy and time remaining from WorkStatus, for families that report them."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import ENERGY_FAMILIES, Family
from .entity import SwitcherCloudEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SwitcherSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], float | int | None]
    present: Callable[[dict[str, Any]], bool]


SENSORS: tuple[SwitcherSensorDescription, ...] = (
    SwitcherSensorDescription(
        key="current",
        translation_key="current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        # vendor convention: raw / 220 (OgeSPlugS216Model.parseWorkStatus)
        value_fn=lambda st: None if st.get("power_raw") is None else st["power_raw"] / 220.0,
        present=lambda st: st.get("power_raw") is not None,
    ),
    SwitcherSensorDescription(
        key="energy",
        translation_key="energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda st: st.get("energy_kwh"),
        present=lambda st: st.get("energy_kwh") is not None,
    ),
    SwitcherSensorDescription(
        key="time_remaining",
        translation_key="time_remaining",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # WorkStatus u16@21: seconds until the device switches itself off.
        # The device keeps the last value while off, so report 0 then.
        value_fn=lambda st: (st.get("remaining") or 0) if st.get("on") else 0,
        present=lambda st: st.get("remaining") is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for did, st in coordinator.data.items():
        if Family(st.get("family", "unknown")) not in ENERGY_FAMILIES:
            continue
        for desc in SENSORS:
            if not desc.present(st):
                continue
            cls = SwitcherCloudCountdown if desc.key == "time_remaining" else SwitcherCloudSensor
            entities.append(cls(coordinator, did, desc))
    async_add_entities(entities)


class SwitcherCloudSensor(SwitcherCloudEntity, SensorEntity):
    entity_description: SwitcherSensorDescription

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 desc: SwitcherSensorDescription) -> None:
        super().__init__(coordinator, did)
        self.entity_description = desc
        # keep the pre-existing unique ids so registry entries survive
        legacy = {"current": "power", "energy": "energy", "time_remaining": "time_remaining"}[desc.key]
        self._attr_unique_id = f"switcher_cloud_{did}_{legacy}"

    @property
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(self._st())


class SwitcherCloudCountdown(SwitcherCloudSensor):
    """Time remaining that ticks every second.

    The cloud only refreshes the device's remaining-seconds field now and
    then, so polling it does not make a countdown. The app does what this
    does (yf/m1.java:348): take the device value as a seed and count down
    locally, re-syncing whenever a new seed differs by more than 3 s.
    """

    _attr_should_poll = False

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 desc: SwitcherSensorDescription) -> None:
        super().__init__(coordinator, did, desc)
        self._seed: int | None = None
        self._seed_at: float = 0.0
        self._unsub_tick = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._resync()
        self._unsub_tick = async_track_time_interval(
            self.hass, self._tick, timedelta(seconds=1))

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None
        await super().async_will_remove_from_hass()

    def _resync(self) -> None:
        st = self._st()
        device = (st.get("remaining") or 0) if st.get("on") else 0
        local = self._local_value()
        if local is None or abs(local - device) > 3 or device == 0:
            self._seed = device
            self._seed_at = time.monotonic()

    def _local_value(self) -> int | None:
        if self._seed is None:
            return None
        return max(0, self._seed - int(time.monotonic() - self._seed_at))

    @callback
    def _handle_coordinator_update(self) -> None:
        self._resync()
        super()._handle_coordinator_update()

    @callback
    def _tick(self, _now) -> None:
        if self._seed:
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | int | None:
        if not self._st().get("on"):
            return 0
        return self._local_value()
