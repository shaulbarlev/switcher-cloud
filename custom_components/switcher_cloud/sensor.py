"""Sensor platform: power, current, energy, remaining time, time on (and Breeze temperature) from WorkStatus."""
from __future__ import annotations

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
from .devices import ENERGY_FAMILIES, Family
from .entity import LocalClock, SwitcherCloudEntity, async_add_new_devices

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
        # Time until the device switches itself off, shown as "X minutes, Y
        # seconds" (the raw seconds are the `seconds` attribute). Ticks down
        # locally between polls; see SwitcherCloudTicker for how the seed is
        # chosen. Shown as "Remaining time", the switcher_kis name.
        value_fn=lambda st: (st.get("remaining") or 0) if st.get("on") else 0,
        present=lambda st: st.get("timed") and st.get("remaining") is not None,
    ),
    SwitcherSensorDescription(
        key="time_on",
        translation_key="time_on",
        # Time since the device switched on (device counter, i32@25 on the On
        # Wall), same "X minutes, Y seconds" text with a `seconds` attribute.
        # Ticks up locally between polls. 0 while off, like the app.
        value_fn=lambda st: (st.get("secs_on") or 0) if st.get("on") else 0,
        present=lambda st: st.get("timed") and st.get("secs_on") is not None,
    ),
    SwitcherSensorDescription(
        # Heater (031F): the limit is reported in WorkStatus. The app has no write
        # path; switcher_kis sends the type-1 set_auto_off packet, unverified on a
        # Heater, so here it stays a read-only sensor.
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

    def factory(did: str, st: dict[str, Any]) -> list[SensorEntity]:
        if Family(st.get("family", "unknown")) not in ENERGY_FAMILIES | {Family.AIRCON}:
            return []
        return [
            (SwitcherCloudTicker if desc.key in TICKING else SwitcherCloudSensor)(coordinator, did, desc)
            for desc in SENSORS if desc.present(st)
        ]

    async_add_new_devices(coordinator, entry, async_add_entities, factory)


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


def format_duration(seconds: int) -> str:
    """0 -> "0 seconds", 61 -> "1 minute, 1 second", 1500 -> "25 minutes, 0 seconds"."""
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    sec = f"{s} second" + ("" if s == 1 else "s")
    if m == 0:
        return sec
    return f"{m} minute" + ("" if m == 1 else "s") + f", {sec}"


class SwitcherCloudTicker(SwitcherCloudSensor):
    """A seconds value that keeps moving between polls (see LocalClock).
    "Remaining time" ticks down, "Time on" ticks up, one write per second
    while the device is on, nothing while it is off."""

    _attr_should_poll = False

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 desc: SwitcherSensorDescription) -> None:
        super().__init__(coordinator, did, desc)
        self._clock = LocalClock(TICKING[desc.key], coordinator, did)
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
        self._clock.resync(st, int(self.entity_description.value_fn(st) or 0))

    @callback
    def _handle_coordinator_update(self) -> None:
        self._resync()
        super()._handle_coordinator_update()

    @callback
    def _tick(self, _now) -> None:
        if self._clock.was_on and self.available:
            self.async_write_ha_state()

    def _seconds(self) -> int:
        if not self._st().get("on"):
            return 0
        return self._clock.value() or 0

    @property
    def native_value(self) -> str:
        return format_duration(self._seconds())

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        return {"seconds": self._seconds()}
