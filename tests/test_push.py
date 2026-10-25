"""Instant updates through an OpenTodo webhook."""

from __future__ import annotations

from datetime import timedelta
import json
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.opentodo.const import (
    CONF_HA_URL,
    CONF_PUSH,
    DATA_REMOTE_WEBHOOK_ID,
    DATA_REMOTE_WEBHOOK_SECRET,
    DATA_WEBHOOK_ID,
    DOMAIN,
)
from custom_components.opentodo.push import signature

from .conftest import tick
from .fake_opentodo import FakeOpenTodo


@pytest.fixture
def options() -> dict[str, Any]:
    """Push on."""
    return {CONF_PUSH: True}


@pytest.fixture(autouse=True)
async def internal_url(hass: HomeAssistant) -> None:
    """Home Assistant on the LAN."""
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.1.10:8123"})


@pytest.fixture
def allow_private(fake: FakeOpenTodo) -> FakeOpenTodo:
    """The owner allowed private network targets."""
    fake.allow_private = True
    return fake


def body(
    event: str, task: dict[str, Any], changes: dict[str, Any] | None = None, delivery: str = "d-1"
) -> dict[str, Any]:
    """A webhook payload (docs/webhooks.md)."""
    return {
        "version": 1,
        "id": delivery,
        "event": event,
        "time": int(time.time() * 1000),
        "webhook_id": "w",
        "source": "device",
        "device": "phone",
        "data": {"task": task},
        "changes": changes or {},
    }


async def deliver(
    hass_client_no_auth: Any,
    entry: MockConfigEntry,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    ts: int | None = None,
) -> int:
    """Post a signed delivery to the entry's Home Assistant webhook."""
    client = await hass_client_no_auth()
    raw = json.dumps(payload).encode()
    stamp = str(ts if ts is not None else int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-OpenTodo-Timestamp": stamp,
        "X-OpenTodo-Signature": signature(secret or entry.data[DATA_REMOTE_WEBHOOK_SECRET], stamp, raw),
    }
    resp = await client.post(f"/api/webhook/{entry.data[DATA_WEBHOOK_ID]}", data=raw, headers=headers)
    return resp.status


def test_signature_matches_opentodo_vector() -> None:
    """The signature is OpenTodo's reference vector (docs/webhooks.md, "Test vector")."""
    raw = b'{"version":1,"id":"7d3c1f0e-0000-4000-8000-000000000001","event":"ping","time":1760060001000,"webhook_id":"0b9a2e4c-0000-4000-8000-000000000002","source":"server","data":{},"changes":{}}'
    assert (
        signature("whsec_opentodo-test-vector-secret-do-not-use", "1760060001", raw)
        == "v1=a049099332920d6f2b44a294b46fc5260f87dfc36294f77069b5ab0058589b98"
    )


@pytest.mark.usefixtures("allow_private")
async def test_webhook_created_and_events_fired(
    hass: HomeAssistant, setup_integration: MockConfigEntry, seeded: FakeOpenTodo, hass_client_no_auth: Any
) -> None:
    """The integration creates its webhook; a signed delivery updates the lists at once and fires events."""
    entry = setup_integration
    hook = seeded.webhooks[entry.data[DATA_REMOTE_WEBHOOK_ID]]
    assert hook["url"] == f"http://192.168.1.10:8123/api/webhook/{entry.data[DATA_WEBHOOK_ID]}"
    assert "task.completed" in hook["events"]
    coordinator = entry.runtime_data
    assert coordinator.push_active
    assert coordinator.update_interval == timedelta(minutes=15)

    completed = async_capture_events(hass, "opentodo_task_completed")
    assigned = async_capture_events(hass, "opentodo_task_assigned")
    task = dict(seeded.tasks["t-today"], completed_at=1792918800000)
    assert await deliver(hass_client_no_auth, entry, body("task.completed", task)) == 200
    await hass.async_block_till_done()
    assert [e.data["task_id"] for e in completed] == ["t-today"]
    assert completed[0].data["delivery"] == "push"
    assert completed[0].data["source"] == "device"
    assert hass.states.get("sensor.olivia_opentodo_test_today").state == "2"
    # The same delivery again changes nothing.
    assert await deliver(hass_client_no_auth, entry, body("task.completed", task)) == 200
    await hass.async_block_till_done()
    assert len(completed) == 1

    late = dict(seeded.tasks["t-late"], assignee_id="bob")
    changes = {"assignee_id": {"from": None, "to": "bob"}}
    assert await deliver(hass_client_no_auth, entry, body("task.updated", late, changes, delivery="d-2")) == 200
    await hass.async_block_till_done()
    assert [e.data["changes"] for e in assigned] == [changes]

    deleted = async_capture_events(hass, "opentodo_task_deleted")
    gone = dict(seeded.tasks["t-work"], deleted_at=1)
    assert await deliver(hass_client_no_auth, entry, body("task.deleted", gone, delivery="d-3")) == 200
    await hass.async_block_till_done()
    assert len(deleted) == 1
    assert hass.states.get("sensor.olivia_opentodo_test_work").state == "0"


