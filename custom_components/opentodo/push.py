"""Instant updates: an OpenTodo webhook that calls a Home Assistant webhook.

OpenTodo signs every delivery with the webhook's secret (docs/webhooks.md); the
handler checks that signature and the timestamp, so the Home Assistant webhook
does not need to be limited to the local network. When OpenTodo refuses the
target (the owner's private network switch is off) the integration keeps polling
and raises a repair issue; it tries again every hour.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import time
from typing import Any

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError
from yarl import URL

from .api import OpenTodoApiError, OpenTodoError
from .const import (
    CONF_HA_URL,
    CONF_PUSH,
    DATA_REMOTE_WEBHOOK_ID,
    DATA_REMOTE_WEBHOOK_SECRET,
    DATA_REMOTE_WEBHOOK_URL,
    DATA_WEBHOOK_ID,
    DOCS_URL,
    DOMAIN,
    ISSUE_PUSH_FAILED,
    ISSUE_PUSH_PRIVATE,
    PUSH_RETRY_INTERVAL,
    WEBHOOK_EVENTS,
)
from .coordinator import OpenTodoConfigEntry, OpenTodoCoordinator

_LOGGER = logging.getLogger(__name__)

SIGNATURE_TOLERANCE = 300  # seconds

TASK_EVENTS = {
    "task.created": "created",
    "task.updated": "updated",
    "task.completed": "completed",
    "task.reopened": "reopened",
    "task.deleted": "deleted",
}


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """The `X-OpenTodo-Signature` value for a body (docs/webhooks.md, "Headers and signature")."""
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _is_private_host(url: str) -> bool:
    """Whether a URL's host is an address on a private network (or a bare host name)."""
    host = URL(url).host or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith((".local", ".lan", ".home.arpa", ".internal"))
    return address.is_private or address.is_loopback or address.is_link_local


def push_url(hass: HomeAssistant, entry: OpenTodoConfigEntry, webhook_id: str) -> str:
    """The address OpenTodo should call: the option, else Home Assistant's own URL."""
    if base := (entry.options.get(CONF_HA_URL) or "").strip().rstrip("/"):
        return f"{base}{webhook.async_generate_path(webhook_id)}"
    server_private = _is_private_host(entry.runtime_data.client.url)
    return webhook.async_generate_url(hass, webhook_id, prefer_external=not server_private)


