"""Diagnostics and Assist."""

from __future__ import annotations

import json

from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opentodo.diagnostics import async_get_config_entry_diagnostics

from .fake_opentodo import TOKEN, FakeOpenTodo


async def test_diagnostics_redact(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """No token, webhook secret, email or task content in the diagnostics."""
    result = await async_get_config_entry_diagnostics(hass, setup_integration)
    text = json.dumps(result)
    assert TOKEN not in text
    assert "olivia@example.com" not in text
    assert "Water plants" not in text
    assert result["counts"]["tasks"] == 8
    assert result["server"]["version"] == "v0.12.0"
    assert result["server"]["scope"] == "full"
    assert result["updates"]["push_active"] is False


@pytest.mark.usefixtures("setup_integration")
async def test_assist_adds_and_completes(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """'Add milk to the Home list' and 'check off milk from the Home list' work through Home Assistant's own intents."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "intent", {})
    result = await conversation.async_converse(hass, "add milk to the Home list", None, Context(), language="en")
    assert result.response.response_type.value == "action_done", result.response.speech
    task = next(t for t in seeded.tasks.values() if t["title"] == "Milk")
    assert task["project_id"] == "home"
    result = await conversation.async_converse(
        hass, "check off milk from the Home list", None, Context(), language="en"
    )
    assert result.response.response_type.value == "action_done", result.response.speech
    assert task["completed_at"]
