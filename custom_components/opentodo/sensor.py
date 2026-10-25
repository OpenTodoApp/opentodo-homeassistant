"""Counts: Today, Earlier (late tasks), the Inbox and each project's open tasks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import logic
from .const import MAX_ATTRIBUTE_TASKS
from .coordinator import OpenTodoConfigEntry, OpenTodoCoordinator
from .entity import OpenTodoEntity, remove_stale_entities
from .todo import exposed_projects

PARALLEL_UPDATES = 0


def today_tasks(coordinator: OpenTodoCoordinator) -> list[dict[str, Any]]:
    """Today's list as the app shows it: due today or earlier, a deadline reached, or a start date come."""
    data = coordinator.data
    zone = coordinator.zone
    today = coordinator.today()
    live = logic.without_archived(data.tasks.values(), data.archived_ids())
    tasks = [t for t in live if logic.in_today(t, today, zone)]
    return sorted(tasks, key=lambda t: logic.due_then_priority_key(t, zone))


def earlier_tasks(coordinator: OpenTodoCoordinator) -> list[dict[str, Any]]:
    """Today's Earlier group: late tasks (due or with a deadline before today), blocked ones left out."""
    zone = coordinator.zone
    today = coordinator.today()
    blocked = logic.blocked_ids(coordinator.data.tasks)
    return [t for t in today_tasks(coordinator) if t["id"] not in blocked and logic.is_earlier(t, today, zone)]


def inbox_tasks(coordinator: OpenTodoCoordinator) -> list[dict[str, Any]]:
    """The Inbox: open tasks with no project and no schedule."""
    data = coordinator.data
    tasks = [t for t in data.tasks.values() if logic.is_inbox_task(t, data.tasks.get)]
    return logic.tree_order(tasks, data.sections)


def project_tasks(coordinator: OpenTodoCoordinator, project_id: str) -> list[dict[str, Any]]:
    """A project's open, active tasks: the sidebar's count."""
    data = coordinator.data
    today = coordinator.today()
    tasks = [t for t in data.tasks.values() if t.get("project_id") == project_id and logic.counts_open(t, today)]
    return logic.tree_order(tasks, data.sections)


@dataclass(frozen=True, kw_only=True)
class OpenTodoSensorDescription(SensorEntityDescription):
    """A count of tasks."""

    tasks_fn: Callable[[OpenTodoCoordinator], list[dict[str, Any]]]


SENSORS: tuple[OpenTodoSensorDescription, ...] = (
    OpenTodoSensorDescription(key="today", translation_key="today", tasks_fn=today_tasks),
    OpenTodoSensorDescription(key="earlier", translation_key="earlier", tasks_fn=earlier_tasks),
    OpenTodoSensorDescription(key="inbox", translation_key="inbox", tasks_fn=inbox_tasks),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: OpenTodoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the counts, and one for every project that appears later."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [OpenTodoCountSensor(coordinator, description) for description in SENSORS]
    known: set[str] = set()
    for project in exposed_projects(coordinator):
        known.add(project["id"])
        entities.append(OpenTodoProjectSensor(coordinator, project["id"]))
    remove_stale_entities(hass, entry, "sensor", entities)
    async_add_entities(entities)

    @callback
    def _add_new_projects() -> None:
        new = [p for p in exposed_projects(coordinator) if p["id"] not in known]
        if new:
            known.update(p["id"] for p in new)
            async_add_entities(OpenTodoProjectSensor(coordinator, p["id"]) for p in new)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_projects))


def _summaries(coordinator: OpenTodoCoordinator, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first tasks of a count, for dashboards and notifications."""
    zone = coordinator.zone
    projects = coordinator.data.projects
    out = []
    for task in tasks[:MAX_ATTRIBUTE_TASKS]:
        moment = logic.due_moment(task.get("due"), zone)
        project = projects.get(task.get("project_id") or "")
        out.append(
            {
                "id": task["id"],
                "title": task.get("title", ""),
                "due": moment.isoformat() if moment is not None else None,
                "priority": task.get("priority", 4),
                "project": project.get("name") if project else None,
            }
        )
    return out


class OpenTodoCountSensor(OpenTodoEntity, SensorEntity):
    """How many tasks are in Today, Earlier or the Inbox."""

    entity_description: OpenTodoSensorDescription
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OpenTodoCoordinator, description: OpenTodoSensorDescription) -> None:
        """One count."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int:
        """The number of tasks."""
        return len(self.entity_description.tasks_fn(self.coordinator))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The first tasks, so a card or notification can list them."""
        return {"tasks": _summaries(self.coordinator, self.entity_description.tasks_fn(self.coordinator))}


class OpenTodoProjectSensor(OpenTodoEntity, SensorEntity):
    """How many open tasks a project has."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "project"

    def __init__(self, coordinator: OpenTodoCoordinator, project_id: str) -> None:
        """One project's count."""
        super().__init__(coordinator, f"project_{project_id}")
        self._project_id = project_id
        project = coordinator.data.projects.get(project_id) if coordinator.data else None
        self._attr_translation_placeholders = {"project": str(project.get("name")) if project else project_id}

    @property
    def available(self) -> bool:
        """Unavailable once the project is deleted or archived."""
        project = self.coordinator.data.projects.get(self._project_id) if self.coordinator.data else None
        return super().available and project is not None and not project.get("archived")

    @property
    def native_value(self) -> int:
        """The number of open, active tasks."""
        return len(project_tasks(self.coordinator, self._project_id))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The first tasks and the project id."""
        return {
            "project_id": self._project_id,
            "tasks": _summaries(self.coordinator, project_tasks(self.coordinator, self._project_id)),
        }
