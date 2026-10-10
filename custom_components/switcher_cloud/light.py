"""Light platform.

Two kinds:
- Switcher Light SL01/SL02/SL03 and the light lines of Runner S11/S12 (mainType
  0F, lineType 3): plain on/off, sub 0x0A with the line number, exactly the
  packet aioswitcher's set_light sends and the entity switcher_kis builds.
- Ogemray colour lights (OgeLightModel, mainType 8): Switcher's app ships no UI
  for them (SDK residue), so the layout comes from the model's setters/getters
  only. On/off and RGB scaling are high-confidence; W/T semantics and
  colour-temperature direction are not. Not verified on hardware.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import LIGHT_EFFECTS, Family, body_light_color, body_light_toggle, rgbwt_bytes
from .entity import SwitcherCloudEntity

PARALLEL_UPDATES = 0

# T channel is 0..100 with unknown direction; expose a nominal CT range and map
# linearly (0 → warm 2700 K, 100 → cold 6500 K). Flip if hardware disagrees.
MIN_KELVIN, MAX_KELVIN = 2700, 6500
EFFECT_BY_NAME = {v: k for k, v in LIGHT_EFFECTS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[LightEntity] = []
    for did, st in coordinator.data.items():
        fam = Family(st.get("family", "unknown"))
        if fam is Family.LIGHT:
            entities.append(SwitcherCloudLight(coordinator, did))
        elif fam is Family.MIX and st.get("line_type") != 12:
            entities.append(SwitcherCloudMixLight(coordinator, did))
    async_add_entities(entities)


class SwitcherCloudMixLight(SwitcherCloudEntity, LightEntity):
    """On/off light line of an S11/S12/SL panel (OgeSPlugAfterPanS11Model)."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"switcher_cloud_{did}_light"

    @property
    def is_on(self) -> bool | None:
        on = self._st().get("on")
        return None if on is None else bool(on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_switch(self._did, False)


class SwitcherCloudLight(SwitcherCloudEntity, LightEntity):
    _attr_name = None
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [n for n in LIGHT_EFFECTS.values() if n != "none"]
    _attr_min_color_temp_kelvin = MIN_KELVIN
    _attr_max_color_temp_kelvin = MAX_KELVIN

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"switcher_cloud_{did}_light"

    # -- state --------------------------------------------------------------------
    @property
    def is_on(self) -> bool | None:
        return self._st().get("on")

    @property
    def color_mode(self) -> ColorMode:
        st = self._st()
        rgb = st.get("rgb") or (0, 0, 0)
        return ColorMode.RGB if any(rgb) else ColorMode.COLOR_TEMP

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        rgb = self._st().get("rgb")
        if not rgb or not any(rgb):
            return None
        m = max(rgb)
        return tuple(c * 255 // m for c in rgb)          # normalised hue; brightness separate

    @property
    def brightness(self) -> int | None:
        st = self._st()
        rgb = st.get("rgb")
        if rgb and any(rgb):
            return max(rgb)
        w = st.get("white_pct")
        return None if w is None else w * 255 // 100

    @property
    def color_temp_kelvin(self) -> int | None:
        t = self._st().get("temp_pct")
        return None if t is None else MIN_KELVIN + (MAX_KELVIN - MIN_KELVIN) * t // 100

    @property
    def effect(self) -> str | None:
        e = self._st().get("effect")
        name = LIGHT_EFFECTS.get(e or 0, "none")
        return None if name == "none" else name

    # -- commands -----------------------------------------------------------------
    async def async_turn_on(self, **kwargs: Any) -> None:
        st = self._st()
        pwd = self.coordinator.client.pwd_block(st.get("device_pwd") or "")
        uid = self.coordinator.client.uid
        if not kwargs:                                   # plain on: cheap toggle op
            await self.coordinator.async_control(
                self._did, body_light_toggle(st["did_int"], uid, pwd, True),
                lambda s: s.get("on") is True)
            return
        # build full colour state from current + requested
        rgb = list(kwargs.get(ATTR_RGB_COLOR) or self.rgb_color or (0, 0, 0))
        bri = kwargs.get(ATTR_BRIGHTNESS, self.brightness or 255)
        w = st.get("white_pct") or 0
        t = st.get("temp_pct") or 50
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            k = max(MIN_KELVIN, min(MAX_KELVIN, kwargs[ATTR_COLOR_TEMP_KELVIN]))
            t = (k - MIN_KELVIN) * 100 // (MAX_KELVIN - MIN_KELVIN)
            rgb = [0, 0, 0]                              # CT mode: colour off, white on
            w = bri * 100 // 255
        elif any(rgb):
            m = max(rgb) or 1
            rgb = [c * bri // m for c in rgb]            # brightness = scale RGB
        else:
            w = bri * 100 // 255
        effect = EFFECT_BY_NAME.get(kwargs.get(ATTR_EFFECT, ""), st.get("effect") or 0)
        body = body_light_color(st["did_int"], uid, pwd, True,
                                rgbwt_bytes(*rgb, w_pct=w, t_pct=t),
                                special_way=effect)
        await self.coordinator.async_control(self._did, body, lambda s: s.get("on") is True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        st = self._st()
        body = body_light_toggle(st["did_int"], self.coordinator.client.uid,
                                 self.coordinator.client.pwd_block(st.get("device_pwd") or ""),
                                 False)
        await self.coordinator.async_control(self._did, body, lambda s: s.get("on") is False)
