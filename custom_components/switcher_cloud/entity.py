"""Shared entity base for Switcher Cloud."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwitcherCloudCoordinator
from .devices import MODEL_NAMES, VERIFIED, Family


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
            model = f"{model} (beta, unverified)"
        connections = set()
        if st.get("dmac"):
            connections.add((CONNECTION_NETWORK_MAC, format_mac(st["dmac"])))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, did)},
            name=st.get("name") or f"Switcher {did}",
            manufacturer="Switcher",
            model=model,
            model_id=st.get("device_type"),
            sw_version=st.get("firmware"),
            connections=connections,
        )

    def _st(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._did) or {}

    @property
    def available(self) -> bool:
        st = self.coordinator.data.get(self._did)
        return super().available and bool(st) and bool(st.get("online", True))
