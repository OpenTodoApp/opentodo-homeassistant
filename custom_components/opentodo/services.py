"""The `opentodo.add_task` action."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import DOMAIN, SERVICE_ADD_TASK
from .coordinator import OpenTodoConfigEntry

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_TEXT = "text"
ATTR_PROJECT = "project"
ATTR_DUE_DATE = "due_date"
ATTR_DUE_DATETIME = "due_datetime"
ATTR_PRIORITY = "priority"
ATTR_LABELS = "labels"
ATTR_DESCRIPTION = "description"
ATTR_QUICK_ADD = "quick_add"

ADD_TASK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_TEXT): vol.All(cv.string, vol.Length(min=1, max=2000)),
        vol.Optional(ATTR_PROJECT): cv.string,
        vol.Exclusive(ATTR_DUE_DATE, "due"): cv.date,
        vol.Exclusive(ATTR_DUE_DATETIME, "due"): cv.datetime,
        vol.Optional(ATTR_PRIORITY): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
        vol.Optional(ATTR_LABELS): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_DESCRIPTION): cv.string,
        vol.Optional(ATTR_QUICK_ADD, default=True): cv.boolean,
    }
)


def _entry(hass: HomeAssistant, entry_id: str | None) -> OpenTodoConfigEntry:
    """The config entry named, or the only loaded one."""
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_found")
    elif len(entries) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_required" if entries else "entry_not_found"
        )
    return entries[0]


def task_response(entry: OpenTodoConfigEntry, task: dict[str, Any]) -> dict[str, Any]:
    """The created task as the action answers it."""
    coordinator = entry.runtime_data
    payload = coordinator.task_event_data(task, delivery="action")
    for key in ("delivery", "source", "config_entry_id"):
        payload.pop(key, None)
    payload["id"] = payload.pop("task_id")
    payload["due_value"] = task.get("due")
    payload["recurrence"] = task.get("recurrence")
    return payload


async def _async_add_task(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry = _entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    coordinator = entry.runtime_data
    project_id = None
    if project := call.data.get(ATTR_PROJECT):
        project_id = coordinator.find_project(project)
        if project_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="project_not_found",
                translation_placeholders={"project": project},
            )
    due = call.data.get(ATTR_DUE_DATETIME) or call.data.get(ATTR_DUE_DATE)
    task = await coordinator.async_create_task(
        call.data[ATTR_TEXT],
        project_id=project_id,
        due=due,
        description=call.data.get(ATTR_DESCRIPTION),
        priority=call.data.get(ATTR_PRIORITY),
        labels=call.data.get(ATTR_LABELS),
        quick_add=call.data[ATTR_QUICK_ADD],
        explicit_project=project_id is not None,
    )
    return {"task": task_response(entry, task)}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once for the integration."""
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_TASK, _async_add_task, schema=ADD_TASK_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
