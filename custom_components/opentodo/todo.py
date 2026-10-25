"""To-do lists: the Inbox, Today and one list per project."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import logic
from .const import CONF_ALL_PROJECTS, CONF_PROJECTS, CONF_TODAY_LIST, DOMAIN, INBOX_KEY, TODAY_KEY
from .coordinator import OpenTodoConfigEntry, OpenTodoCoordinator, OpenTodoData
from .entity import OpenTodoEntity, remove_stale_entities

PARALLEL_UPDATES = 0

WRITE_FEATURES = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
)


def exposed_projects(coordinator: OpenTodoCoordinator) -> list[dict[str, Any]]:
    """The projects that get a list (and a count), per the options."""
    options = coordinator.config_entry.options
    projects = coordinator.data.active_projects() if coordinator.data else []
    if options.get(CONF_ALL_PROJECTS, True):
        return projects
    chosen = set(options.get(CONF_PROJECTS) or [])
    return [p for p in projects if p["id"] in chosen]


async def async_setup_entry(
    hass: HomeAssistant, entry: OpenTodoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the lists, and a list for every project that appears later."""
    coordinator = entry.runtime_data
    entities: list[OpenTodoListEntity] = [OpenTodoListEntity(coordinator, INBOX_KEY)]
    if entry.options.get(CONF_TODAY_LIST, True):
        entities.append(OpenTodoListEntity(coordinator, TODAY_KEY))
    known: set[str] = set()
    for project in exposed_projects(coordinator):
        known.add(project["id"])
        entities.append(OpenTodoListEntity(coordinator, project["id"]))
    remove_stale_entities(hass, entry, "todo", entities)
    async_add_entities(entities)

    @callback
    def _add_new_projects() -> None:
        new = [p for p in exposed_projects(coordinator) if p["id"] not in known]
        if new:
            known.update(p["id"] for p in new)
            async_add_entities(OpenTodoListEntity(coordinator, p["id"]) for p in new)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_projects))


