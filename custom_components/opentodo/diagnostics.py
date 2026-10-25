"""Diagnostics: the setup and counts, never the token, the webhook secret or task content."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

from . import logic
from .const import DATA_REMOTE_WEBHOOK_SECRET, DATA_REMOTE_WEBHOOK_URL, DATA_WEBHOOK_ID, SCOPE_READ
from .coordinator import OpenTodoConfigEntry

TO_REDACT = {
    CONF_TOKEN,
    DATA_REMOTE_WEBHOOK_SECRET,
    DATA_WEBHOOK_ID,
    DATA_REMOTE_WEBHOOK_URL,
    "email",
    "name",
    "token_name",
    "token_id",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: OpenTodoConfigEntry) -> dict[str, Any]:
    """What helps to understand a problem, without personal data."""
    coordinator = entry.runtime_data
    data = coordinator.data
    tasks = list(data.tasks.values()) if data else []
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "server": {
            "version": data.version if data else None,
            "scope": data.scope if data else None,
            "read_only": data.scope == SCOPE_READ if data else None,
            "encrypted": data.encrypted if data else None,
            "account": async_redact_data(dict(data.me), TO_REDACT | {"user", "agent_excluded_projects"})
            if data
            else None,
        },
        "updates": {
            "push_active": coordinator.push_active,
            "push_error": coordinator.push_error,
            "update_interval_seconds": coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None,
            "last_update_success": coordinator.last_update_success,
        },
        "counts": {
            "tasks": len(tasks),
            "open": sum(1 for t in tasks if logic.is_open(t)),
            "with_due": sum(1 for t in tasks if t.get("due")),
            "zoned_due": sum(1 for t in tasks if (t.get("due") or {}).get("tz")),
            "recurring": sum(1 for t in tasks if t.get("recurrence")),
            "subtasks": sum(1 for t in tasks if t.get("parent_id")),
            "ciphertext_titles": sum(1 for t in tasks if logic.is_ciphertext(t.get("title"))),
            "projects": len(data.projects) if data else 0,
            "archived_projects": len(data.archived_ids()) if data else 0,
            "shared_projects": sum(1 for p in data.projects.values() if p.get("role")) if data else 0,
            "labels": len(data.labels) if data else 0,
            "sections": len(data.sections) if data else 0,
        },
    }