class PushManager:
    """Keep one OpenTodo webhook pointed at this config entry's Home Assistant webhook."""

    def __init__(self, hass: HomeAssistant, entry: OpenTodoConfigEntry, coordinator: OpenTodoCoordinator) -> None:
        """Remember the entry and coordinator."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.webhook_id: str = entry.data.get(DATA_WEBHOOK_ID) or webhook.async_generate_id()
        self._registered = False

    @property
    def wanted(self) -> bool:
        """Push is on in the options and the token may manage webhooks."""
        return bool(self.entry.options.get(CONF_PUSH, True)) and not self.coordinator.read_only

    async def async_setup(self) -> None:
        """Register the local webhook, make sure the remote one exists, and check again every hour."""
        if not self.wanted:
            self._clear_issues()
            return
        if self.entry.data.get(DATA_WEBHOOK_ID) != self.webhook_id:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, DATA_WEBHOOK_ID: self.webhook_id}
            )
        webhook.async_register(
            self.hass, DOMAIN, f"OpenTodo ({self.entry.title})", self.webhook_id, self._handle, allowed_methods=["POST"]
        )
        self._registered = True
        self.entry.async_on_unload(self._unregister)
        await self.async_ensure_remote()
        self.entry.async_on_unload(async_track_time_interval(self.hass, self._retry, PUSH_RETRY_INTERVAL))

    @callback
    def _unregister(self) -> None:
        if self._registered:
            webhook.async_unregister(self.hass, self.webhook_id)
            self._registered = False

    async def _retry(self, _now: datetime) -> None:
        await self.async_ensure_remote()

    async def async_ensure_remote(self) -> None:
        """Create, repair or re-enable the OpenTodo webhook; fall back to polling on failure."""
        try:
            url = push_url(self.hass, self.entry, self.webhook_id)
        except NoURLAvailableError:
            self._fail(ISSUE_PUSH_FAILED, "no_url", "Home Assistant has no URL OpenTodo could call")
            return
        client = self.coordinator.client
        data = self.entry.data
        remote_id = data.get(DATA_REMOTE_WEBHOOK_ID)
        try:
            existing = None
            if remote_id and data.get(DATA_REMOTE_WEBHOOK_SECRET):
                existing = next((w for w in await client.webhooks() if w.get("id") == remote_id), None)
            if existing is None:
                created = await client.create_webhook(url, WEBHOOK_EVENTS)
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={
                        **self.entry.data,
                        DATA_REMOTE_WEBHOOK_ID: created["id"],
                        DATA_REMOTE_WEBHOOK_SECRET: created["secret"],
                        DATA_REMOTE_WEBHOOK_URL: url,
                    },
                )
            else:
                patch: dict[str, Any] = {}
                if existing.get("url") != url:
                    patch["url"] = url
                if sorted(existing.get("events") or []) != sorted(WEBHOOK_EVENTS):
                    patch["events"] = WEBHOOK_EVENTS
                if not existing.get("enabled", True):
                    patch["enabled"] = True
                if patch:
                    await client.update_webhook(existing["id"], patch)
                    self.hass.config_entries.async_update_entry(
                        self.entry, data={**self.entry.data, DATA_REMOTE_WEBHOOK_URL: url}
                    )
        except OpenTodoApiError as err:
            if err.code == "private_target":
                self._fail(ISSUE_PUSH_PRIVATE, err.code, err.message)
            else:
                self._fail(ISSUE_PUSH_FAILED, err.code, err.message)
            return
        except OpenTodoError as err:
            _LOGGER.debug("OpenTodo webhook could not be checked: %s", err)
            self.coordinator.set_push_active(False, str(err))
            return
        self._clear_issues()
        self.coordinator.set_push_active(True)

    def _fail(self, issue: str, code: str, message: str) -> None:
        _LOGGER.info("OpenTodo will be polled; its webhook could not be set up: %s (%s)", message, code)
        self.coordinator.set_push_active(False, code)
        other = ISSUE_PUSH_FAILED if issue == ISSUE_PUSH_PRIVATE else ISSUE_PUSH_PRIVATE
        ir.async_delete_issue(self.hass, DOMAIN, f"{other}_{self.entry.entry_id}")
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{issue}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
            translation_placeholders={
                "title": self.entry.title,
                "message": message,
                "server": self.coordinator.client.url,
            },
            learn_more_url=DOCS_URL,
        )

    def _clear_issues(self) -> None:
        for issue in (ISSUE_PUSH_PRIVATE, ISSUE_PUSH_FAILED):
            ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{self.entry.entry_id}")

    async def _handle(self, hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        """Check a delivery's signature, then apply it and fire the bus events."""
        secret = self.entry.data.get(DATA_REMOTE_WEBHOOK_SECRET)
        body = await request.read()
        timestamp = request.headers.get("X-OpenTodo-Timestamp", "")
        given = request.headers.get("X-OpenTodo-Signature", "")
        if not secret or not timestamp.isdigit() or not hmac.compare_digest(signature(secret, timestamp, body), given):
            return web.Response(status=401, text="bad signature")
        if abs(time.time() - int(timestamp)) > SIGNATURE_TOLERANCE:
            return web.Response(status=401, text="stale timestamp")
        try:
            payload = json.loads(body)
        except ValueError:
            return web.Response(status=400, text="not JSON")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="not an object")
        delivery = str(payload.get("id") or request.headers.get("X-OpenTodo-Delivery", ""))
        if delivery and not self.coordinator.remember_delivery(delivery):
            return web.Response(status=200, text="duplicate")
        self.handle_payload(payload)
        return web.Response(status=200, text="ok")

    @callback
    def handle_payload(self, payload: dict[str, Any]) -> None:
        """Apply one verified delivery."""
        event = payload.get("event")
        data = payload.get("data") or {}
        if event in TASK_EVENTS and isinstance(data.get("task"), dict):
            task = data["task"]
            kind = TASK_EVENTS[event]
            changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else None
            # Apply first, so the event's project name and the entities agree.
            if kind == "deleted":
                self.coordinator.apply_task(None, removed=task.get("id"))
            else:
                self.coordinator.apply_task(task)
            self.coordinator.fire_task_events(
                kind, task, delivery="push", source=payload.get("source"), changes=changes
            )
        elif isinstance(event, str) and event.startswith(("project.", "import.")):
            self.hass.async_create_task(self.coordinator.async_request_refresh())


async def async_remove_remote(hass: HomeAssistant, entry: OpenTodoConfigEntry, client: Any) -> None:
    """Delete the OpenTodo webhook when the config entry is removed (best effort)."""
    remote_id = entry.data.get(DATA_REMOTE_WEBHOOK_ID)
    if not remote_id:
        return
    try:
        await client.delete_webhook(remote_id)
    except OpenTodoError as err:
        _LOGGER.debug("The OpenTodo webhook %s could not be deleted: %s", remote_id, err)
