"""Diagnostics — lets users with unverified device types share raw data in issues."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import SwitcherCloudConfigEntry

TO_REDACT = {"password", "account", "device_pwd", "dmac", "name", "did", "did_int"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SwitcherCloudConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
        },
        "devices": {
            f"device_{i}": async_redact_data(st, TO_REDACT)
            for i, st in enumerate((coordinator.data or {}).values())
        },
        "note": (
            "DeviceType = %02X%02X of (main_type, sub_type). Only family "
            "'on_wall' is verified against hardware; for other families please "
            "open an issue with this dump and, ideally, a packet capture of the "
            "official app controlling the device."
        ),
    }
