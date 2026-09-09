"""DataUpdateCoordinator for the ChannelBin integration.

Polls GET /api/ha/v1/status (app/routes/ha.py in the ChannelBin repo) on
SCAN_INTERVAL_SECONDS and hands the decoded JSON straight to entities - entities read
fields off coordinator.data rather than each polling the endpoint separately.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_SCHEME, DOMAIN, SCAN_INTERVAL_SECONDS, STATUS_PATH

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

    async def _async_update_data(self) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                self._url, headers=self._headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with ChannelBin: {err}") from err
        except TimeoutError as err:
            raise UpdateFailed(f"Timed out communicating with ChannelBin: {err}") from err
