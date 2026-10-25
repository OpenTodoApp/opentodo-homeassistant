"""Setting up, unloading, refusing and removing."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opentodo.const import DOMAIN

from .conftest import FROZEN, tick
from .fake_opentodo import USER_ID, FakeOpenTodo


async def test_setup_and_unload(hass: HomeAssistant, seeded: FakeOpenTodo, setup_integration: MockConfigEntry) -> None:
    """The entry loads, makes one device for the account, and unloads."""
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, USER_ID), setup_integration.entry_id)
    assert device is not None
    assert device.manufacturer == "OpenTodo"
    assert device.sw_version == "0.12.0"
    assert device.configuration_url == "http://opentodo.test:8080"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_offline_retries(hass: HomeAssistant, fake: FakeOpenTodo, config_entry: MockConfigEntry) -> None:
    """An unreachable server is retried later."""
    fake.offline = True
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_revoked_token_starts_reauth(
    hass: HomeAssistant, fake: FakeOpenTodo, config_entry: MockConfigEntry
) -> None:
    """A refused token asks for a new one."""
    fake.tokens.clear()
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_encrypted_account_refused(
    hass: HomeAssistant, fake: FakeOpenTodo, config_entry: MockConfigEntry
) -> None:
    """An end-to-end encrypted account does not load and raises a repair issue."""
    fake.encrypted = True
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"encrypted_account_{config_entry.entry_id}")


async def test_encryption_turned_on_later(
    hass: HomeAssistant, seeded: FakeOpenTodo, setup_integration: MockConfigEntry, freezer: Any
) -> None:
    """Encryption turned on while running: updates stop with a repair issue, and resume when it is off."""
    issue_id = f"encrypted_account_{setup_integration.entry_id}"
    seeded.encrypted = True
    await tick(hass, freezer, 3)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert hass.states.get("sensor.olivia_opentodo_test_inbox").state == "unavailable"
    seeded.encrypted = False
    await tick(hass, freezer, 3)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert hass.states.get("sensor.olivia_opentodo_test_inbox").state == "1"


async def test_frozen_constant() -> None:
    """The frozen time is in Rome's winter offset."""
    assert FROZEN.endswith("+01:00")
