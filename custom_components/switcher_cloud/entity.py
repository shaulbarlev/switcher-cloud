"""Shared entity base for Switcher Cloud."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import MODEL_NAMES, VERIFIED, Family


@callback
def async_add_new_devices(
    coordinator: SwitcherCloudCoordinator,
    entry: SwitcherCloudConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[str, dict[str, Any]], Iterable[Entity]],
) -> None:
    """Add entities for every device now and for devices that join the
    account later: `factory(did, state)` runs once per coordinator key."""
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new: list[Entity] = []
        for did, st in (coordinator.data or {}).items():
            if did in known:
                continue
            known.add(did)
            new.extend(factory(did, st))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class SwitcherCloudEntity(CoordinatorEntity[SwitcherCloudCoordinator]):
    """Base: one HA device per cloud DID, availability from online flag."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SwitcherCloudCoordinator, did: str) -> None:
        super().__init__(coordinator)
        self._did = did
        st = coordinator.data[did]
        fam = Family(st.get("family", "unknown"))
        model = MODEL_NAMES.get(fam, "Unknown device")
        if fam not in VERIFIED:
            model = f"{model} (unverified)"
        # Mix panels: one HA device per physical panel (lines share the DID);
        # entities are distinguished by name, not by device.
        device_key = str(st.get("did")) if fam is Family.MIX else did
        connections = set()
        if st.get("dmac"):
            connections.add((CONNECTION_NETWORK_MAC, format_mac(st["dmac"])))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_key)},
            name=st.get("name") or f"Switcher {device_key}",
            manufacturer="Switcher",
            model=model,
            model_id=st.get("device_type"),
            sw_version=st.get("firmware"),
            connections=connections,
        )
        if fam is Family.MIX and st.get("line_no"):
            self._attr_translation_key = "line"
            self._attr_translation_placeholders = {"n": str(st["line_no"])}

    def _st(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._did) or {}

    @property
    def available(self) -> bool:
        st = self.coordinator.data.get(self._did)
        return super().available and bool(st) and bool(st.get("online", True))
