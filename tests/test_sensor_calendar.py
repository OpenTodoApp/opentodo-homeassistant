"""Counts and the calendar."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
import pytest

from .conftest import tick
from .fake_opentodo import FakeOpenTodo

PREFIX = "sensor.olivia_opentodo_test"
CALENDAR = "calendar.olivia_opentodo_test_tasks"


@pytest.mark.usefixtures("setup_integration")
async def test_counts(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """Today, Earlier, the Inbox and each project count like the app."""
    today = hass.states.get(f"{PREFIX}_today")
    assert today.state == "3"
    assert today.attributes["unit_of_measurement"] == "tasks"
    assert [t["title"] for t in today.attributes["tasks"]] == ["Call plumber", "Pay rent", "Water plants"]
    assert today.attributes["tasks"][1]["project"] == "Home"
    assert hass.states.get(f"{PREFIX}_earlier").state == "1"
    assert hass.states.get(f"{PREFIX}_inbox").state == "1"
    # Someday tasks are not counted, like the sidebar.
    home = hass.states.get(f"{PREFIX}_home")
    assert home.state == "3"
    assert home.name == "Olivia (opentodo.test) Home"
    assert hass.states.get(f"{PREFIX}_work").state == "1"


@pytest.mark.usefixtures("setup_integration")
async def test_earlier_rules(hass: HomeAssistant, seeded: FakeOpenTodo, freezer: Any) -> None:
    """A passed deadline is Earlier unless the task is planned for today; a blocked task is not Earlier."""
    seeded.add_task("Deadline passed", project_id="work", deadline="2026-10-24")
    seeded.add_task("Planned today", project_id="work", deadline="2026-10-24", due={"date": "2026-10-25"})
    seeded.add_task("Blocked late", project_id="work", due={"date": "2026-10-01"}, blocked_by=["t-work"])
    seeded.add_task("Started", project_id="work", start="2026-10-20")
    await tick(hass, freezer, 3)
    assert hass.states.get(f"{PREFIX}_earlier").state == "2"
    assert hass.states.get(f"{PREFIX}_today").state == "7"


@pytest.mark.usefixtures("setup_integration")
async def test_inbox_subtask_follows_parent(hass: HomeAssistant, seeded: FakeOpenTodo, freezer: Any) -> None:
    """A subtask of a scheduled task is not in the Inbox."""
    seeded.add_task("Scheduled parent", id="p", due={"date": "2026-11-01"})
    seeded.add_task("Its subtask", parent_id="p")
    await tick(hass, freezer, 3)
    assert hass.states.get(f"{PREFIX}_inbox").state == "1"


@pytest.mark.usefixtures("setup_integration")
async def test_calendar(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """Tasks with a do-on date are events: all day, or a block of their estimate."""
    state = hass.states.get(CALENDAR)
    assert state.attributes["message"] == "Water plants"
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": CALENDAR, "start_date_time": "2026-10-19 00:00:00", "end_date_time": "2026-10-31 00:00:00"},
        blocking=True,
        return_response=True,
    )
    events = result[CALENDAR]["events"]
    assert [e["summary"] for e in events] == ["Call plumber", "Water plants", "Pay rent", "Report"]
    rent = events[2]
    assert rent["start"] == "2026-10-25T18:30:00+01:00"
    assert rent["end"] == "2026-10-25T18:45:00+01:00"
    assert rent["description"] == "Home"
    assert events[1]["start"] == "2026-10-25" and events[1]["end"] == "2026-10-26"


@pytest.mark.usefixtures("setup_integration")
async def test_calendar_current_event_and_location(hass: HomeAssistant, seeded: FakeOpenTodo, freezer: Any) -> None:
    """The state is on during an open task's event; a location is the event's location."""
    seeded.add_task(
        "Dentist", id="d", due={"date": "2026-10-25", "time": "10:00"}, estimate_minutes=60, location={"name": "Studio"}
    )
    seeded.tasks["t-today"]["completed_at"] = 1
    await tick(hass, freezer, 3)
    state = hass.states.get(CALENDAR)
    assert state.state == "on"
    assert state.attributes["message"] == "Dentist"
    assert state.attributes["location"] == "Studio"
