"""Constants for the OpenTodo integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "opentodo"
MANUFACTURER: Final = "OpenTodo"

CONF_VERIFY_SSL: Final = "verify_ssl"

# Options
CONF_ALL_PROJECTS: Final = "all_projects"
CONF_PROJECTS: Final = "projects"
CONF_TODAY_LIST: Final = "today_list"
CONF_COMPLETED_DAYS: Final = "completed_days"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_QUICK_ADD: Final = "quick_add"
CONF_PUSH: Final = "push"
CONF_HA_URL: Final = "ha_url"

DEFAULT_COMPLETED_DAYS: Final = 1
DEFAULT_SCAN_INTERVAL: Final = 2  # minutes
MIN_SCAN_INTERVAL: Final = 1
MAX_SCAN_INTERVAL: Final = 60

# Entry data kept by the integration itself
DATA_WEBHOOK_ID: Final = "webhook_id"
DATA_REMOTE_WEBHOOK_ID: Final = "remote_webhook_id"
DATA_REMOTE_WEBHOOK_SECRET: Final = "remote_webhook_secret"
DATA_REMOTE_WEBHOOK_URL: Final = "remote_webhook_url"

# Retry creating the OpenTodo webhook this often while it cannot be created.
PUSH_RETRY_INTERVAL: Final = timedelta(hours=1)

INBOX_KEY: Final = "inbox"
TODAY_KEY: Final = "today"

# Token scopes
SCOPE_FULL: Final = "full"
SCOPE_READ: Final = "read"
SCOPE_AGENT: Final = "agent"
SCOPE_ADMIN: Final = "admin"

# Webhook events the integration subscribes to.
WEBHOOK_EVENTS: Final = [
    "task.created",
    "task.updated",
    "task.completed",
    "task.reopened",
    "task.deleted",
    "project.created",
    "project.updated",
    "project.archived",
    "project.deleted",
    "import.completed",
    "import.undone",
]

# Events fired on the Home Assistant bus.
EVENT_TASK_CREATED: Final = "opentodo_task_created"
EVENT_TASK_UPDATED: Final = "opentodo_task_updated"
EVENT_TASK_COMPLETED: Final = "opentodo_task_completed"
EVENT_TASK_REOPENED: Final = "opentodo_task_reopened"
EVENT_TASK_DELETED: Final = "opentodo_task_deleted"
EVENT_TASK_ASSIGNED: Final = "opentodo_task_assigned"
EVENT_TASK_DUE: Final = "opentodo_task_due"

# Repair issue ids (suffixed with the entry id).
ISSUE_PUSH_PRIVATE: Final = "push_private_network"
ISSUE_PUSH_FAILED: Final = "push_failed"
ISSUE_ENCRYPTED: Final = "encrypted_account"

CIPHERTEXT_PREFIX: Final = "enc:v1:"

SERVICE_ADD_TASK: Final = "add_task"

# A task's list order uses gaps of this size, like the web app.
POSITION_GAP: Final = 1000.0

# How long a timed task without an estimate lasts on the calendar.
DEFAULT_EVENT_MINUTES: Final = 30

# Attributes listing tasks are capped at this many entries.
MAX_ATTRIBUTE_TASKS: Final = 20

DOCS_URL: Final = "https://github.com/pixari/OpenTodo/blob/main/docs/home-assistant.md"
