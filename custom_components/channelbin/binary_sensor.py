"""Binary sensor platform for the ChannelBin integration.

Same CoordinatorEntity pattern as sensor.py - reads off coordinator.data, no sensor
polls independently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ChannelBinConfigEntry
from .const import DOMAIN
from .coordinator import ChannelBinDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class ChannelBinBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a ChannelBin binary_sensor - is_on_fn pulls from coordinator.data."""

    is_on_fn: Callable[[dict[str, Any]], bool]


BINARY_SENSOR_DESCRIPTIONS: tuple[ChannelBinBinarySensorEntityDescription, ...] = (
    ChannelBinBinarySensorEntityDescription(
        key='recording',
        translation_key='recording',
        icon='mdi:record-circle',
        is_on_fn=lambda data: data['recording']['capturing_count'] > 0,
    ),
    ChannelBinBinarySensorEntityDescription(
        key='has_alerts',
        translation_key='has_alerts',
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: data['alerts']['unread_count'] > 0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ChannelBinConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        ChannelBinBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class ChannelBinBinarySensor(
    CoordinatorEntity[ChannelBinDataUpdateCoordinator], BinarySensorEntity
):
    """A single ChannelBin on/off condition, read off the shared coordinator."""

    entity_description: ChannelBinBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ChannelBinDataUpdateCoordinator,
        entry: ChannelBinConfigEntry,
        description: ChannelBinBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f'{entry.entry_id}_{description.key}'
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name='ChannelBin',
            manufacturer='ChannelBin',
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self.coordinator.data)
