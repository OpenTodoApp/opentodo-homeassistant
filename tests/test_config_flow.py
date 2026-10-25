"""The config flow, reauthentication, reconfiguration and options."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opentodo.const import (
    CONF_ALL_PROJECTS,
    CONF_COMPLETED_DAYS,
    CONF_HA_URL,
    CONF_PROJECTS,
    CONF_PUSH,
    CONF_QUICK_ADD,
    CONF_SCAN_INTERVAL,
    CONF_TODAY_LIST,
    CONF_VERIFY_SSL,
    DOMAIN,
)

from .fake_opentodo import ADMIN_TOKEN, AGENT_TOKEN, BASE, OTHER_ACCOUNT_TOKEN, READ_TOKEN, TOKEN, USER_ID, FakeOpenTodo


async def start(hass: HomeAssistant, **data: Any) -> Any:
    """Open the flow and submit the form once."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE + "/api/v1/", CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: True, **data}
    )


async def test_user_flow(hass: HomeAssistant, fake: FakeOpenTodo) -> None:
    """A good address and token create the entry, named after the account and host."""
    result = await start(hass, **{CONF_TOKEN: f"  {TOKEN} "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Olivia (opentodo.test)"
    assert result["data"] == {CONF_URL: BASE, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: True}
    assert result["result"].unique_id == USER_ID


async def test_read_token_accepted(hass: HomeAssistant, fake: FakeOpenTodo) -> None:
    """A read token is accepted (read-only lists)."""
    result = await start(hass, **{CONF_TOKEN: READ_TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("setup", "token", "error"),
    [
        ("offline", TOKEN, "cannot_connect"),
        ("not_opentodo", TOKEN, "not_opentodo"),
        (None, "ot_wrong", "invalid_auth"),
        (None, AGENT_TOKEN, "agent_token"),
        (None, ADMIN_TOKEN, "admin_token"),
        ("encrypted", TOKEN, "encrypted_account"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, fake: FakeOpenTodo, setup: str | None, token: str, error: str
) -> None:
    """Each problem has its own message; fixing it lets the flow finish."""
    if setup:
        setattr(fake, setup, True)
    result = await start(hass, **{CONF_TOKEN: token})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    if setup:
        setattr(fake, setup, False)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_unknown_error(hass: HomeAssistant, fake: FakeOpenTodo) -> None:
    """An unexpected refusal of /me is an unknown error."""
    fake.fail_next_write = None
    fake.tokens[TOKEN] = ("full", "")
    result = await start(hass)
    assert result["errors"] == {"base": "unknown"}


async def test_already_configured(hass: HomeAssistant, fake: FakeOpenTodo, config_entry: MockConfigEntry) -> None:
    """The same account cannot be added twice."""
    config_entry.add_to_hass(hass)
    result = await start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, setup_integration: MockConfigEntry, fake: FakeOpenTodo) -> None:
    """A new token for the same account replaces the old one; another account's is refused."""
    fake.tokens["ot_new"] = ("full", USER_ID)
    result = await setup_integration.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "ot_bad"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: OTHER_ACCOUNT_TOKEN})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    result = await setup_integration.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "ot_new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert setup_integration.data[CONF_TOKEN] == "ot_new"


async def test_reconfigure(hass: HomeAssistant, setup_integration: MockConfigEntry, fake: FakeOpenTodo) -> None:
    """The address and token can change for the same account."""
    result = await setup_integration.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "opentodo.test:8080", CONF_TOKEN: "ot_bad", CONF_VERIFY_SSL: False}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "opentodo.test:8080", CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert setup_integration.data[CONF_URL] == BASE
    assert setup_integration.data[CONF_VERIFY_SSL] is False


async def test_options(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """The options form lists the projects and saves the choices; the entry reloads."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    projects = next(v for k, v in schema.items() if k == CONF_PROJECTS)
    assert [o["label"] for o in projects.config["options"]] == ["Home", "Work"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ALL_PROJECTS: False,
            CONF_PROJECTS: ["work"],
            CONF_TODAY_LIST: False,
            CONF_COMPLETED_DAYS: 0,
            CONF_QUICK_ADD: True,
            CONF_SCAN_INTERVAL: 5,
            CONF_PUSH: False,
            CONF_HA_URL: "",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert CONF_HA_URL not in setup_integration.options
    assert hass.states.get("todo.home") is None
    assert hass.states.get("todo.work") is not None
    assert setup_integration.runtime_data.update_interval.total_seconds() == 300
