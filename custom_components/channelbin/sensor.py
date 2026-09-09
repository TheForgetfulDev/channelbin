"""Sensor platform for the ChannelBin integration.

All sensors are CoordinatorEntity subclasses reading fields off
ChannelBinDataUpdateCoordinator.data (the decoded GET /api/ha/v1/status payload -
app/routes/ha.py in the ChannelBin repo). No sensor polls on its own; the coordinator
(coordinator.py) is the only thing that talks to the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import ChannelBinConfigEntry
from .const import DOMAIN
from .coordinator import ChannelBinDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class ChannelBinSensorEntityDescription(SensorEntityDescription):
    """Describes a ChannelBin sensor - value_fn/attrs_fn pull from coordinator.data."""

    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _next_recording_value(data: dict[str, Any]) -> datetime | None:
    """start_time on the wire is naive UTC (app/database.py: 'naive UTC' storage
    convention) - a TIMESTAMP-class sensor requires a timezone-aware value, so a bare
    parse_datetime() would leave it naive and HA would reject/warn on it."""
    nxt = data['recording'].get('next_recording')
    if not nxt:
        return None
    parsed = dt_util.parse_datetime(nxt['start_time'])
    if parsed is None:
        return None
    # Attach UTC directly rather than dt_util.as_utc(), which would treat a naive
    # datetime as HA's local timezone (hass.config.time_zone) and convert it - wrong,
    # since the value is already UTC on the wire.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt_util.UTC)


def _next_recording_attrs(data: dict[str, Any]) -> dict[str, Any]:
    nxt = data['recording'].get('next_recording')
    if not nxt:
        return {}
    return {'name': nxt['name'], 'channel': nxt['channel'], 'recording_id': nxt['id']}


def _unread_alerts_attrs(data: dict[str, Any]) -> dict[str, Any]:
    latest = data['alerts'].get('latest')
    if not latest:
        return {}
    return {
        'severity': latest['severity'],
        'title': latest['title'],
        'created_at': latest['created_at'],
    }


def _accounts_ok_attrs(data: dict[str, Any]) -> dict[str, Any]:
    accounts = data['accounts']
    return {'error_count': accounts['error_count'], 'total': accounts['total']}


SENSOR_DESCRIPTIONS: tuple[ChannelBinSensorEntityDescription, ...] = (
    ChannelBinSensorEntityDescription(
        key='capturing',
        translation_key='capturing',
        icon='mdi:record-rec',
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['recording']['capturing_count'],
    ),
    ChannelBinSensorEntityDescription(
        key='converting',
        translation_key='converting',
        icon='mdi:cog-sync',
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['recording']['converting_count'],
    ),
    ChannelBinSensorEntityDescription(
        key='next_recording',
        translation_key='next_recording',
        icon='mdi:calendar-clock',
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_recording_value,
        attrs_fn=_next_recording_attrs,
    ),
    ChannelBinSensorEntityDescription(
        key='disk_free',
        translation_key='disk_free',
        icon='mdi:harddisk',
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['disk']['free_bytes'],
    ),
    ChannelBinSensorEntityDescription(
        key='disk_used_percent',
        translation_key='disk_used_percent',
        icon='mdi:harddisk',
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['disk']['used_pct'],
    ),
    ChannelBinSensorEntityDescription(
        key='unread_alerts',
        translation_key='unread_alerts',
        icon='mdi:alert-circle',
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['alerts']['unread_count'],
        attrs_fn=_unread_alerts_attrs,
    ),
    ChannelBinSensorEntityDescription(
        key='accounts_ok',
        translation_key='accounts_ok',
        icon='mdi:account-check',
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['accounts']['ok_count'],
        attrs_fn=_accounts_ok_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ChannelBinConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        ChannelBinSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class ChannelBinSensor(CoordinatorEntity[ChannelBinDataUpdateCoordinator], SensorEntity):
    """A single ChannelBin status field, read off the shared coordinator."""

    entity_description: ChannelBinSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ChannelBinDataUpdateCoordinator,
        entry: ChannelBinConfigEntry,
        description: ChannelBinSensorEntityDescription,
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
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
