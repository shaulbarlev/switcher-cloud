"""Config flow for Switcher Cloud (cloud credentials)."""
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


class SwitcherCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Switcher Cloud."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: config_entries.ConfigEntry | None = None

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
                    title="Switcher Cloud",
                    data={
                        CONF_ACCOUNT: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data):
        """HA-initiated reauthentication after ConfigEntryAuthFailed."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        assert self._reauth_entry is not None
        if user_input is not None:
            data = {
                **self._reauth_entry.data,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            try:
                await validate_input(self.hass, data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry, data=data)
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={
                CONF_EMAIL: self._reauth_entry.data.get(CONF_ACCOUNT, "")
            },
        )


class CannotConnect(HomeAssistantError):
    """Failure to connect."""


class InvalidAuth(HomeAssistantError):
    """Authentication failure."""