@pytest.mark.usefixtures("allow_private")
async def test_bad_deliveries_refused(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_client_no_auth: Any, seeded: FakeOpenTodo
) -> None:
    """A wrong signature or an old timestamp is refused; nothing is fired."""
    created = async_capture_events(hass, "opentodo_task_created")
    payload = body("task.created", dict(seeded.tasks["t-inbox"]))
    assert await deliver(hass_client_no_auth, setup_integration, payload, secret="whsec_wrong") == 401
    assert await deliver(hass_client_no_auth, setup_integration, payload, ts=int(time.time()) - 3600) == 401
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{setup_integration.data[DATA_WEBHOOK_ID]}", data=b"{}")
    assert resp.status == 401
    await hass.async_block_till_done()
    assert created == []


@pytest.mark.usefixtures("allow_private")
async def test_project_event_refreshes(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_client_no_auth: Any, seeded: FakeOpenTodo
) -> None:
    """A project change makes the integration fetch again (a new list appears)."""
    seeded.add_project("Garden", id="garden")
    payload = {
        "version": 1,
        "id": "p-1",
        "event": "project.created",
        "time": 1,
        "data": {"project": seeded.projects["garden"]},
        "changes": {},
    }
    assert await deliver(hass_client_no_auth, setup_integration, payload) == 200
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("todo.garden") is not None


async def test_private_network_refused_then_allowed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, seeded: FakeOpenTodo, freezer: Any
) -> None:
    """With private targets off: polling and a repair issue; once allowed, push within the hour."""
    entry = setup_integration
    issue_id = f"push_private_network_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert not entry.runtime_data.push_active
    assert entry.runtime_data.update_interval == timedelta(minutes=2)
    seeded.allow_private = True
    await tick(hass, freezer, 61)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert entry.runtime_data.push_active
    assert len(seeded.webhooks) == 1


@pytest.mark.usefixtures("allow_private")
async def test_disabled_webhook_reenabled_and_url_followed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, seeded: FakeOpenTodo, freezer: Any
) -> None:
    """OpenTodo disabled the webhook after failures, or Home Assistant moved: the next check repairs it."""
    hook = seeded.webhooks[setup_integration.data[DATA_REMOTE_WEBHOOK_ID]]
    hook["enabled"] = False
    hook["disabledReason"] = "too_many_failures"
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.1.11:8123"})
    await tick(hass, freezer, 61)
    assert hook["enabled"] is True
    assert hook["url"].startswith("http://192.168.1.11:8123/api/webhook/")
    assert len(seeded.webhooks) == 1


@pytest.mark.parametrize("options", [{CONF_PUSH: True, CONF_HA_URL: "http://ha.lan:8123/"}])
@pytest.mark.usefixtures("allow_private")
async def test_address_option_and_removal(
    hass: HomeAssistant, setup_integration: MockConfigEntry, seeded: FakeOpenTodo
) -> None:
    """The address option wins; removing the entry deletes its OpenTodo webhook."""
    hook = next(iter(seeded.webhooks.values()))
    assert hook["url"] == f"http://ha.lan:8123/api/webhook/{setup_integration.data[DATA_WEBHOOK_ID]}"
    await hass.config_entries.async_remove(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert seeded.webhooks == {}


async def test_other_failure_is_a_repair_issue(
    hass: HomeAssistant, fake: FakeOpenTodo, config_entry: MockConfigEntry, freezer: Any, seeded: FakeOpenTodo
) -> None:
    """Another refusal (the webhook limit) raises the general issue."""
    fake.allow_private = True
    fake.fail_next_write = (409, "limit_reached", "at most 20 webhooks per account")
    freezer.move_to("2026-10-25 10:00:00+01:00")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"push_failed_{config_entry.entry_id}")
    assert not config_entry.runtime_data.push_active
