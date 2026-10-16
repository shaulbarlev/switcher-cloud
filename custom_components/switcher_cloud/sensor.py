"""Sensor platform: power, current, energy, remaining time, time on (and Breeze temperature) from WorkStatus."""
from __future__ import annotations

import logging
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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import UNDEFINED

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import ENERGY_FAMILIES, Family
from .entity import ClockData, LocalClock, SwitcherCloudEntity, async_add_new_devices

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SwitcherSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], float | int | None]
    present: Callable[[dict[str, Any]], bool]


def watts(st: dict[str, Any]) -> int | None:
    """The device's last power reading, in W, 0 while the device is off.

    WorkStatus is a snapshot the device sends at a state change and now and
    then while running (seen: 33 s and 23 min after switching on), not a
    value sampled per poll. Its power field is simply the last measurement
    the device made, so the snapshot taken when it switches off can still
    carry the running figure — an On Wall sat at 2693 W for five hours
    while off (2026-10-11). Off means no current can flow, so report 0.
    While on, 0 is real: the boiler's own thermostat has opened and the
    element is not drawing.
    """
    raw = st.get("power_raw")
    if raw is None:
        return None
    return 0 if st.get("on") is False else raw


SENSORS: tuple[SwitcherSensorDescription, ...] = (
    SwitcherSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        # aioswitcher reads the same field as watts (power_consumption) and
        # derives amps as watts / 220, which is what the app's "/220" is.
        value_fn=watts,
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
        value_fn=lambda st: None if (w := watts(st)) is None else w / 220.0,
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
    _migrate_power_sensors(hass, entry)

    def factory(did: str, st: dict[str, Any]) -> list[SensorEntity]:
        if Family(st.get("family", "unknown")) not in ENERGY_FAMILIES | {Family.AIRCON}:
            return []
        return [
            (SwitcherCloudTicker if desc.key in TICKING else SwitcherCloudSensor)(coordinator, did, desc)
            for desc in SENSORS if desc.present(st)
        ]

    async_add_new_devices(coordinator, entry, async_add_entities, factory)


def _migrate_power_sensors(hass: HomeAssistant, entry: SwitcherCloudConfigEntry) -> None:
    """Give the watts sensor back its original identity.

    Releases 1.0.0 to 1.0.2 shipped the sensor born as `<did>_power` (W) as
    an amps sensor, and the watts came back under `<did>_power_w`. On an
    install from before that, `sensor.<name>_power` therefore turned into
    "Current" in A — its history stopped (HA refuses a unit change on a
    statistic) and anything reading it got ~11 instead of ~2500. Now the keys
    are `_power` (W) and `_current` (A), and the entity ids follow: the watts
    sensor takes `sensor.<name>_power` back, the amps one moves to
    `sensor.<name>_current`. Statistics follow a renamed entity id, so the W
    history resumes under the id it was recorded for. An install from 1.0.1
    or 1.0.2 only has its unique ids renamed; its entity ids already said
    the right thing when they were created.
    """
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    amps = {e.unique_id[: -len("_power")]: e for e in entries
            if e.domain == "sensor" and e.unique_id.endswith("_power")
            and e.original_device_class == SensorDeviceClass.CURRENT}
    watts = {e.unique_id[: -len("_power_w")]: e for e in entries
             if e.domain == "sensor" and e.unique_id.endswith("_power_w")}
    for stem, old_amps in amps.items():
        old_watts = watts.get(stem)
        amps_id, watts_id = old_amps.entity_id, old_watts.entity_id if old_watts else None
        # Born as "Power": hand the id to the watts sensor and move the amps aside.
        swap = old_watts is not None and amps_id.endswith("_power")
        if swap:
            amps_id = amps_id[: -len("_power")] + "_current"
            if registry.async_get(amps_id) is not None:
                amps_id = old_amps.entity_id  # taken; keep the old id rather than invent one
                swap = False
        # The amps sensor's unique id is what the watts one needs, so free it first.
        registry.async_update_entity(old_amps.entity_id, new_unique_id=f"{stem}_current",
                                     new_entity_id=amps_id if swap else UNDEFINED)
        if old_watts is not None:
            registry.async_update_entity(old_watts.entity_id, new_unique_id=f"{stem}_power",
                                         new_entity_id=old_amps.entity_id if swap else UNDEFINED)
        _LOGGER.info("migrated power sensors for %s: amps -> %s, watts -> %s",
                     stem, amps_id, old_amps.entity_id if swap else watts_id)


class SwitcherCloudSensor(SwitcherCloudEntity, SensorEntity):
    entity_description: SwitcherSensorDescription

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 desc: SwitcherSensorDescription) -> None:
        super().__init__(coordinator, did)
        self.entity_description = desc
        self._attr_unique_id = f"switcher_cloud_{did}_{desc.key}"

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


class SwitcherCloudTicker(SwitcherCloudSensor, RestoreEntity):
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
        if (saved := await self.async_get_last_extra_data()) is not None:
            self._clock.restore(saved.as_dict())
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
    def extra_restore_state_data(self) -> ClockData:
        return ClockData(self._clock.dump())

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        return {"seconds": self._seconds()}
