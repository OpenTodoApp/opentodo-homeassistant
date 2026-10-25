"""Data coordinator: polls OpenTodo, applies pushed changes and fires bus events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, tzinfo
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import logic
from .api import OpenTodoApiError, OpenTodoAuthError, OpenTodoClient, OpenTodoConnectionError, OpenTodoError
from .const import (
    CONF_COMPLETED_DAYS,
    CONF_QUICK_ADD,
    CONF_SCAN_INTERVAL,
    DEFAULT_COMPLETED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOCS_URL,
    DOMAIN,
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_CREATED,
    EVENT_TASK_DELETED,
    EVENT_TASK_DUE,
    EVENT_TASK_REOPENED,
    EVENT_TASK_UPDATED,
    ISSUE_ENCRYPTED,
    SCOPE_READ,
)

_LOGGER = logging.getLogger(__name__)

# While pushed changes arrive, polling is only a safety net.
PUSH_SCAN_INTERVAL = timedelta(minutes=15)

# Fields that never produce an update event on their own.
QUIET_FIELDS = {"notes_doc"}

type OpenTodoConfigEntry = ConfigEntry[OpenTodoCoordinator]


@dataclass(frozen=True)
class OpenTodoData:
    """One snapshot of the account."""

    me: dict[str, Any]
    version: str
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def user(self) -> dict[str, Any]:
        """The signed-in account."""
        return self.me.get("user") or {}

    @property
    def scope(self) -> str:
        """The token's scope."""
        return str((self.me.get("auth") or {}).get("scope", "full"))

    @property
    def encrypted(self) -> bool:
        """Whether the account uses end-to-end encryption."""
        return bool((self.me.get("encryption") or {}).get("enabled"))

    def active_projects(self) -> list[dict[str, Any]]:
        """Live, not archived projects, by position."""
        return sorted(
            (p for p in self.projects.values() if not p.get("archived") and not p.get("deleted_at")),
            key=logic.position_key,
        )

    def archived_ids(self) -> set[str]:
        """Archived project ids."""
        return {p["id"] for p in self.projects.values() if p.get("archived")}


