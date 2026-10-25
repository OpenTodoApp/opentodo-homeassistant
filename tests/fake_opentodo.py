"""An in-memory OpenTodo server behind Home Assistant's aiohttp client mock.

It implements the routes the integration uses with the shapes of docs/api.md:
bearer tokens with scopes, tasks/projects/labels/sections/reminders, the
quick-add parser (a small subset of the grammar), and webhooks with the owner's
private network switch.
"""

from __future__ import annotations

from datetime import date, timedelta
import re
import time
from typing import Any
import uuid

from aiohttp import ClientConnectionError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse
from yarl import URL

BASE = "http://opentodo.test:8080"
TOKEN = "ot_full_token"
READ_TOKEN = "ot_read_token"
AGENT_TOKEN = "ot_agent_token"
ADMIN_TOKEN = "ot_admin_token"
OTHER_ACCOUNT_TOKEN = "ot_other_account"
USER_ID = "7f1c0c9e-0000-4000-8000-000000000001"

PRIORITY = re.compile(r"(?:^|\s)p([1-4])(?=\s|$)")


def now_ms() -> int:
    """The current time in Unix milliseconds."""
    return int(time.time() * 1000)


class FakeOpenTodo:
    """State and request handling of one fake server."""

    def __init__(self, mocker: AiohttpClientMocker) -> None:
        """Start empty, with one account and the usual tokens."""
        self.mocker = mocker
        self.offline = False
        self.not_opentodo = False
        self.encrypted = False
        self.allow_private = False
        self.tokens = {
            TOKEN: ("full", USER_ID),
            READ_TOKEN: ("read", USER_ID),
            AGENT_TOKEN: ("agent", USER_ID),
            ADMIN_TOKEN: ("admin", USER_ID),
            OTHER_ACCOUNT_TOKEN: ("full", "someone-else"),
        }
        self.tasks: dict[str, dict[str, Any]] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self.labels: dict[str, dict[str, Any]] = {}
        self.sections: dict[str, dict[str, Any]] = {}
        self.reminders: dict[str, dict[str, Any]] = {}
        self.webhooks: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.fail_next_write: tuple[int, str, str] | None = None
        for method in ("GET", "POST", "PATCH", "DELETE", "PUT"):
            mocker.request(method, re.compile(rf"^{re.escape(BASE)}/.*"), side_effect=self._handle)

    # ---- data helpers -----------------------------------------------------------

    def add_project(self, name: str, **fields: Any) -> dict[str, Any]:
        """Add a project directly."""
        project = {
            "id": fields.pop("id", str(uuid.uuid4())),
            "name": name,
            "position": float(len(self.projects) + 1) * 1000,
            "created_at": now_ms(),
        }
        project.update(fields)
        self.projects[project["id"]] = project
        return project

    def add_task(self, title: str, **fields: Any) -> dict[str, Any]:
        """Add a task directly."""
        task: dict[str, Any] = {
            "id": fields.pop("id", str(uuid.uuid4())),
            "title": title,
            "description": "",
            "project_id": None,
            "parent_id": None,
            "priority": 4,
            "due": None,
            "labels": [],
            "position": float(len(self.tasks) + 1) * 1000,
            "created_at": now_ms(),
            "completed_at": None,
            "deleted_at": None,
            "fields": {},
            "origin": None,
        }
        task.update(fields)
        self.tasks[task["id"]] = task
        return task

    def add_label(self, name: str) -> dict[str, Any]:
        """Add a label directly."""
        label = {
            "id": str(uuid.uuid4()),
            "name": name,
            "color": "#f97316",
            "position": now_ms(),
            "created_at": now_ms(),
        }
        self.labels[label["id"]] = label
        return label

    # ---- request handling ---------------------------------------------------------

    def _auth(self) -> tuple[str, str] | None:
        headers = self.mocker.mock_calls[-1][3] or {}
        value = headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            return None
        return self.tokens.get(value[len("Bearer ") :])

    @staticmethod
    def _json(method: str, url: URL, status: int, body: Any = None) -> AiohttpClientMockResponse:
        if body is None:
            return AiohttpClientMockResponse(method, url, status=status, text="")
        return AiohttpClientMockResponse(method, url, status=status, json=body)

    def _error(
        self, method: str, url: URL, status: int, code: str, message: str = "refused"
    ) -> AiohttpClientMockResponse:
        return self._json(method, url, status, {"error": {"code": code, "message": message}})

    async def _handle(self, method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        method = method.upper()
        self.requests.append((method, url.path, data))
        if self.offline:
            return AiohttpClientMockResponse(method, url, exc=ClientConnectionError("Connection refused"))
        if self.not_opentodo:
            return AiohttpClientMockResponse(method, url, status=200, text="<html>router</html>")
        path = url.path
        if path == "/healthz":
            return self._json(method, url, 200, {"status": "ok", "version": "v0.12.0"})
        auth = self._auth()
        if auth is None:
            return self._error(method, url, 401, "unauthorized", "sign in required")
        scope, user_id = auth
        if path == "/api/v1/me":
            return self._json(method, url, 200, self._me(scope, user_id))
        writes = method not in ("GET", "HEAD")
        if writes and self.fail_next_write:
            status, code, message = self.fail_next_write
            self.fail_next_write = None
            return self._error(method, url, status, code, message)
        if writes and scope in ("read", "agent") and path != "/api/v1/quick-add/parse":
            return self._error(method, url, 403, "insufficient_scope", "this token may not change anything")
        parts = path.removeprefix("/api/v1/").split("/")
        name = parts[0]
        if name == "quick-add" and method == "POST":
            return self._json(method, url, 200, self._parse(data["line"], data.get("today")))
        if name == "webhooks":
            return self._webhooks(method, url, parts, data)
        store = {
            "tasks": self.tasks,
            "projects": self.projects,
            "labels": self.labels,
            "sections": self.sections,
            "reminders": self.reminders,
        }.get(name)
        if store is None:
            return self._error(method, url, 404, "not_found", "unknown collection")
        if len(parts) == 1:
            if method == "GET":
                return self._json(
                    method, url, 200, {name: [dict(v) for v in store.values() if not v.get("deleted_at")]}
                )
            if method == "POST":
                return self._create(method, url, name, store, data)
        item_id = parts[1]
        item = store.get(item_id)
        if item is None or item.get("deleted_at"):
            return self._error(method, url, 404, "not_found", "not found")
        if method == "GET":
            return self._json(method, url, 200, dict(item))
        if method == "PATCH":
            return self._patch(method, url, name, item, data)
        if method == "DELETE":
            item["deleted_at"] = now_ms()
            return self._json(method, url, 204)
        return self._error(method, url, 405, "method_not_allowed")

    def _me(self, scope: str, user_id: str) -> dict[str, Any]:
        return {
            "user": {"id": user_id, "email": "olivia@example.com", "name": "Olivia", "role": "owner", "createdAt": 1},
            "auth": {"method": "token", "scope": scope, "token_id": "tok-1", "token_name": "Home Assistant"},
            "encryption": {"enabled": self.encrypted, "keyVersion": 1 if self.encrypted else 0},
            "mfa": {"enrolled": False},
            "agent_excluded_projects": [],
        }

    def _create(
        self, method: str, url: URL, name: str, store: dict[str, Any], data: dict[str, Any]
    ) -> AiohttpClientMockResponse:
        if name == "tasks":
            if not data.get("title"):
                return self._error(method, url, 400, "invalid_input", "task.title is required")
            project = data.get("project_id")
            if project is not None and project not in self.projects:
                return self._error(method, url, 400, "invalid_input", "task.project_id references unknown project")
            fields = dict(data)
            title = fields.pop("title")
            item = self.add_task(title, position=float(now_ms()), **fields)
        elif name == "labels":
            item = self.add_label(data["name"])
        elif name == "projects":
            item = self.add_project(data["name"])
        elif name == "reminders":
            item = {"id": str(uuid.uuid4()), "created_at": now_ms(), "deleted_at": None, **data}
            store[item["id"]] = item
        else:
            item = {"id": str(uuid.uuid4()), **data}
            store[item["id"]] = item
        return self._json(method, url, 201, dict(item))

    def _patch(
        self, method: str, url: URL, name: str, item: dict[str, Any], data: dict[str, Any]
    ) -> AiohttpClientMockResponse:
        if name == "tasks":
            if data.get("completed_at") and item.get("recurrence") and not item.get("completed_at"):
                # A repeating task moves on and records a completed occurrence.
                occurrence = dict(
                    item, id=str(uuid.uuid4()), series_id=item["id"], recurrence=None, completed_at=data["completed_at"]
                )
                self.tasks[occurrence["id"]] = occurrence
                next_day = date.fromisoformat(item["due"]["date"]) + timedelta(days=1)
                item["due"] = {**item["due"], "date": next_day.isoformat()}
                data = {k: v for k, v in data.items() if k != "completed_at"}
            if "completed_at" in data:
                item["completed_by"] = USER_ID if data["completed_at"] else None
        item.update(data)
        return self._json(method, url, 200, dict(item))

    def _webhooks(self, method: str, url: URL, parts: list[str], data: Any) -> AiohttpClientMockResponse:
        if len(parts) == 1 and method == "GET":
            return self._json(
                method, url, 200, {"webhooks": [self._public_hook(w) for w in self.webhooks.values()], "events": []}
            )
        if len(parts) == 1 and method == "POST":
            host = URL(data["url"]).host or ""
            if not self.allow_private and (
                host.startswith(("192.168.", "10.", "127."))
                or host in ("localhost", "homeassistant.local")
                or "." not in host
            ):
                return self._error(
                    method,
                    url,
                    400,
                    "private_target",
                    "url: the target resolves to a private, loopback or link-local address",
                )
            hook = {
                "id": str(uuid.uuid4()),
                "url": data["url"],
                "events": sorted(data["events"]),
                "projectId": None,
                "enabled": data.get("enabled", True),
                "disabledReason": None,
                "consecutiveFailures": 0,
                "createdAt": now_ms(),
                "updatedAt": now_ms(),
                "secret": f"whsec_{uuid.uuid4().hex}",
            }
            self.webhooks[hook["id"]] = hook
            return self._json(method, url, 201, {"webhook": dict(hook)})
        hook = self.webhooks.get(parts[1]) if len(parts) > 1 else None
        if hook is None:
            return self._error(method, url, 404, "not_found", "no such webhook")
        if method == "PATCH":
            hook.update(data)
            if data.get("enabled"):
                hook["disabledReason"] = None
            return self._json(method, url, 200, {"webhook": self._public_hook(hook)})
        if method == "DELETE":
            del self.webhooks[hook["id"]]
            return self._json(method, url, 204)
        return self._json(method, url, 200, {"webhook": self._public_hook(hook)})

    @staticmethod
    def _public_hook(hook: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in hook.items() if k != "secret"}

    def _parse(self, line: str, today: str | None) -> dict[str, Any]:
        """A small part of the quick-add grammar: today, tomorrow, #project, @label, p1-p4."""
        day = date.fromisoformat(today) if today else date.today()
        fields: dict[str, Any] = {}
        words = []
        for word in line.split():
            lower = word.lower()
            if lower == "today":
                fields["due"] = {"date": day.isoformat()}
            elif lower == "tomorrow":
                fields["due"] = {"date": (day + timedelta(days=1)).isoformat()}
            elif word.startswith("#") and len(word) > 1:
                match = next((p for p in self.projects.values() if p["name"].lower() == word[1:].lower()), None)
                if match:
                    fields["project_id"] = match["id"]
                else:
                    words.append(word)
            elif word.startswith("@") and len(word) > 1:
                label = next((lb for lb in self.labels.values() if lb["name"].lower() == word[1:].lower()), None)
                fields.setdefault("labels", []).append(
                    {"id": label["id"], "name": label["name"]} if label else {"name": word[1:]}
                )
            elif PRIORITY.fullmatch(f" {lower}"):
                fields["priority"] = int(lower[1])
            elif lower == "!1h":
                fields.setdefault("reminders", []).append({"kind": "relative", "offset_minutes": -60})
            else:
                words.append(word)
        return {"title": " ".join(words), "fields": fields, "chips": [], "unmatched": [], "origin": None}
