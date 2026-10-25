"""Shared entity base: one device per OpenTodo account."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import OpenTodoCoordinator


class OpenTodoEntity(CoordinatorEntity[OpenTodoCoordinator]):
    """An entity of one OpenTodo account."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: OpenTodoCoordinator, key: str) -> None:
        """Set the unique id and the account's device."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        account = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{account}_{key}"
        data = coordinator.data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model="Account",
            sw_version=data.version.lstrip("v") if data and data.version else None,
            configuration_url=coordinator.client.url,
            entry_type=DeviceEntryType.SERVICE,
        )


def remove_stale_entities(hass: HomeAssistant, entry: ConfigEntry, domain: str, entities: Iterable[Entity]) -> None:
    """Forget this platform's entities that are no longer set up (a project left the options, or was deleted)."""
    wanted = {entity.unique_id for entity in entities}
    registry = er.async_get(hass)
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.domain == domain and reg.unique_id not in wanted:
            registry.async_remove(reg.entity_id)
