"""The OpenTodo integration: lists, counts, a calendar and events from a self-hosted OpenTodo."""

from __future__ import annotations

from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import OpenTodoClient
from .const import CONF_VERIFY_SSL, DOMAIN
from .coordinator import OpenTodoConfigEntry, OpenTodoCoordinator
from .push import PushManager, async_remove_remote
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.CALENDAR, Platform.SENSOR, Platform.TODO]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""
    async_setup_services(hass)
    return True


def _client(hass: HomeAssistant, entry: OpenTodoConfigEntry) -> OpenTodoClient:
    verify = entry.data.get(CONF_VERIFY_SSL, True)
    return OpenTodoClient(
        async_get_clientsession(hass, verify_ssl=verify),
        entry.data[CONF_URL],
        entry.data[CONF_TOKEN],
        verify_ssl=verify,
    )


async def async_setup_entry(hass: HomeAssistant, entry: OpenTodoConfigEntry) -> bool:
    """Fetch the account, then set up the platforms and instant updates."""
    coordinator = OpenTodoCoordinator(hass, entry, _client(hass, entry))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_due_tracking()
    await PushManager(hass, entry, coordinator).async_setup()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenTodoConfigEntry) -> bool:
    """Unload the platforms (the OpenTodo webhook stays for the next start)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: OpenTodoConfigEntry) -> None:
    """Delete the OpenTodo webhook this entry created."""
    await async_remove_remote(hass, entry, _client(hass, entry))
