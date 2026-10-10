"""Switch platform — boiler power."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SwitcherCloudCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: SwitcherCloudCoordinator = entry.runtime_data

    def known_dids():
        return set(coordinator.data.keys())

    entities: list[SwitcherCloudSwitch] = [
        SwitcherCloudSwitch(coordinator, did) for did in known_dids()
    ]
    async_add_entities(entities)


class SwitcherCloudSwitch(CoordinatorEntity[SwitcherCloudCoordinator], SwitchEntity):
    """Boiler power switch, keyed on the cloud device id."""

    _attr_name = None

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator)
        self._did = did
        self._attr_unique_id = f"switcher_cloud_{did}_power"
        st = coordinator.data[did]
        self._attr_name = st.get("name") or f"Switcher {did}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, did)},
            "name": st.get("name") or f"Switcher {did}",
            "manufacturer": "Switcher",
            "model": st.get("model"),
        }

    @property
    def state_attrs(self) -> dict[str, Any] | None:
        return self.coordinator.data.get(self._did)

    @property
    def is_on(self) -> bool | None:
        st = self.coordinator.data.get(self._did)
        return None if st is None else bool(st.get("on"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        st = self.coordinator.data.get(self._did)
        if not st or not st.get("device_pwd"):
            raise HomeAssistantError("device password unavailable")
        import asyncio
        try:
            await asyncio.to_thread(
                self.coordinator.client.set_switch,
                st["did_int"], st["device_pwd"], on)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
