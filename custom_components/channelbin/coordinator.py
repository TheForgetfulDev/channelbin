"""DataUpdateCoordinator for the ChannelBin integration.

Polls GET /api/ha/v1/status (app/routes/ha.py in the ChannelBin repo) on
SCAN_INTERVAL_SECONDS and hands the decoded JSON straight to entities - entities read
fields off coordinator.data rather than each polling the endpoint separately.

Every poll also checks the server's reported version against MIN_CHANNELBIN_VERSION. A server
that is too old gets a repair issue naming both versions and the update fails, so entities go
unavailable instead of showing numbers from an API this integration does not understand. The
check runs on every poll, not only at setup, so upgrading ChannelBin clears the issue by itself.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .compat import MIN_CHANNELBIN_VERSION, server_too_old
from .const import (
    CONF_SCHEME,
    DOMAIN,
    ISSUE_SERVER_TOO_OLD,
    ISSUE_SERVER_VERSION_UNKNOWN,
    SCAN_INTERVAL_SECONDS,
    STATUS_PATH,
)

_LOGGER = logging.getLogger(__name__)


class ChannelBinDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls ChannelBin's combined status payload."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self._url = (
            f"{config[CONF_SCHEME]}://{config[CONF_HOST]}:{config[CONF_PORT]}{STATUS_PATH}"
        )
        self._headers = {"X-API-Key": config[CONF_API_KEY]}
        self._host = config[CONF_HOST]
        self._issue_id = f"server_too_old_{config[CONF_HOST]}_{config[CONF_PORT]}"

    async def _async_update_data(self) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                self._url, headers=self._headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with ChannelBin: {err}") from err
        except TimeoutError as err:
            raise UpdateFailed(f"Timed out communicating with ChannelBin: {err}") from err

        too_old, reported = server_too_old(data)
        if too_old:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=(
                    ISSUE_SERVER_TOO_OLD if reported else ISSUE_SERVER_VERSION_UNKNOWN
                ),
                translation_placeholders={
                    "host": self._host,
                    "reported": reported or "",
                    "minimum": MIN_CHANNELBIN_VERSION,
                },
            )
            raise UpdateFailed(
                f"ChannelBin at {self._host} is version {reported or 'unknown'}; "
                f"this integration needs {MIN_CHANNELBIN_VERSION} or newer"
            )
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        return data
