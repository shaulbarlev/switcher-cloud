"""Cover platform for Switcher Runner shutters (OgeCurtainModel; Runner, Runner
Mini, and the runner lines of S11/S12). Not verified on hardware through the
cloud, but the sub-ops are the ones aioswitcher sends over the LAN: open and
close are set-position 100 and 0, stop is sub 0x02 (switcher_kis cover.py)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import Family, body_curtain_action, body_curtain_position
from .entity import SwitcherCloudEntity

PARALLEL_UPDATES = 0

ACTION_STOP = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        SwitcherCloudCover(coordinator, did)
        for did, st in coordinator.data.items()
        if Family(st.get("family", "unknown")) is Family.CURTAIN
        or (Family(st.get("family", "unknown")) is Family.MIX and st.get("line_type") == 12)
    )


class SwitcherCloudCover(SwitcherCloudEntity, CoverEntity):
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP | CoverEntityFeature.SET_POSITION
    )
    _attr_name = None

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"switcher_cloud_{did}_cover"

    @property
    def is_closed(self) -> bool | None:
        on = self._st().get("on")
        return None if on is None else not on

    @property
    def is_opening(self) -> bool | None:
        return self._st().get("moving_up")

    @property
    def is_closing(self) -> bool | None:
        return self._st().get("moving_down")

    @property
    def current_cover_position(self) -> int | None:
        return self._st().get("position")

    async def _go(self, position: int) -> None:
        st = self._st()
        body = body_curtain_position(
            st["did_int"], self.coordinator.client.uid,
            self.coordinator.client.pwd_block(st.get("device_pwd") or ""),
            position, st.get("line_no", 0))
        await self.coordinator.async_control(
            self._did, body,
            lambda s: s.get("moving_up") or s.get("moving_down") or s.get("position") == position)

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._go(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._go(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._go(int(kwargs[ATTR_POSITION]))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        st = self._st()
        body = body_curtain_action(
            st["did_int"], self.coordinator.client.uid,
            self.coordinator.client.pwd_block(st.get("device_pwd") or ""),
            ACTION_STOP, st.get("line_no", 0),
            moving=bool(st.get("moving_up") or st.get("moving_down")))
        await self.coordinator.async_control(
            self._did, body, lambda s: not (s.get("moving_up") or s.get("moving_down")))
