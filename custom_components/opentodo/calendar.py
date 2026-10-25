"""A read-only calendar of the tasks that have a do-on date."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import logic
from .const import DEFAULT_EVENT_MINUTES
from .coordinator import OpenTodoConfigEntry, OpenTodoCoordinator
from .entity import OpenTodoEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: OpenTodoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the account's calendar."""
    async_add_entities([OpenTodoCalendar(entry.runtime_data)])


def task_event(coordinator: OpenTodoCoordinator, task: dict[str, Any]) -> CalendarEvent | None:
    """A task with a do-on date as an event: all day, or a block of its estimate (30 minutes without one)."""
    moment = logic.due_moment(task.get("due"), coordinator.zone)
    if moment is None:
        return None
    title = task.get("title") or ""
    if logic.is_ciphertext(title):
        return None
    if isinstance(moment, datetime):
        minutes = task.get("estimate_minutes") or DEFAULT_EVENT_MINUTES
        start: date | datetime = moment
        end: date | datetime = moment + timedelta(minutes=int(minutes))
    else:
        start, end = moment, moment + timedelta(days=1)
    project = coordinator.data.projects.get(task.get("project_id") or "")
    location = task.get("location") if isinstance(task.get("location"), dict) else None
    description = task.get("description") or ""
    if project and project.get("name"):
        description = f"{project['name']}\n\n{description}".strip()
    return CalendarEvent(
        start=start,
        end=end,
        summary=title,
        description=description or None,
        location=(location.get("name") or location.get("address")) if location else None,
        uid=task["id"],
    )


def _local(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    return dt_util.start_of_local_day(value)


class OpenTodoCalendar(OpenTodoEntity, CalendarEntity):
    """Tasks with a do-on date, open and completed, as calendar events."""

    _attr_translation_key = "tasks"

    def __init__(self, coordinator: OpenTodoCoordinator) -> None:
        """The account's calendar."""
        super().__init__(coordinator, "calendar")

    def _events(self, *, open_only: bool = False) -> list[CalendarEvent]:
        data = self.coordinator.data
        if data is None:
            return []
        archived = data.archived_ids()
        events = []
        for task in data.tasks.values():
            if task.get("project_id") in archived or (open_only and task.get("completed_at")):
                continue
            if task.get("skipped"):
                continue
            if (event := task_event(self.coordinator, task)) is not None:
                events.append(event)
        return sorted(events, key=lambda e: _local(e.start))

    @property
    def event(self) -> CalendarEvent | None:
        """The current or next open task."""
        now = dt_util.now()
        for event in self._events(open_only=True):
            if _local(event.end) > now:
                return event
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Tasks whose event overlaps the range."""
        return [e for e in self._events() if _local(e.start) < end_date and _local(e.end) > start_date]
