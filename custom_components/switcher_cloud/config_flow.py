"""Config flow for Switcher Cloud (cloud credentials + reauth)."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import SwitcherAuthError, SwitcherClient
from .const import CONF_ACCOUNT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)
STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)


async def validate_input(hass: HomeAssistant, account: str, password: str) -> dict[str, Any]:
    client = SwitcherClient(account, password)
    try:
        await hass.async_add_executor_job(client.connect)
    except SwitcherAuthError as err:
        raise InvalidAuth from err
    except OSError as err:
        raise CannotConnect from err
    except Exception as err:  # noqa: BLE001 - non-credential login errcodes
        raise CannotConnect from err
    finally:
        await hass.async_add_executor_job(client.close)
    return {"uid": client.uid}


class SwitcherCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Switcher Cloud."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            account = user_input[CONF_EMAIL].strip().lower()
            try:
                info = await validate_input(self.hass, account, user_input[CONF_PASSWORD])
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
                    data={CONF_ACCOUNT: account, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """HA-initiated reauthentication after ConfigEntryAuthFailed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        account = entry.data[CONF_ACCOUNT]
        if user_input is not None:
            try:
                info = await validate_input(self.hass, account, user_input[CONF_PASSWORD])
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(f"switcher_{info['uid']}")
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={CONF_EMAIL: account},
        )


class CannotConnect(HomeAssistantError):
    """Failure to connect."""


class InvalidAuth(HomeAssistantError):
    """Authentication failure."""
