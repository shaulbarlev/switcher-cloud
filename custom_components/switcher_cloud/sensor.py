"""Sensor platform for Switcher Cloud boiler (S216 WorkStatus fields)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwitcherCloudCoordinator

from .devices import ENERGY_FAMILIES, Family


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: SwitcherCloudCoordinator = entry.runtime_data
    async_add_entities(
        ent
        for did, st in coordinator.data.items()
        if Family(st.get("family", "unknown")) in ENERGY_FAMILIES
        for ent in (
            SwitcherCloudSecondsSensor(coordinator, did),
            SwitcherCloudPowerSensor(coordinator, did),
            SwitcherCloudEnergySensor(coordinator, did),
        )
    )


def _base(coordinator: SwitcherCloudCoordinator, did: str) -> dict:
    st = coordinator.data[did]
    return {
        "identifiers": {(DOMAIN, did)},
        "name": st.get("name") or f"Switcher {did}",
        "manufacturer": "Switcher",
        "model": st.get("model"),
    }


def _st(coordinator: SwitcherCloudCoordinator, did: str) -> dict[str, Any]:
    return coordinator.data.get(did) or {}


class SwitcherCloudSecondsSensor(
    CoordinatorEntity[SwitcherCloudCoordinator], SensorEntity
):
    """Seconds-on counter (opentime from WorkStatus)."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "s"
    _attr_name = "Time on"

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator)
        self._did = did
        self._attr_unique_id = f"switcher_cloud_{did}_secs_on"
        self._attr_device_info = _base(coordinator, did)

    @property
    def native_value(self) -> int | None:
        return _st(self.coordinator, self._did).get("secs_on")


class SwitcherCloudPowerSensor(
    CoordinatorEntity[SwitcherCloudCoordinator], SensorEntity
):
    """Instantaneous current draw (WorkStatus int32@9 / 220; unit A per the device)."""

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator)
        self._did = did
        self._attr_unique_id = f"switcher_cloud_{did}_power"
        self._attr_device_info = _base(coordinator, did)

    @property
    def native_value(self) -> float | None:
        v = _st(self.coordinator, self._did).get("power_raw")
        return None if v is None else v / 220.0


class SwitcherCloudEnergySensor(
    CoordinatorEntity[SwitcherCloudCoordinator], SensorEntity
):
    """Energy counter from the device (WorkStatus int32@17, kWh)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator)
        self._did = did
        self._attr_unique_id = f"switcher_cloud_{did}_energy"
        self._attr_device_info = _base(coordinator, did)

    @property
    def native_value(self) -> float | None:
        return _st(self.coordinator, self._did).get("energy_kwh")
