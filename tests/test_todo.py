"""The to-do lists: what they hold, and changes made from Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.todo import DOMAIN as TODO_DOMAIN, TodoListEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opentodo.const import (
    CONF_ALL_PROJECTS,
    CONF_PROJECTS,
    CONF_PUSH,
    CONF_QUICK_ADD,
    CONF_TODAY_LIST,
)

from .conftest import tick
from .fake_opentodo import READ_TOKEN, FakeOpenTodo

INBOX = "todo.inbox"
TODAY = "todo.today"
HOME = "todo.home"
WORK = "todo.work"


async def items(hass: HomeAssistant, entity_id: str) -> list[dict[str, Any]]:
    """The items of a list, through the todo.get_items action."""
    result = await hass.services.async_call(
        TODO_DOMAIN, "get_items", {ATTR_ENTITY_ID: entity_id}, blocking=True, return_response=True
    )
    return result[entity_id]["items"]


async def call(hass: HomeAssistant, service: str, entity_id: str, **data: Any) -> None:
    """Run a todo action."""
    await hass.services.async_call(TODO_DOMAIN, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True)


@pytest.mark.usefixtures("setup_integration")
async def test_lists_and_items(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """The Inbox, Today and each active project get a list with the app's membership."""
    assert hass.states.get(INBOX).state == "1"
    assert hass.states.get(INBOX).name == "Inbox"
    assert hass.states.get(HOME).name == "Home"
    assert hass.states.get("todo.old") is None  # archived

    assert [i["summary"] for i in await items(hass, INBOX)] == ["Inbox thing"]
    today = await items(hass, TODAY)
    # Earlier first, then today's timed task before the untimed one; archived projects stay out.
    assert [i["summary"] for i in today] == ["Call plumber", "Pay rent", "Water plants"]
    assert today[1]["due"] == "2026-10-25T18:30:00+01:00"
    assert today[2]["due"] == "2026-10-25"

    home = await items(hass, HOME)
    assert [i["summary"] for i in home] == ["Water plants", "Call plumber", "Pay rent", "Later", "Done this morning"]
    assert home[-1]["status"] == "completed"
    assert hass.states.get(HOME).state == "4"


@pytest.mark.usefixtures("setup_integration")
async def test_subtasks_follow_parent_and_sections(hass: HomeAssistant, seeded: FakeOpenTodo, freezer: Any) -> None:
    """Tasks without a section come first, then sections in order; subtasks follow their parent."""
    seeded.sections["s2"] = {"id": "s2", "name": "B", "project_id": "work", "position": 2.0}
    seeded.sections["s1"] = {"id": "s1", "name": "A", "project_id": "work", "position": 1.0}
    seeded.add_task("In B", id="b", project_id="work", section_id="s2", position=1.0)
    seeded.add_task("In A", id="a", project_id="work", section_id="s1", position=5.0)
    seeded.add_task("Child of A", id="a1", project_id="work", section_id="s1", parent_id="a", position=1.0)
    await tick(hass, freezer, 3)
    assert [i["summary"] for i in await items(hass, WORK)] == ["Report", "In A", "Child of A", "In B"]


