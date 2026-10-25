"""The add_task action and the events on the bus (from polling and from the clock)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.opentodo.const import DOMAIN

from .conftest import tick
from .fake_opentodo import FakeOpenTodo


async def add_task(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    """Run opentodo.add_task and return its answer."""
    return await hass.services.async_call(DOMAIN, "add_task", data, blocking=True, return_response=True)


@pytest.mark.usefixtures("setup_integration")
async def test_add_task_returns_the_task(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """The action reads quick-add syntax and answers with the new task."""
    result = await add_task(hass, text="milk tomorrow #Home p1", labels=["errands"], description="2 litres")
    task = result["task"]
    assert task["title"] == "milk"
    assert task["project_name"] == "Home"
    assert task["due"] == "2026-10-26"
    assert task["priority"] == 1
    assert task["labels"] == ["errands"]
    assert task["description"] == "2 litres"
    assert task["url"] == f"http://opentodo.test:8080/?task={task['id']}"
    assert seeded.tasks[task["id"]]["project_id"] == "home"


@pytest.mark.usefixtures("setup_integration")
async def test_add_task_explicit_project_and_due(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """An explicit project and due time win over the text."""
    result = await add_task(
        hass, text="call #Home tomorrow", project="Work", due_datetime="2026-10-27 08:00:00", priority=2
    )
    stored = seeded.tasks[result["task"]["id"]]
    assert stored["project_id"] == "work"
    assert stored["due"] == {"date": "2026-10-27", "time": "08:00"}
    assert stored["priority"] == 2
    result = await add_task(hass, text="keep #Home as text", quick_add=False, due_date="2026-11-02")
    assert seeded.tasks[result["task"]["id"]]["title"] == "keep #Home as text"
    assert seeded.tasks[result["task"]["id"]]["due"] == {"date": "2026-11-02"}


@pytest.mark.usefixtures("setup_integration")
async def test_add_task_errors(hass: HomeAssistant, seeded: FakeOpenTodo, setup_integration: MockConfigEntry) -> None:
    """Unknown projects and accounts are refused before anything is written."""
    with pytest.raises(ServiceValidationError, match="no project called Garden"):
        await add_task(hass, text="x", project="Garden")
    with pytest.raises(ServiceValidationError, match="No OpenTodo account"):
        await add_task(hass, text="x", config_entry_id="missing")
    second = MockConfigEntry(
        domain=DOMAIN, title="Second", unique_id="u2", data=setup_integration.data, options={"push": False}
    )
    second.add_to_hass(hass)
    await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="choose one"):
        await add_task(hass, text="x")
    result = await add_task(hass, text="y", config_entry_id=second.entry_id)
    assert result["task"]["title"] == "y"


@pytest.mark.usefixtures("setup_integration")
async def test_poll_events(hass: HomeAssistant, seeded: FakeOpenTodo, freezer: Any) -> None:
    """Without push, changes between two updates fire the events."""
    created = async_capture_events(hass, "opentodo_task_created")
    completed = async_capture_events(hass, "opentodo_task_completed")
    reopened = async_capture_events(hass, "opentodo_task_reopened")
    updated = async_capture_events(hass, "opentodo_task_updated")
    deleted = async_capture_events(hass, "opentodo_task_deleted")
    assigned = async_capture_events(hass, "opentodo_task_assigned")
    seeded.add_task("New one", id="n", project_id="home")
    seeded.tasks["t-today"]["completed_at"] = 1792918800000
    seeded.tasks["t-done"]["completed_at"] = None
    seeded.tasks["t-late"]["assignee_id"] = "bob"
    seeded.tasks["t-work"]["deleted_at"] = 1
    await tick(hass, freezer, 3)
    assert [e.data["task_id"] for e in created] == ["n"]
    assert created[0].data["project_name"] == "Home"
    assert created[0].data["delivery"] == "poll"
    assert [e.data["task_id"] for e in completed] == ["t-today"]
    assert [e.data["task_id"] for e in reopened] == ["t-done"]
    assert [e.data["task_id"] for e in updated] == ["t-late"]
    assert updated[0].data["changes"] == {"assignee_id": {"from": None, "to": "bob"}}
    assert [e.data["task_id"] for e in assigned] == ["t-late"]
    assert [e.data["task_id"] for e in deleted] == ["t-work"]


@pytest.mark.usefixtures("setup_integration")
async def test_repeating_completion_is_one_completed_event(
    hass: HomeAssistant, seeded: FakeOpenTodo, freezer: Any
) -> None:
    """Completing a repeating task fires completed for its occurrence record."""
    seeded.tasks["t-today"]["recurrence"] = "every day"
    await tick(hass, freezer, 3)
    completed = async_capture_events(hass, "opentodo_task_completed")
    created = async_capture_events(hass, "opentodo_task_created")
    await hass.services.async_call(
        "todo", "update_item", {"entity_id": "todo.home", "item": "t-today", "status": "completed"}, blocking=True
    )
    await tick(hass, freezer, 3)
    assert [e.data["series_id"] for e in completed] == ["t-today"]
    assert created == []


@pytest.mark.usefixtures("setup_integration")
async def test_due_events(hass: HomeAssistant, seeded: FakeOpenTodo, freezer: Any) -> None:
    """A timed task fires opentodo_task_due when its time comes; an all-day task at the start of its day."""
    seeded.add_task("Soon", id="soon", due={"date": "2026-10-25", "time": "10:05"})
    seeded.add_task("Tomorrow", id="tom", due={"date": "2026-10-26"})
    await tick(hass, freezer, 3)
    due: list[Event] = async_capture_events(hass, "opentodo_task_due")
    await tick(hass, freezer, 1)
    await tick(hass, freezer, 1)
    await tick(hass, freezer, 1)
    assert [e.data["task_id"] for e in due] == ["soon"]
    assert due[0].data["all_day"] is False
    await tick(hass, freezer, 14 * 60)
    assert [e.data["task_id"] for e in due] == ["soon", "t-timed", "tom"]
    assert due[2].data["all_day"] is True
