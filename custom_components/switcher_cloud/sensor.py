"""Sensor platform: current, energy and time-on from WorkStatus, for families that report them."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

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
        key="time_on",
        translation_key="time_on",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,   # per-session counter, resets
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda st: st.get("secs_on"),
        present=lambda st: st.get("secs_on") is not None,
    ),
    SwitcherSensorDescription(
        key="time_remaining",
        translation_key="time_remaining",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # WorkStatus lasttime: the device's own countdown to auto-off while on,
        # the same number the app shows on the boiler screen (yf/m1.java:348).
        # Zero when the device is off.
        value_fn=lambda st: (st.get("lasttime") or 0) if st.get("on") else 0,
        present=lambda st: st.get("lasttime") is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        SwitcherCloudSensor(coordinator, did, desc)
        for did, st in coordinator.data.items()
        if Family(st.get("family", "unknown")) in ENERGY_FAMILIES
        for desc in SENSORS
        if desc.present(st)
    )


class SwitcherCloudSensor(SwitcherCloudEntity, SensorEntity):
    entity_description: SwitcherSensorDescription

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str,
                 desc: SwitcherSensorDescription) -> None:
        super().__init__(coordinator, did)
        self.entity_description = desc
        # keep the pre-existing unique ids so registry entries survive
        legacy = {"current": "power", "energy": "energy", "time_on": "secs_on",
                  "time_remaining": "time_remaining"}[desc.key]
        self._attr_unique_id = f"switcher_cloud_{did}_{legacy}"

    @property
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(self._st())