@pytest.mark.usefixtures("setup_integration")
async def test_add_item_reads_quick_add(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """'milk tomorrow #Home p1 @errands' added to the Inbox lands in Home, due tomorrow, with a new label."""
    await call(hass, "add_item", INBOX, item="milk tomorrow #Home p1 @errands")
    task = next(t for t in seeded.tasks.values() if t["title"] == "milk")
    assert task["project_id"] == "home"
    assert task["due"] == {"date": "2026-10-26"}
    assert task["priority"] == 1
    assert [seeded.labels[i]["name"] for i in task["labels"]] == ["errands"]
    assert "milk" in [i["summary"] for i in await items(hass, HOME)]


@pytest.mark.usefixtures("setup_integration")
async def test_add_item_uses_existing_label_and_reminder(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """A label is matched by name (no duplicate), and a quick-add reminder is created."""
    seeded.add_label("Errands")
    await call(hass, "add_item", HOME, item="bread today @errands !1h")
    task = next(t for t in seeded.tasks.values() if t["title"] == "bread")
    assert len(seeded.labels) == 1
    assert task["project_id"] == "home"
    reminder = next(iter(seeded.reminders.values()))
    assert reminder == {**reminder, "task_id": task["id"], "kind": "relative", "offset_minutes": -60}


@pytest.mark.usefixtures("setup_integration")
async def test_add_item_explicit_values_win(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """A due date and notes given in Home Assistant win over the line."""
    await call(
        hass, "add_item", WORK, item="slides tomorrow", due_datetime="2026-10-27 09:15:00", description="for Monday"
    )
    task = next(t for t in seeded.tasks.values() if t["title"] == "slides")
    assert task["due"] == {"date": "2026-10-27", "time": "09:15"}
    assert task["description"] == "for Monday"
    assert task["project_id"] == "work"


@pytest.mark.usefixtures("setup_integration")
async def test_add_item_to_today(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """An item added to Today is due today."""
    await call(hass, "add_item", TODAY, item="stretch")
    task = next(t for t in seeded.tasks.values() if t["title"] == "stretch")
    assert task["due"] == {"date": "2026-10-25"}
    assert task["project_id"] is None


@pytest.mark.parametrize("options", [{CONF_PUSH: False, CONF_QUICK_ADD: False}])
@pytest.mark.usefixtures("setup_integration")
async def test_add_item_without_quick_add(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """With quick add off, the text is the title."""
    await call(hass, "add_item", INBOX, item="milk tomorrow #Home")
    assert any(t["title"] == "milk tomorrow #Home" and t["project_id"] is None for t in seeded.tasks.values())
    assert not any(path.endswith("quick-add/parse") for _, path, _ in seeded.requests)


@pytest.mark.usefixtures("setup_integration")
async def test_update_item(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """Renaming, notes, a new due time and completing write only what changed."""
    await call(hass, "update_item", HOME, item="Water plants", rename="Water the plants", description="balcony")
    assert seeded.requests[-1] == (
        "PATCH",
        "/api/v1/tasks/t-today",
        {"title": "Water the plants", "description": "balcony"},
    )
    await call(hass, "update_item", HOME, item="t-today", due_datetime="2026-10-25 07:00:00")
    assert seeded.tasks["t-today"]["due"] == {"date": "2026-10-25", "time": "07:00"}
    await call(hass, "update_item", HOME, item="t-today", status="completed")
    assert seeded.tasks["t-today"]["completed_at"]
    assert seeded.requests[-1][2].keys() == {"completed_at"}
    await call(hass, "update_item", HOME, item="t-today", status="needs_action")
    assert seeded.tasks["t-today"]["completed_at"] is None
    await call(hass, "update_item", HOME, item="t-today", due_date=None)
    assert seeded.tasks["t-today"]["due"] is None


@pytest.mark.usefixtures("setup_integration")
async def test_zoned_due_is_kept_when_unchanged(hass: HomeAssistant, seeded: FakeOpenTodo, freezer: Any) -> None:
    """A task with a zoned time shows in Home Assistant's zone and is not rewritten by an unrelated edit."""
    seeded.add_task(
        "Call Tokyo", id="z", project_id="work", due={"date": "2026-10-26", "time": "17:00", "tz": "Asia/Tokyo"}
    )
    await tick(hass, freezer, 3)
    item = next(i for i in await items(hass, WORK) if i["uid"] == "z")
    assert item["due"] == "2026-10-26T09:00:00+01:00"
    await call(hass, "update_item", WORK, item="z", rename="Call Kenji")
    assert seeded.requests[-1][2] == {"title": "Call Kenji"}
    assert seeded.tasks["z"]["due"]["tz"] == "Asia/Tokyo"


@pytest.mark.usefixtures("setup_integration")
async def test_complete_repeating_task(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """Completing a repeating task moves it on; it stays open in the list."""
    seeded.tasks["t-today"]["recurrence"] = "every day"
    await call(hass, "update_item", HOME, item="t-today", status="completed")
    assert seeded.tasks["t-today"]["due"] == {"date": "2026-10-26"}
    assert seeded.tasks["t-today"]["completed_at"] is None


@pytest.mark.usefixtures("setup_integration")
async def test_remove_items(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """Removing items moves them to OpenTodo's Trash."""
    await call(hass, "remove_item", HOME, item=["Water plants", "Pay rent"])
    assert seeded.tasks["t-today"]["deleted_at"] and seeded.tasks["t-timed"]["deleted_at"]
    assert "Water plants" not in [i["summary"] for i in await items(hass, HOME)]
    await call(hass, "remove_completed_items", HOME)
    assert seeded.tasks["t-done"]["deleted_at"]


async def move(
    hass: HomeAssistant, hass_ws_client: Any, entity_id: str, uid: str, previous: str | None
) -> dict[str, Any]:
    """Move an item through the websocket command the frontend uses."""
    client = await hass_ws_client(hass)
    message: dict[str, Any] = {"type": "todo/item/move", "entity_id": entity_id, "uid": uid}
    if previous:
        message["previous_uid"] = previous
    await client.send_json_auto_id(message)
    return await client.receive_json()


@pytest.mark.usefixtures("setup_integration")
async def test_move_item(hass: HomeAssistant, seeded: FakeOpenTodo, hass_ws_client: Any) -> None:
    """Moving writes a position between the new neighbours."""
    # Positions: t-today 2000, t-late 3000, t-timed 4000, t-later 5000.
    result = await move(hass, hass_ws_client, HOME, "t-later", "t-today")
    assert result["success"]
    assert seeded.tasks["t-later"]["position"] == 2500.0
    result = await move(hass, hass_ws_client, HOME, "t-timed", None)
    assert seeded.tasks["t-timed"]["position"] == 1000.0
    assert [i["summary"] for i in await items(hass, HOME)][:3] == ["Pay rent", "Water plants", "Later"]


@pytest.mark.usefixtures("setup_integration")
async def test_move_without_room_renumbers(
    hass: HomeAssistant, seeded: FakeOpenTodo, hass_ws_client: Any, freezer: Any
) -> None:
    """Neighbours with the same position are renumbered first."""
    for task_id in ("t-today", "t-late", "t-timed", "t-later"):
        seeded.tasks[task_id]["position"] = 7.0
    await tick(hass, freezer, 3)
    order = [i["uid"] for i in await items(hass, HOME)][:4]
    result = await move(hass, hass_ws_client, HOME, order[3], order[0])
    assert result["success"]
    assert [i["uid"] for i in await items(hass, HOME)][:4] == [order[0], order[3], order[1], order[2]]


@pytest.mark.usefixtures("setup_integration")
async def test_move_into_section(hass: HomeAssistant, seeded: FakeOpenTodo, hass_ws_client: Any, freezer: Any) -> None:
    """A task moved next to a task of another section joins that section."""
    seeded.sections["s1"] = {"id": "s1", "name": "A", "project_id": "home", "position": 1.0}
    seeded.tasks["t-later"]["section_id"] = "s1"
    await tick(hass, freezer, 3)
    await move(hass, hass_ws_client, HOME, "t-today", "t-later")
    assert seeded.tasks["t-today"]["section_id"] == "s1"


@pytest.mark.usefixtures("setup_integration")
async def test_server_errors_are_shown(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """A refused write and an unreachable server become clear errors."""
    seeded.fail_next_write = (403, "forbidden", "viewers cannot change tasks")
    with pytest.raises(HomeAssistantError, match="viewers cannot change tasks"):
        await call(hass, "update_item", HOME, item="t-today", rename="x")
    seeded.offline = True
    with pytest.raises(HomeAssistantError, match="cannot be reached"):
        await call(hass, "add_item", INBOX, item="offline task")


@pytest.mark.parametrize(
    "options", [{CONF_PUSH: False, CONF_ALL_PROJECTS: False, CONF_PROJECTS: ["work"], CONF_TODAY_LIST: False}]
)
@pytest.mark.usefixtures("setup_integration")
async def test_chosen_projects_only(hass: HomeAssistant, seeded: FakeOpenTodo) -> None:
    """With a choice of projects, only those (and the Inbox) get a list."""
    assert hass.states.get(WORK) is not None
    assert hass.states.get(HOME) is None
    assert hass.states.get(TODAY) is None


@pytest.mark.usefixtures("setup_integration")
async def test_new_and_archived_projects(hass: HomeAssistant, seeded: FakeOpenTodo, freezer: Any) -> None:
    """A new project gets a list at the next update; an archived one becomes unavailable."""
    seeded.add_project("Garden", id="garden")
    seeded.projects["work"]["archived"] = True
    await tick(hass, freezer, 3)
    assert hass.states.get("todo.garden") is not None
    assert hass.states.get(WORK).state == "unavailable"


async def test_read_only_token(
    hass: HomeAssistant, seeded: FakeOpenTodo, config_entry: MockConfigEntry, freezer: Any
) -> None:
    """A read token shows the lists without any way to change them."""
    freezer.move_to("2026-10-25 10:00:00+01:00")
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, "token": READ_TOKEN})
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(INBOX).attributes[ATTR_SUPPORTED_FEATURES] == 0
    assert not hass.states.get(HOME).attributes[ATTR_SUPPORTED_FEATURES] & TodoListEntityFeature.CREATE_TODO_ITEM
    with pytest.raises(HomeAssistantError, match="read only"):
        await hass.services.async_call("opentodo", "add_task", {"text": "nope"}, blocking=True)
    assert er.async_get(hass).async_get(INBOX) is not None
