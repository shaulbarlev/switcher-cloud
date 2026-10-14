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


async def _coordinator(hass: HomeAssistant, on: bool):
    from custom_components.switcher_cloud.const import CONF_ACCOUNT, CONF_PASSWORD
    from custom_components.switcher_cloud.coordinator import SwitcherCloudCoordinator

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ACCOUNT: "a@b.c", CONF_PASSWORD: "x"})
    entry.add_to_hass(hass)
    coord = SwitcherCloudCoordinator(hass, entry)
    coord.async_set_updated_data({DID: {"family": "on_wall", "on": on, "did_int": 1,
                                        "device_pwd": "p", "power_raw": 0}})
    return coord


async def test_switch_is_optimistic_until_the_device_answers(hass: HomeAssistant) -> None:
    """The commanded state shows before the cloud confirms; readings do not move."""
    coord = await _coordinator(hass, on=False)
    seen: list[tuple] = []
    coord.async_add_listener(lambda: seen.append(
        (coord.pending.get((DID, 0)), coord.data[DID]["on"])))

    async def fake_set(did, on, gang, run_seconds):
        assert coord.pending[(DID, 0)] is True          # shown while the command is in flight
        assert coord.data[DID]["on"] is False           # ...while the device data is untouched
        coord.async_set_updated_data({DID: {**coord.data[DID], "on": True, "power_raw": 2600}})

    coord._set_switch = fake_set
    await coord.async_set_switch(DID, True)
    assert (DID, 0) not in coord.pending
    assert seen[0] == (True, False)                     # first update: optimistic, data still off
    assert coord.data[DID]["on"] is True


async def test_failed_command_drops_the_optimistic_state(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import HomeAssistantError

    coord = await _coordinator(hass, on=False)

    async def fail(did, on, gang, run_seconds):
        raise HomeAssistantError("cloud said no")

    coord._set_switch = fail
    with pytest.raises(HomeAssistantError):
        await coord.async_set_switch(DID, True)
    assert (DID, 0) not in coord.pending                # back to the device's own state
    assert coord.data[DID]["on"] is False


async def _confirm_run(hass, monkeypatch, readings):
    """Drive _send_and_confirm against a fake cloud that answers `readings` in turn."""
    import custom_components.switcher_cloud.coordinator as c

    monkeypatch.setattr(c, "CONFIRM_INTERVAL", 0.01)
    monkeypatch.setattr(c, "POWER_WAIT", 0.3)
    coord = await _coordinator(hass, on=False)
    polls = iter(readings)
    calls = []

    class Fake:
        def send_control(self, body):
            pass

        def poll(self):
            on, w = next(polls, readings[-1])
            calls.append((on, w))
            return {DID: {**coord.data[DID], "on": on, "power_raw": w}}

    coord.client = Fake()
    await coord._send_and_confirm(DID, b"", lambda s: s.get("on") is True)
    return coord, calls


async def test_confirm_waits_for_the_first_power_reading(hass, monkeypatch) -> None:
    coord, calls = await _confirm_run(
        hass, monkeypatch, [(False, 0), (True, 0), (True, 0), (True, 2600), (True, 2600)])
    assert calls == [(False, 0), (True, 0), (True, 0), (True, 2600)]   # stops at the reading
    assert coord.data[DID]["power_raw"] == 2600


async def test_confirm_gives_up_on_a_real_zero(hass, monkeypatch) -> None:
    """Thermostat open: 0 W is the truth; the wait is bounded."""
    import time
    t = time.monotonic()
    coord, calls = await _confirm_run(hass, monkeypatch, [(True, 0)])
    assert time.monotonic() - t < 2
    assert coord.data[DID]["on"] is True and coord.data[DID]["power_raw"] == 0


async def test_refused_login_raises_a_repair_and_no_reset(hass) -> None:
    import time

    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.switcher_cloud.api import SwitcherApiError

    coord = await _coordinator(hass, on=False)
    resets = []

    class Refused:
        login_rejected = (12, time.time() - 600)

        def poll(self):
            raise SwitcherApiError("timeout waiting for 0x1343")

        def reset(self):
            resets.append(1)

    coord.client = Refused()
    for _ in range(4):                                   # past RESET_AFTER_FAILURES
        with pytest.raises(UpdateFailed, match="errcode 12"):
            await coord._async_update_data()
    assert resets == []
    issue_id = f"login_rejected_{coord.config_entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.translation_placeholders["errcode"] == "12"

    class Back:
        login_rejected = None

        def poll(self):
            return {DID: {**coord.data[DID]}}

    coord.client = Back()
    await coord._async_update_data()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
