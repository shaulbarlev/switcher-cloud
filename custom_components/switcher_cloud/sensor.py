"""Sensor platform: power, current, energy, time remaining (and Breeze temperature) from WorkStatus."""
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
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import ENERGY_FAMILIES, Family, next_seed
from .entity import SwitcherCloudEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SwitcherSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], float | int | None]
    present: Callable[[dict[str, Any]], bool]


SENSORS: tuple[SwitcherSensorDescription, ...] = (
    SwitcherSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        # aioswitcher reads the same field as watts (power_consumption) and
        # derives amps as watts / 220, which is what the app's "/220" is.
        value_fn=lambda st: st.get("power_raw"),
        present=lambda st: st.get("power_raw") is not None,
    ),
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
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # Seconds until the device switches itself off. Ticks down locally
        # between polls; see SwitcherCloudTicker for how the seed is chosen.
        value_fn=lambda st: (st.get("remaining") or 0) if st.get("on") else 0,
        present=lambda st: st.get("remaining") is not None,
    ),
    SwitcherSensorDescription(
        key="time_on",
        translation_key="time_on",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # Seconds since the device switched on (device counter, i32@25 on the
        # On Wall). Ticks up locally between polls. 0 while off, like the app.
        value_fn=lambda st: (st.get("secs_on") or 0) if st.get("on") else 0,
        present=lambda st: st.get("secs_on") is not None,
    ),
    SwitcherSensorDescription(
        # Heater (031F): the limit is reported in WorkStatus but neither the app
        # nor aioswitcher has a write path for it, so it is a read-only sensor.
        key="auto_shutdown",
        translation_key="auto_shutdown",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda st: round((st.get("auto_shutdown_s") or 0) / 60),
        present=lambda st: st.get("family") == Family.BATH_HEATER.value
        and st.get("auto_shutdown_s") is not None,
    ),
    SwitcherSensorDescription(
        # Breeze: room temperature, the one thing the cloud gives without IR codes.
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda st: st.get("temperature"),
        present=lambda st: st.get("temperature") is not None,
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
        if Family(st.get("family", "unknown")) not in ENERGY_FAMILIES | {Family.AIRCON}:
            continue
        for desc in SENSORS:
            if not desc.present(st):
                continue
            cls = SwitcherCloudTicker if desc.key in TICKING else SwitcherCloudSensor
            entities.append(cls(coordinator, did, desc))
    async_add_entities(entities)


class SwitcherCloudSensor(SwitcherCloudEntity, SensorEntity):
    entity_description: SwitcherSensorDescription

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 desc: SwitcherSensorDescription) -> None:
        super().__init__(coordinator, did)
        self.entity_description = desc
        # keep the pre-existing unique ids so registry entries survive: the
        # amps sensor was born as "_power", so the watts one takes "_power_w".
        legacy = {"current": "power", "power": "power_w"}.get(desc.key, desc.key)
        self._attr_unique_id = f"switcher_cloud_{did}_{legacy}"

    @property
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(self._st())


TICKING = {"time_remaining": -1, "time_on": +1}   # key -> tick direction per second


class SwitcherCloudTicker(SwitcherCloudSensor):
    """A seconds value that keeps moving between polls.

    The cloud only refreshes the device's timer fields now and then, and
    often hands back a stale copy, so polling alone does not make a clock.
    The app (yf/m1.java:348) takes the device value as a seed and moves it
    locally, re-syncing only when a fresh value differs by more than 3 s.
    Same here: "Time remaining" ticks down, "Time on" ticks up, one write per
    second while the device is on, nothing while it is off.
    """

    _attr_should_poll = False

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 desc: SwitcherSensorDescription) -> None:
        super().__init__(coordinator, did, desc)
        self._dir = TICKING[desc.key]
        self._seed: int | None = None          # device value at _seed_at
        self._seed_at: float = 0.0             # monotonic
        self._last_device: int | None = None
        self._was_on = False
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

    def _local_value(self) -> int | None:
        if self._seed is None:
            return None
        return max(0, self._seed + self._dir * int(time.monotonic() - self._seed_at))

    def _resync(self) -> None:
        st = self._st()
        on = bool(st.get("on"))
        device = int(self.entity_description.value_fn(st) or 0)
        local = self._local_value()
        if self._dir < 0:
            hint = self.coordinator.run_hint.get(self._did)
            hint_newer = bool(hint and hint[1] > self._seed_at)
            if hint_newer and on and hint:
                # we just asked for a timed run; the cloud copy may still be
                # the previous run's, so seed from what we requested
                device = max(device, hint[0] - int(time.monotonic() - hint[1]))
            seed = next_seed(device, local, on, self._last_device, hint_newer)
        else:
            # up-counter: a stale repeat is LOWER than the local clock, so a
            # fresh value is one that moved ahead; also re-seed on off -> on
            if not on:
                seed = 0
            elif local is None or not self._was_on or device > local + 3:
                seed = device
            else:
                seed = None
        self._last_device = device
        self._was_on = on
        if seed is not None:
            self._seed, self._seed_at = seed, time.monotonic()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._resync()
        super()._handle_coordinator_update()

    @callback
    def _tick(self, _now) -> None:
        if self._was_on and self.available:
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | int | None:
        if not self._st().get("on"):
            return 0
        return self._local_value()
