"""Sensor platform: power, current, energy, time remaining (and Breeze temperature) from WorkStatus."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from homeassistant.util import dt as dt_util

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
        device_class=SensorDeviceClass.TIMESTAMP,
        # Published as the moment the device will switch itself off; see
        # SwitcherCloudCountdown for how the seed is chosen.
        value_fn=lambda st: None,
        present=lambda st: st.get("remaining") is not None,
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
            cls = SwitcherCloudCountdown if desc.key == "time_remaining" else SwitcherCloudSensor
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


class SwitcherCloudCountdown(SwitcherCloudSensor):
    """When the device will switch itself off, as a timestamp.

    The cloud only refreshes the device's remaining-seconds field now and
    then, so polling it does not make a countdown. The app (yf/m1.java:348)
    takes the device value as a seed and counts down locally. Here the seed
    becomes a fixed "auto off at" timestamp: the frontend renders it as a live
    countdown on its own, and the state only changes when the seed moves, so
    the recorder is not written to every second.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_state_class = None
    _attr_native_unit_of_measurement = None

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 desc: SwitcherSensorDescription) -> None:
        super().__init__(coordinator, did, desc)
        self._seed: int | None = None          # seconds remaining at _seed_at
        self._seed_at: float = 0.0             # monotonic
        self._off_at: datetime | None = None   # wall clock, what we publish
        self._last_device: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._resync()

    def _local_value(self) -> int | None:
        if self._seed is None:
            return None
        return max(0, self._seed - int(time.monotonic() - self._seed_at))

    def _resync(self) -> None:
        st = self._st()
        on = bool(st.get("on"))
        device = int(st.get("remaining") or 0) if on else 0
        hint = self.coordinator.run_hint.get(self._did)
        hint_newer = bool(hint and hint[1] > self._seed_at)
        if hint_newer and on and hint:
            # we just asked for a timed run; the cloud value may still be the
            # previous run's, so seed from what we requested
            device = max(device, hint[0] - int(time.monotonic() - hint[1]))
        seed = next_seed(device, self._local_value(), on, self._last_device, hint_newer)
        self._last_device = device
        if seed is None:
            return
        self._seed, self._seed_at = seed, time.monotonic()
        self._off_at = (dt_util.utcnow() + timedelta(seconds=seed)) if (on and seed > 0) else None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._resync()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> datetime | None:
        if not self._st().get("on"):
            return None
        return self._off_at
