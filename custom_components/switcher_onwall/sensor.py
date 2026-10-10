"""Sensor platform — seconds on / energy proxy."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwitcherOnwallCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: SwitcherOnwallCoordinator = entry.runtime_data
    async_add_entities(
        [SwitcherOnwallSecondsSensor(coordinator, did)
         for did in coordinator.data.keys()]
    )


class SwitcherOnwallSecondsSensor(
    CoordinatorEntity[SwitcherOnwallCoordinator], SensorEntity
):
    """Seconds the boiler has been on (resets at off)."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "s"
    _attr_translation_key = None
    _attr_name = "Time on"

    def __init__(self, coordinator: SwitcherOnwallCoordinator, did: str) -> None:
        super().__init__(coordinator)
        self._did = did
        self._attr_unique_id = f"switcher_onwall_{did}_secs_on"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, did)},
            "name": (coordinator.data[did].get("name") or f"Switcher {did}"),
            "manufacturer": "Switcher",
            "model": coordinator.data[did].get("model"),
        }

    @property
    def native_value(self) -> int | None:
        st = self.coordinator.data.get(self._did)
        return None if st is None else st.get("secs_on")
