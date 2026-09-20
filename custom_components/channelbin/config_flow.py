"""Config flow for the ChannelBin integration.

Validates by calling GET /api/ha/v1/status with the entered host/port/scheme/API key
before creating the entry, so a bad host or key fails at setup time rather than silently
at the coordinator's first poll.

The error keys raised here ("invalid_auth", "cannot_connect", and the two server-too-old keys
from const.py) are rendered via strings.json/translations/en.json.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .compat import MIN_CHANNELBIN_VERSION, server_too_old
from .const import (
    CONF_SCHEME,
    DEFAULT_PORT,
    DEFAULT_SCHEME,
    DOMAIN,
    ISSUE_SERVER_TOO_OLD,
    ISSUE_SERVER_VERSION_UNKNOWN,
    STATUS_PATH,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SCHEME, default=DEFAULT_SCHEME): vol.In(["http", "https"]),
        vol.Required(CONF_API_KEY): str,
    }
)


class CannotConnect(Exception):
    """Error to indicate the status endpoint could not be reached."""


class InvalidAuth(Exception):
    """Error to indicate the supplied API key was rejected."""


class ServerTooOld(Exception):
    """Error to indicate the server is older than MIN_CHANNELBIN_VERSION."""

    def __init__(self, reported: str | None) -> None:
        super().__init__(reported)
        self.reported = reported


async def _validate_status_endpoint(hass: HomeAssistant, data: dict[str, Any]) -> None:
    session = async_get_clientsession(hass)
    payload: Any = None
    url = f"{data[CONF_SCHEME]}://{data[CONF_HOST]}:{data[CONF_PORT]}{STATUS_PATH}"
    try:
        async with session.get(
            url,
            headers={"X-API-Key": data[CONF_API_KEY]},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 401:
                raise InvalidAuth
            resp.raise_for_status()
            payload = await resp.json()
    except InvalidAuth:
        raise
    except aiohttp.ClientError as err:
        raise CannotConnect from err
    except TimeoutError as err:
        raise CannotConnect from err
    too_old, reported = server_too_old(payload)
    if too_old:
        raise ServerTooOld(reported)


class ChannelBinConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ChannelBin."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"minimum": MIN_CHANNELBIN_VERSION, "host": "", "reported": ""}
        if user_input is not None:
            try:
                await _validate_status_endpoint(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except ServerTooOld as err:
                errors["base"] = (
                    ISSUE_SERVER_TOO_OLD if err.reported else ISSUE_SERVER_VERSION_UNKNOWN
                )
                placeholders["host"] = user_input[CONF_HOST]
                placeholders["reported"] = err.reported or ""
            else:
                self._async_abort_entries_match(
                    {CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT]}
                )
                return self.async_create_entry(title="ChannelBin", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )
