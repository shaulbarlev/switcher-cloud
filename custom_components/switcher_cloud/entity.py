"""Shared entity base for Switcher Cloud."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwitcherCloudConfigEntry, SwitcherCloudCoordinator
from .devices import MODEL_NAMES, VERIFIED, Family, next_seed


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


class LocalClock:
    """A device seconds counter kept moving between polls.

    The cloud only refreshes the device's timer fields now and then, and
    often hands back a stale copy, so polling alone does not make a clock.
    The app (yf/m1.java:348) takes the device value as a seed and moves it
    locally, re-syncing only when a fresh value differs by more than 3 s.
    Used by the Remaining time / Time on sensors and the Run timer number.
    Host entities call clock.resync(st, device_value) on every coordinator
    update and read clock.value().

    Restart: the host entities save the clock (dump) and restore it on start.
    Without that, the first poll after a restart seeded the clock from the
    cloud, which keeps the snapshot taken at switch-on: on 2026-10-14 the
    countdown jumped from 5 min 31 s to 21 min 30 s and Time on from 16 min
    to 1 s until the device's next real report. After a restore the cloud
    value is believed only if it differs from the last one seen before the
    restart; the same number again is that stale copy.
    """

    def __init__(self, direction: int, coordinator: Any, did: str) -> None:
        self._dir = direction                  # -1 counts down, +1 counts up
        self._coordinator = coordinator
        self._did = did
        self._seed: int | None = None          # device value at _seed_at
        self._seed_at: float = 0.0             # monotonic
        self._last_device: int | None = None
        self._restored = False
        self.was_on = False

    def value(self) -> int | None:
        if self._seed is None:
            return None
        return max(0, self._seed + self._dir * int(time.monotonic() - self._seed_at))

    def dump(self) -> dict[str, Any]:
        if self._seed is None:
            return {}
        return {"seed": self._seed,
                "seed_wall": time.time() - (time.monotonic() - self._seed_at),
                "last_device": self._last_device, "was_on": self.was_on}

    def restore(self, saved: dict[str, Any]) -> None:
        try:
            seed, seed_wall = int(saved["seed"]), float(saved["seed_wall"])
        except (KeyError, TypeError, ValueError):
            return
        self._seed = seed
        self._seed_at = time.monotonic() - max(0.0, time.time() - seed_wall)
        self._last_device = saved.get("last_device")
        self.was_on = bool(saved.get("was_on"))
        self._restored = True

    def resync(self, st: dict[str, Any], device: int) -> None:
        on = bool(st.get("on"))
        local = self.value()
        if self._restored:
            self._restored = False
            if on and self.was_on and device == self._last_device:
                self.was_on = on
                return              # the stale copy from before the restart
        if self._dir < 0:
            hint = self._coordinator.run_hint.get(self._did)
            hint_newer = bool(hint and hint[1] > self._seed_at)
            if hint_newer and on and hint:
                # we just asked for a timed run; the cloud copy may still be
                # the previous run's, so seed from what we requested
                device = max(device, hint[0] - int(time.monotonic() - hint[1]))
            seed = next_seed(device, local, on, self._last_device, hint_newer)
        else:
            # up-counter: a stale repeat is LOWER than the local clock, so a
            # fresh value is one that moved ahead; also re-seed on off -> on
            if not on:
                seed = 0
            elif local is None or not self.was_on or device > local + 3:
                seed = device
            else:
                seed = None
        self._last_device = device
        self.was_on = on
        if seed is not None:
            self._seed, self._seed_at = seed, time.monotonic()


class ClockData(ExtraStoredData):
    """A LocalClock as saved across a restart."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def as_dict(self) -> dict[str, Any]:
        return self.data