class OpenTodoCoordinator(DataUpdateCoordinator[OpenTodoData]):
    """Keep one account's tasks, projects, labels and sections."""

    config_entry: OpenTodoConfigEntry

    def __init__(self, hass: HomeAssistant, entry: OpenTodoConfigEntry, client: OpenTodoClient) -> None:
        """Set up the coordinator with the interval from the options."""
        self._base_interval = timedelta(minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self._base_interval,
        )
        self.client = client
        self.push_active = False
        self.push_error: str | None = None
        self._due_checked: datetime = dt_util.now()
        self._seen_deliveries: list[str] = []

    # ---- options and clock ----------------------------------------------------

    @property
    def zone(self) -> tzinfo:
        """Home Assistant's time zone, used for floating dates."""
        return dt_util.get_default_time_zone()

    def today(self) -> str:
        """Today's date in Home Assistant's time zone."""
        return dt_util.now().date().isoformat()

    @property
    def read_only(self) -> bool:
        """A read token: nothing can be written."""
        return self.data is not None and self.data.scope == SCOPE_READ

    @property
    def completed_days(self) -> int:
        """How long completed tasks stay in the lists."""
        return int(self.config_entry.options.get(CONF_COMPLETED_DAYS, DEFAULT_COMPLETED_DAYS))

    @callback
    def set_push_active(self, active: bool, error: str | None = None) -> None:
        """Record whether OpenTodo pushes changes, and poll less often while it does."""
        self.push_active = active
        self.push_error = error
        self.update_interval = max(self._base_interval, PUSH_SCAN_INTERVAL) if active else self._base_interval

    # ---- fetching -------------------------------------------------------------

    async def _async_update_data(self) -> OpenTodoData:
        """Fetch everything, then derive events from what changed since last time."""
        try:
            me, health, tasks, projects, labels, sections = await asyncio.gather(
                self.client.me(),
                self.client.health(),
                self.client.tasks(),
                self.client.projects(),
                self.client.labels(),
                self.client.sections(),
            )
        except OpenTodoAuthError as err:
            raise ConfigEntryAuthFailed("The OpenTodo token was refused") from err
        except OpenTodoConnectionError as err:
            raise UpdateFailed(f"OpenTodo cannot be reached: {err}") from err
        except OpenTodoApiError as err:
            raise UpdateFailed(f"OpenTodo refused the request: {err}") from err

        data = OpenTodoData(
            me=me,
            version=str(health.get("version", "")),
            tasks={t["id"]: t for t in tasks if isinstance(t, dict) and "id" in t and not t.get("deleted_at")},
            projects={p["id"]: p for p in projects if isinstance(p, dict) and "id" in p and not p.get("deleted_at")},
            labels={lb["id"]: lb for lb in labels if isinstance(lb, dict) and "id" in lb and not lb.get("deleted_at")},
            sections={s["id"]: s for s in sections if isinstance(s, dict) and "id" in s and not s.get("deleted_at")},
        )
        self._check_encryption(data)
        if self.data is not None and not self.push_active:
            self._fire_diff_events(self.data, data)
        return data

    def _check_encryption(self, data: OpenTodoData) -> None:
        """Stop with a repair issue when the account is end-to-end encrypted."""
        issue_id = f"{ISSUE_ENCRYPTED}_{self.config_entry.entry_id}"
        if not data.encrypted:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_ENCRYPTED,
            translation_placeholders={"title": self.config_entry.title},
            learn_more_url=DOCS_URL,
        )
        if self.data is None:
            raise ConfigEntryError("The OpenTodo account uses end-to-end encryption")
        raise UpdateFailed("The OpenTodo account uses end-to-end encryption")

    # ---- local changes ----------------------------------------------------------

    @callback
    def apply_task(self, task: dict[str, Any] | None, *, removed: str | None = None) -> None:
        """Put a task the server returned (or a removal) into the current data at once."""
        if self.data is None:
            return
        tasks = dict(self.data.tasks)
        if removed:
            tasks.pop(removed, None)
        if task and "id" in task:
            if task.get("deleted_at"):
                tasks.pop(task["id"], None)
            else:
                tasks[task["id"]] = task
        self.async_set_updated_data(replace(self.data, tasks=tasks))

    @callback
    def remember_delivery(self, delivery_id: str) -> bool:
        """False when this webhook delivery was already handled (deliveries are at least once)."""
        if delivery_id in self._seen_deliveries:
            return False
        self._seen_deliveries.append(delivery_id)
        del self._seen_deliveries[:-500]
        return True

    # ---- events -------------------------------------------------------------------

    def task_event_data(
        self, task: dict[str, Any], *, delivery: str, source: str | None = None, changes: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """The payload of an `opentodo_task_*` event."""
        data = self.data
        project = data.projects.get(task.get("project_id") or "") if data else None
        labels = data.labels if data else {}
        moment = logic.due_moment(task.get("due"), self.zone)
        payload: dict[str, Any] = {
            "config_entry_id": self.config_entry.entry_id,
            "task_id": task.get("id"),
            "title": task.get("title", ""),
            "description": task.get("description") or "",
            "project_id": task.get("project_id"),
            "project_name": project.get("name") if project else None,
            "due": moment.isoformat() if moment is not None else None,
            "deadline": task.get("deadline"),
            "priority": task.get("priority", 4),
            "labels": [labels[i]["name"] for i in task.get("labels") or [] if i in labels],
            "assignee_id": task.get("assignee_id"),
            "completed_at": task.get("completed_at"),
            "completed_by": task.get("completed_by"),
            "series_id": task.get("series_id"),
            "url": f"{self.client.url}/?task={task.get('id')}",
            "delivery": delivery,
            "source": source,
        }
        if changes is not None:
            payload["changes"] = changes
        return payload

    @callback
    def fire_task_events(
        self,
        kind: str,
        task: dict[str, Any],
        *,
        delivery: str,
        source: str | None = None,
        changes: dict[str, Any] | None = None,
    ) -> None:
        """Fire the bus event(s) for one change: `kind` is created, updated, completed, reopened or deleted."""
        if kind == "created" and task.get("completed_at") and task.get("series_id"):
            # Completing a repeating task records a completed occurrence: that is a completion.
            kind = "completed"
        event = {
            "created": EVENT_TASK_CREATED,
            "updated": EVENT_TASK_UPDATED,
            "completed": EVENT_TASK_COMPLETED,
            "reopened": EVENT_TASK_REOPENED,
            "deleted": EVENT_TASK_DELETED,
        }.get(kind)
        if event is None:
            return
        payload = self.task_event_data(task, delivery=delivery, source=source, changes=changes)
        self.hass.bus.async_fire(event, payload)
        assigned = (changes or {}).get("assignee_id")
        newly_assigned = kind == "created" or (isinstance(assigned, dict) and bool(assigned.get("to")))
        if kind in ("created", "updated") and task.get("assignee_id") and newly_assigned:
            self.hass.bus.async_fire(EVENT_TASK_ASSIGNED, payload)

    def _fire_diff_events(self, old: OpenTodoData, new: OpenTodoData) -> None:
        """Without pushed changes: derive events from two snapshots."""
        for task_id, task in new.tasks.items():
            before = old.tasks.get(task_id)
            if before is None:
                self.fire_task_events("created", task, delivery="poll", changes=_changes(None, task))
                continue
            changes = _changes(before, task)
            if not changes:
                continue
            if "completed_at" in changes and task.get("completed_at") and not before.get("completed_at"):
                kind = "completed"
            elif "completed_at" in changes and not task.get("completed_at"):
                kind = "reopened"
            else:
                kind = "updated"
            self.fire_task_events(kind, task, delivery="poll", changes=changes)
        for task_id, task in old.tasks.items():
            if task_id not in new.tasks:
                self.fire_task_events("deleted", task, delivery="poll")

    @callback
    def async_start_due_tracking(self) -> None:
        """Fire `opentodo_task_due` once a minute for tasks whose due moment has just come."""
        self._due_checked = dt_util.now()
        self.config_entry.async_on_unload(async_track_time_change(self.hass, self._check_due, second=0))

    @callback
    def _check_due(self, now: datetime) -> None:
        since, self._due_checked = self._due_checked, now
        if self.data is None:
            return
        zone = self.zone
        for task in self.data.tasks.values():
            if not logic.is_open(task):
                continue
            moment = logic.due_moment(task.get("due"), zone)
            if moment is None:
                continue
            all_day = not isinstance(moment, datetime)
            at = dt_util.start_of_local_day(moment) if isinstance(moment, date) and all_day else moment
            if since < at <= now:  # type: ignore[operator]
                payload = self.task_event_data(task, delivery="clock")
                payload["all_day"] = all_day
                self.hass.bus.async_fire(EVENT_TASK_DUE, payload)

    # ---- writing --------------------------------------------------------------------

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="read_only",
            )

    async def async_write(self, coro: Any) -> Any:
        """Run one write and turn the client's errors into messages for the user."""
        self._ensure_writable()
        try:
            return await coro
        except OpenTodoAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed") from err
        except OpenTodoConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        except OpenTodoApiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"message": err.message, "code": err.code},
            ) from err

    async def async_update_task(self, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Change a task and show the result at once."""
        task = await self.async_write(self.client.update_task(task_id, fields))
        self.apply_task(task)
        return task

    async def async_delete_tasks(self, task_ids: list[str]) -> None:
        """Move tasks to the Trash."""
        for task_id in task_ids:
            await self.async_write(self.client.delete_task(task_id))
            self.apply_task(None, removed=task_id)

    def find_project(self, value: str) -> str | None:
        """A project id by id or by name (ignoring case)."""
        if self.data is None:
            return None
        if value in self.data.projects:
            return value
        folded = value.casefold().lstrip("#")
        for project in self.data.active_projects():
            if str(project.get("name", "")).casefold() == folded:
                return project["id"]
        return None

    async def _label_ids(self, names: list[str]) -> list[str]:
        """Label ids by name; a label that does not exist yet is created."""
        known = {
            str(lb.get("name", "")).casefold(): lb["id"] for lb in (self.data.labels.values() if self.data else [])
        }
        ids: list[str] = []
        for raw in names:
            name = raw.strip().lstrip("@")
            if not name:
                continue
            label_id = known.get(name.casefold())
            if label_id is None:
                label = await self.async_write(self.client.create_label(name))
                label_id = label["id"]
                if self.data is not None:
                    self.data = replace(self.data, labels={**self.data.labels, label_id: label})
                known[name.casefold()] = label_id
            if label_id not in ids:
                ids.append(label_id)
        return ids

    async def async_create_task(
        self,
        text: str,
        *,
        project_id: str | None = None,
        due_today: bool = False,
        due: date | datetime | None = None,
        description: str | None = None,
        priority: int | None = None,
        labels: list[str] | None = None,
        quick_add: bool | None = None,
        explicit_project: bool = False,
    ) -> dict[str, Any]:
        """Create a task from a line of text, reading quick-add syntax when enabled.

        Values given explicitly win over what the line says; a `#project` in the
        line wins over the list the item was added to, but not over a project
        named explicitly (`explicit_project`).
        """
        self._ensure_writable()
        if quick_add is None:
            quick_add = bool(self.config_entry.options.get(CONF_QUICK_ADD, True))
        now = dt_util.now()
        body: dict[str, Any] = {"title": text.strip(), "project_id": project_id}
        label_names: list[str] = []
        label_ids: list[str] = []
        reminders: list[dict[str, Any]] = []
        if quick_add and text.strip():
            parsed = await self.async_write(
                self.client.parse_quick_add(text.strip(), now.date().isoformat(), now.strftime("%H:%M"))
            )
            fields = parsed.get("fields") or {}
            if parsed.get("title"):
                body["title"] = parsed["title"]
            for key in (
                "due",
                "deadline",
                "start",
                "evening",
                "project_id",
                "priority",
                "estimate_minutes",
                "recurrence",
            ):
                if key in fields and fields[key] is not None:
                    if key == "project_id" and explicit_project:
                        continue
                    body[key] = fields[key]
            for label in fields.get("labels") or []:
                if isinstance(label, dict) and label.get("id"):
                    label_ids.append(label["id"])
                elif isinstance(label, dict) and label.get("name"):
                    label_names.append(label["name"])
            reminders = [r for r in fields.get("reminders") or [] if isinstance(r, dict)]
        if due is not None:
            body["due"] = logic.due_from_ha(due, self.zone)
        elif due_today and "due" not in body:
            body["due"] = {"date": now.date().isoformat()}
        if description:
            body["description"] = description
        if priority is not None:
            body["priority"] = priority
        label_names.extend(labels or [])
        if label_names:
            label_ids.extend(await self._label_ids(label_names))
        if label_ids:
            body["labels"] = list(dict.fromkeys(label_ids))
        if not body["title"]:
            body["title"] = text.strip()
        task = await self.async_write(self.client.create_task(body))
        for reminder in reminders:
            await self._create_reminder(task, reminder)
        self.apply_task(task)
        return task

    async def _create_reminder(self, task: dict[str, Any], reminder: dict[str, Any]) -> None:
        """Add a reminder the quick-add line asked for; a failure does not undo the task."""
        fields: dict[str, Any] = {"task_id": task["id"]}
        if reminder.get("kind") == "relative":
            fields.update(kind="relative", offset_minutes=reminder.get("offset_minutes", 0))
        elif reminder.get("kind") == "absolute" and reminder.get("date"):
            moment = logic.due_moment({"date": reminder["date"], "time": reminder.get("time") or "09:00"}, self.zone)
            if not isinstance(moment, datetime):
                return
            fields.update(kind="absolute", fire_at=int(moment.timestamp() * 1000))
        else:
            return
        try:
            await self.client.create_reminder(fields)
        except OpenTodoError as err:
            _LOGGER.warning("The reminder for task %s could not be created: %s", task.get("id"), err)


def _changes(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    """The changed fields as `{field: {from, to}}`, like a webhook's `changes`."""
    out: dict[str, Any] = {}
    keys = set(after) | set(before or {})
    for key in sorted(keys - QUIET_FIELDS):
        old = (before or {}).get(key)
        new = after.get(key)
        if old != new and not (old in (None, "", [], {}) and new in (None, "", [], {})):
            out[key] = {"from": old, "to": new}
    return out
