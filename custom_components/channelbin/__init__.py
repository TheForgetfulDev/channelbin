"""The ChannelBin integration.

Sets up the DataUpdateCoordinator that polls GET /api/ha/v1/status and forwards setup to
the sensor/binary_sensor platforms in PLATFORMS.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import ChannelBinDataUpdateCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

type ChannelBinConfigEntry = ConfigEntry[ChannelBinDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ChannelBinConfigEntry) -> bool:
    coordinator = ChannelBinDataUpdateCoordinator(hass, entry.data)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ChannelBinConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
