"""Diagnostics — lets users with unverified device types share raw data in issues."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import SwitcherCloudCoordinator

TO_REDACT = {"password", "account", "device_pwd", "dmac"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: SwitcherCloudCoordinator = entry.runtime_data
    devices = {}
    for did, st in (coordinator.data or {}).items():
        devices[did] = {
            k: v for k, v in st.items()
            if k not in TO_REDACT and k not in ("did_int",)
        }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "devices": devices,
        "note": (
            "DeviceType = %02X%02X of (main_type, sub_type). Only family "
            "'on_wall' is verified against hardware; for other families please "
            "open an issue with this diagnostics dump and, ideally, a packet "
            "capture of the official app controlling the device."
        ),
    }
