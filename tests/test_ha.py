"""Sensor behaviour that needs Home Assistant itself: the power/current registry migration and the power value.

Needs `pytest-homeassistant-custom-component`; the plain protocol suite does not,
so this file skips itself when the harness is absent.
"""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switcher_cloud.const import DOMAIN
from custom_components.switcher_cloud.sensor import _migrate_power_sensors

DID = "0123456789ab"


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    pass


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="test")
    entry.add_to_hass(hass)
    return entry


def _add(hass, entry, unique: str, suggested: str, device_class) -> er.RegistryEntry:
    return er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, unique, config_entry=entry,
        suggested_object_id=suggested, original_device_class=device_class)


async def test_old_install_gets_power_back(hass: HomeAssistant) -> None:
    """Born before 1.0.1: `sensor.boiler_power` had become the amps sensor."""
    entry = _entry(hass)
    amps = _add(hass, entry, f"switcher_cloud_{DID}_power", "boiler_power", SensorDeviceClass.CURRENT)
    watts = _add(hass, entry, f"switcher_cloud_{DID}_power_w", "boiler_power_2", SensorDeviceClass.POWER)
    assert amps.entity_id == "sensor.boiler_power" and watts.entity_id == "sensor.boiler_power_2"

    _migrate_power_sensors(hass, entry)

    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"switcher_cloud_{DID}_power") == "sensor.boiler_power"
    assert reg.async_get_entity_id("sensor", DOMAIN, f"switcher_cloud_{DID}_current") == "sensor.boiler_current"
    assert reg.async_get_entity_id("sensor", DOMAIN, f"switcher_cloud_{DID}_power_w") is None
    assert reg.async_get("sensor.boiler_power_2") is None


async def test_fresh_install_keeps_entity_ids(hass: HomeAssistant) -> None:
    """Installed on 1.0.1/1.0.2: ids were right, only the unique ids were odd."""
    entry = _entry(hass)
    _add(hass, entry, f"switcher_cloud_{DID}_power", "boiler_current", SensorDeviceClass.CURRENT)
    _add(hass, entry, f"switcher_cloud_{DID}_power_w", "boiler_power", SensorDeviceClass.POWER)

    _migrate_power_sensors(hass, entry)

    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"switcher_cloud_{DID}_current") == "sensor.boiler_current"
    assert reg.async_get_entity_id("sensor", DOMAIN, f"switcher_cloud_{DID}_power") == "sensor.boiler_power"


async def test_already_migrated_is_a_no_op(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    _add(hass, entry, f"switcher_cloud_{DID}_current", "boiler_current", SensorDeviceClass.CURRENT)
    _add(hass, entry, f"switcher_cloud_{DID}_power", "boiler_power", SensorDeviceClass.POWER)

    _migrate_power_sensors(hass, entry)

    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"switcher_cloud_{DID}_current") == "sensor.boiler_current"
    assert reg.async_get_entity_id("sensor", DOMAIN, f"switcher_cloud_{DID}_power") == "sensor.boiler_power"


async def test_renamed_by_user_is_left_alone(hass: HomeAssistant) -> None:
    """A user who renamed the amps sensor keeps that id; only the unique ids move."""
    entry = _entry(hass)
    _add(hass, entry, f"switcher_cloud_{DID}_power", "dud_amps", SensorDeviceClass.CURRENT)
    _add(hass, entry, f"switcher_cloud_{DID}_power_w", "dud_watts", SensorDeviceClass.POWER)

    _migrate_power_sensors(hass, entry)

    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"switcher_cloud_{DID}_current") == "sensor.dud_amps"
    assert reg.async_get_entity_id("sensor", DOMAIN, f"switcher_cloud_{DID}_power") == "sensor.dud_watts"


def test_power_is_zero_while_off() -> None:
    """The off snapshot keeps the last running figure; the sensor must not."""
    from custom_components.switcher_cloud.sensor import watts

    assert watts({"on": False, "power_raw": 2693}) == 0
    assert watts({"on": True, "power_raw": 2693}) == 2693
    assert watts({"on": True, "power_raw": 0}) == 0          # thermostat open: real
    assert watts({"on": None, "power_raw": 2693}) == 2693    # unknown state: pass it on
    assert watts({"on": False}) is None                      # family without the field
