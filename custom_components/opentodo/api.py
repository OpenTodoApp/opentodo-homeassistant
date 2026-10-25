"""A small client for the OpenTodo HTTP API (docs/api.md in OpenTodo)."""

from __future__ import annotations

from http import HTTPStatus
import json
from typing import Any
import uuid

import aiohttp
from yarl import URL

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class OpenTodoError(Exception):
    """Base error of the client."""


class OpenTodoConnectionError(OpenTodoError):
    """The server could not be reached or answered something that is not OpenTodo."""


class OpenTodoNotOpenTodoError(OpenTodoConnectionError):
    """The address answers, but not like an OpenTodo server."""


class OpenTodoAuthError(OpenTodoError):
    """The token is missing, wrong or revoked (401)."""


class OpenTodoApiError(OpenTodoError):
    """The server refused a request with its error envelope."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Keep the status, the error code and the server's message."""
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def normalize_url(url: str) -> str:
    """Return the server's base URL without a trailing slash or API path."""
    url = url.strip()
    if "://" not in url:
        url = f"http://{url}"
    parsed = URL(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v1"):
        path = path[: -len("/api/v1")]
    return str(parsed.with_path(path or "/").with_query(None).with_fragment(None)).rstrip("/")


class OpenTodoClient:
    """Talk to one OpenTodo server with one bearer token."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str, *, verify_ssl: bool = True) -> None:
        """Store the session, base URL and token."""
        self._session = session
        self.url = normalize_url(url)
        self._token = token
        self._verify_ssl = verify_ssl

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, str] | None = None,
        auth: bool = True,
        idempotency_key: str | None = None,
    ) -> Any:
        """Send one request and return the decoded JSON (None for an empty answer)."""
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        kwargs: dict[str, Any] = {"headers": headers, "timeout": REQUEST_TIMEOUT}
        if body is not None:
            kwargs["json"] = body
        if params:
            kwargs["params"] = params
        if not self._verify_ssl:
            kwargs["ssl"] = False
        try:
            async with self._session.request(method, f"{self.url}{path}", **kwargs) as resp:
                text = await resp.text()
                status = resp.status
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise OpenTodoConnectionError(str(err) or type(err).__name__) from err
        data: Any = None
        if text:
            try:
                data = json.loads(text)
            except ValueError as err:
                raise OpenTodoNotOpenTodoError(f"{status}: not a JSON answer") from err
        if status < 400:
            return data
        code, message = "error", text[:200]
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            code = str(data["error"].get("code", code))
            message = str(data["error"].get("message", message))
        if status == HTTPStatus.UNAUTHORIZED:
            raise OpenTodoAuthError(message)
        raise OpenTodoApiError(status, code, message)

    # Server and account

    async def health(self) -> dict[str, Any]:
        """Return `/healthz` ({status, version}); raises when it is not OpenTodo."""
        try:
            data = await self._request("GET", "/healthz", auth=False)
        except OpenTodoApiError as err:
            raise OpenTodoNotOpenTodoError(str(err)) from err
        if not isinstance(data, dict) or data.get("status") != "ok" or "version" not in data:
            raise OpenTodoNotOpenTodoError("unexpected /healthz answer")
        return data

    async def me(self) -> dict[str, Any]:
        """Return the account and the credential in use."""
        return await self._request("GET", "/api/v1/me")

    # Collections

    async def _list(self, name: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/v1/{name}")
        items = data.get(name) if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    async def tasks(self) -> list[dict[str, Any]]:
        """Every live task the account can read (open and completed)."""
        return await self._list("tasks")

    async def projects(self) -> list[dict[str, Any]]:
        """Every live project the account can read."""
        return await self._list("projects")

    async def labels(self) -> list[dict[str, Any]]:
        """Every live label."""
        return await self._list("labels")

    async def sections(self) -> list[dict[str, Any]]:
        """Every live section."""
        return await self._list("sections")

    async def create_task(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Create a task; one idempotency key per call so a retry cannot duplicate it."""
        return await self._request("POST", "/api/v1/tasks", body=fields, idempotency_key=str(uuid.uuid4()))

    async def update_task(self, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Change some fields of a task."""
        return await self._request("PATCH", f"/api/v1/tasks/{task_id}", body=fields)

    async def delete_task(self, task_id: str) -> None:
        """Move a task to the Trash."""
        await self._request("DELETE", f"/api/v1/tasks/{task_id}")

    async def create_label(self, name: str) -> dict[str, Any]:
        """Create a label."""
        return await self._request("POST", "/api/v1/labels", body={"name": name}, idempotency_key=str(uuid.uuid4()))

    async def create_reminder(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Create a reminder for a task."""
        return await self._request("POST", "/api/v1/reminders", body=fields, idempotency_key=str(uuid.uuid4()))

    async def parse_quick_add(self, line: str, today: str, now: str) -> dict[str, Any]:
        """Read a quick-add line with the account's language, projects and labels."""
        return await self._request("POST", "/api/v1/quick-add/parse", body={"line": line, "today": today, "now": now})

    # Webhooks

    async def webhooks(self) -> list[dict[str, Any]]:
        """The account's webhooks."""
        return await self._list("webhooks")

    async def create_webhook(self, url: str, events: list[str]) -> dict[str, Any]:
        """Create a webhook; the answer holds the secret, shown only this once."""
        data = await self._request("POST", "/api/v1/webhooks", body={"url": url, "events": events, "enabled": True})
        return data["webhook"]

    async def update_webhook(self, webhook_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Change a webhook."""
        data = await self._request("PATCH", f"/api/v1/webhooks/{webhook_id}", body=fields)
        return data["webhook"]

    async def delete_webhook(self, webhook_id: str) -> None:
        """Delete a webhook and its delivery log."""
        await self._request("DELETE", f"/api/v1/webhooks/{webhook_id}")