class OpenTodoListEntity(OpenTodoEntity, TodoListEntity):
    """One OpenTodo list as a Home Assistant to-do list."""

    def __init__(self, coordinator: OpenTodoCoordinator, key: str) -> None:
        """A list for the Inbox, Today or one project."""
        super().__init__(coordinator, f"todo_{key}")
        # Lists are not attached to the account's device: Assist matches a list by its
        # full name, and "add milk to the Home list" should find the list called Home
        # (Local To-do and Todoist do the same).
        self._attr_device_info = None
        self._key = key
        if key in (INBOX_KEY, TODAY_KEY):
            self._attr_translation_key = key
        features = TodoListEntityFeature(0) if coordinator.read_only else WRITE_FEATURES
        if not coordinator.read_only and key != TODAY_KEY:
            features |= TodoListEntityFeature.MOVE_TODO_ITEM
        self._attr_supported_features = features
        self._tasks: list[dict[str, Any]] = []
        self._update_items()

    @property
    def project_id(self) -> str | None:
        """The project of a project list."""
        return None if self._key in (INBOX_KEY, TODAY_KEY) else self._key

    @property
    def name(self) -> str | None:  # type: ignore[override]
        """A project list is named after its project; the Inbox and Today are translated."""
        if self.project_id is None:
            return super().name  # type: ignore[return-value]
        project = self.coordinator.data.projects.get(self._key) if self.coordinator.data else None
        return str(project.get("name")) if project else self._key

    @property
    def available(self) -> bool:
        """A project list is unavailable once its project is deleted or archived."""
        if not super().available:
            return False
        if self.project_id is None:
            return True
        project = self.coordinator.data.projects.get(self._key)
        return project is not None and not project.get("archived")

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_items()
        super()._handle_coordinator_update()

    def _belongs(self, task: dict[str, Any], data: OpenTodoData, today: str) -> bool:
        """Whether a task (open, or completed recently) belongs to this list."""
        zone = self.coordinator.zone
        if self._key == INBOX_KEY:
            probe = {**task, "completed_at": None}
            return logic.is_inbox_task(probe, data.tasks.get)
        if self._key == TODAY_KEY:
            if task.get("project_id") in data.archived_ids():
                return False
            if task.get("completed_at"):
                due = logic.local_due_date(task.get("due"), zone)
                return due is not None and due <= today
            return logic.in_today(task, today, zone)
        return task.get("project_id") == self._key

    def _update_items(self) -> None:
        data = self.coordinator.data
        if data is None:
            self._attr_todo_items = None
            return
        zone = self.coordinator.zone
        today = self.coordinator.today()
        cutoff_ms = (dt_util.utcnow() - timedelta(days=self.coordinator.completed_days)).timestamp() * 1000
        open_tasks: list[dict[str, Any]] = []
        done: list[dict[str, Any]] = []
        for task in data.tasks.values():
            if task.get("deleted_at") or not self._belongs(task, data, today):
                continue
            completed = task.get("completed_at")
            if not completed:
                open_tasks.append(task)
            elif self.coordinator.completed_days > 0 and completed >= cutoff_ms and not task.get("skipped"):
                done.append(task)
        if self._key == TODAY_KEY:
            open_tasks.sort(key=lambda t: logic.due_then_priority_key(t, zone))
        else:
            open_tasks = logic.tree_order(open_tasks, data.sections)
        done.sort(key=lambda t: -(t.get("completed_at") or 0))
        self._tasks = open_tasks
        self._attr_todo_items = [self._item(t) for t in open_tasks + done]

    def _item(self, task: dict[str, Any]) -> TodoItem:
        title = task.get("title") or ""
        completed = task.get("completed_at")
        return TodoItem(
            summary=title if not logic.is_ciphertext(title) else "…",
            uid=task["id"],
            status=TodoItemStatus.COMPLETED if completed else TodoItemStatus.NEEDS_ACTION,
            due=logic.due_moment(task.get("due"), self.coordinator.zone),
            description=(task.get("description") or None) if not logic.is_ciphertext(task.get("description")) else None,
            completed=dt_util.utc_from_timestamp(completed / 1000) if completed else None,
        )

    # ---- writes ------------------------------------------------------------------

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add a task to this list, reading quick-add syntax in its text."""
        await self.coordinator.async_create_task(
            item.summary or "",
            project_id=self.project_id,
            due_today=self._key == TODAY_KEY,
            due=item.due,
            description=item.description,
        )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Write only what changed: the title, notes, due date and completion."""
        if not item.uid or self.coordinator.data is None:
            return
        task = self.coordinator.data.tasks.get(item.uid)
        if task is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="task_not_found")
        zone = self.coordinator.zone
        fields: dict[str, Any] = {}
        if item.summary is not None and item.summary != task.get("title") and item.summary != "…":
            fields["title"] = item.summary
        if (item.description or "") != (task.get("description") or "") and not logic.is_ciphertext(
            task.get("description")
        ):
            fields["description"] = item.description or ""
        current_due = logic.due_moment(task.get("due"), zone)
        if not logic.same_due(item.due, current_due):
            fields["due"] = logic.due_from_ha(item.due, zone)
        if item.status is not None:
            done = item.status == TodoItemStatus.COMPLETED
            if done and not task.get("completed_at"):
                fields["completed_at"] = int(dt_util.utcnow().timestamp() * 1000)
            elif not done and task.get("completed_at"):
                fields["completed_at"] = None
        if fields:
            await self.coordinator.async_update_task(item.uid, fields)

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Move tasks to OpenTodo's Trash (they can be restored there)."""
        await self.coordinator.async_delete_tasks(uids)

    async def async_move_todo_item(self, uid: str, previous_uid: str | None = None) -> None:
        """Move a task after another one: a position between its new neighbours, and their section."""
        await self._move(uid, previous_uid, renumbered=False)

    async def _move(self, uid: str, previous_uid: str | None, *, renumbered: bool) -> None:
        data = self.coordinator.data
        task = data.tasks.get(uid) if data else None
        if data is None or task is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="task_not_found")
        siblings = [t for t in self._tasks if t["id"] != uid and t.get("parent_id") == task.get("parent_id")]
        if previous_uid is None:
            before, after = None, siblings[0] if siblings else None
        else:
            prev = data.tasks.get(previous_uid)
            # Moving after a subtask of another parent: after that subtask's top-level task.
            while prev is not None and prev.get("parent_id") != task.get("parent_id") and prev.get("parent_id"):
                prev = data.tasks.get(prev["parent_id"])
            if prev is None or prev["id"] == uid or prev not in siblings:
                return
            index = siblings.index(prev)
            before, after = prev, siblings[index + 1] if index + 1 < len(siblings) else None
        fields: dict[str, Any] = {}
        position = logic.position_between(
            logic.position_key(before)[0] if before else None, logic.position_key(after)[0] if after else None
        )
        if position is None and (before or after) and not renumbered:
            # No room: renumber this list's siblings with even gaps, then place the task.
            await self._renumber(siblings)
            await self._move(uid, previous_uid, renumbered=True)
            return
        if position is not None:
            fields["position"] = position
        neighbour = before or after
        if self.project_id and neighbour is not None and neighbour.get("section_id") != task.get("section_id"):
            fields["section_id"] = neighbour.get("section_id")
        if fields:
            await self.coordinator.async_update_task(uid, fields)

    async def _renumber(self, siblings: list[dict[str, Any]]) -> None:
        start = logic.position_key(siblings[0])[0] if siblings else 0.0
        for index, sibling in enumerate(siblings):
            await self.coordinator.async_update_task(sibling["id"], {"position": start + index * 1000.0})
