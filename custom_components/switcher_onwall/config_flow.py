"""Config flow for Switcher On Wall (cloud credentials)."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .api import SwitcherAuthError, SwitcherClient
from .const import CONF_ACCOUNT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    client = SwitcherClient(data[CONF_EMAIL], data[CONF_PASSWORD])
    try:
        await hass.async_add_executor_job(client.connect)
    except SwitcherAuthError as err:
        raise InvalidAuth from err
    except OSError as err:
        raise CannotConnect from err
    finally:
        await hass.async_add_executor_job(client.close)
    return {"uid": client.uid}


class SwitcherOnwallConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Switcher On Wall."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(f"switcher_{info['uid']}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Switcher On Wall",
                    data={
                        CONF_ACCOUNT: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )


class CannotConnect(HomeAssistantError):
    """Failure to connect."""


class InvalidAuth(HomeAssistantError):
    """Authentication failure."""
