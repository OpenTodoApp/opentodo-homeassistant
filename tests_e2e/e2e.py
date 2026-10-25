"""End-to-end test: a real OpenTodo container and a real Home Assistant container.

Run from the repository root (needs Docker and aiohttp):

    python tests_e2e/e2e.py

It starts both containers on a private Docker network, installs the integration
into Home Assistant, sets it up through the config flow REST API, and checks
both directions: tasks added and completed in OpenTodo reach Home Assistant at
once (webhook) with `opentodo_task_*` events; tasks added and completed in Home
Assistant (to-do actions, the add_task action and Assist) reach OpenTodo. It
then turns the owner's private network switch off and checks the repair issue.
Everything it starts is removed at the end unless E2E_KEEP=1.

Environment: E2E_HA_IMAGE, E2E_OT_IMAGE, E2E_PREFIX (container names),
E2E_HA_PORT, E2E_OT_PORT (published on 127.0.0.1).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

import aiohttp

HA_IMAGE = os.environ.get("E2E_HA_IMAGE", "ghcr.io/home-assistant/home-assistant:2026.10.4")
OT_IMAGE = os.environ.get("E2E_OT_IMAGE", "ghcr.io/pixari/opentodo:0.12.0")
PREFIX = os.environ.get("E2E_PREFIX", "st-ha-a-e2e")
HA_PORT = int(os.environ.get("E2E_HA_PORT", "19202"))
OT_PORT = int(os.environ.get("E2E_OT_PORT", "19201"))
KEEP = os.environ.get("E2E_KEEP") == "1"

NET = f"{PREFIX}-net"
OT = f"{PREFIX}-ot"
HA = f"{PREFIX}-ha"
HA_VOLUME = f"{PREFIX}-ha-config"
OT_VOLUME = f"{PREFIX}-ot-data"
HA_URL = f"http://127.0.0.1:{HA_PORT}"
OT_URL = f"http://127.0.0.1:{OT_PORT}"
CLIENT_ID = f"{HA_URL}/"
ROOT = Path(__file__).resolve().parent.parent

CONFIGURATION = f"""
homeassistant:
  name: E2E
  time_zone: Europe/Rome
  country: IT
  unit_system: metric
  internal_url: http://{HA}:8123
http:
  server_port: 8123
default_config:
logger:
  default: warning
  logs:
    custom_components.opentodo: debug
"""


def sh(*args: str, check: bool = True) -> str:
    """Run a command and return its output."""
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def step(message: str) -> None:
    """Print a progress line."""
    print(f"[e2e] {message}", flush=True)


def cleanup() -> None:
    """Remove every container, volume and network this run created."""
    for name in (HA, OT):
        sh("docker", "rm", "-f", name, check=False)
    for volume in (HA_VOLUME, OT_VOLUME):
        sh("docker", "volume", "rm", "-f", volume, check=False)
    sh("docker", "network", "rm", NET, check=False)


def start_containers() -> None:
    """OpenTodo with a fresh data volume; Home Assistant with the integration copied in."""
    cleanup()
    sh("docker", "network", "create", NET)
    sh("docker", "volume", "create", OT_VOLUME)
    sh("docker", "volume", "create", HA_VOLUME)
    sh(
        "docker", "run", "-d", "--name", OT, "--network", NET, "-p", f"127.0.0.1:{OT_PORT}:8080",
        "-v", f"{OT_VOLUME}:/data", OT_IMAGE,
    )
    sh(
        "docker", "create", "--name", HA, "--network", NET, "-p", f"127.0.0.1:{HA_PORT}:8123",
        "-e", "TZ=Europe/Rome", "-v", f"{HA_VOLUME}:/config", HA_IMAGE,
    )
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config"
        (config / "custom_components").mkdir(parents=True)
        (config / "configuration.yaml").write_text(CONFIGURATION)
        sh("cp", "-r", str(ROOT / "custom_components" / "opentodo"), str(config / "custom_components" / "opentodo"))
        sh("docker", "cp", f"{config}/.", f"{HA}:/config/")
    sh("docker", "start", HA)


async def wait_for(session: aiohttp.ClientSession, url: str, timeout: float = 300) -> None:
    """Wait until a URL answers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError):
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status < 500:
                    return
        await asyncio.sleep(2)
    raise TimeoutError(f"{url} did not answer")


async def eventually(check: Any, what: str, timeout: float = 30) -> Any:
    """Retry an async check until it returns something truthy."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


class OpenTodo:
    """The OpenTodo side: an owner account and a full token."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.token = ""
        self.cookies = aiohttp.CookieJar(unsafe=True)
        self.owner = aiohttp.ClientSession(cookie_jar=self.cookies)

    async def setup(self) -> None:
        body = {"email": "owner@example.com", "name": "Olivia", "password": "correct-horse-battery"}
        async with self.owner.post(f"{OT_URL}/api/v1/auth/register", json=body) as resp:
            assert resp.status == 201, await resp.text()
        async with self.owner.post(f"{OT_URL}/api/v1/tokens", json={"name": "Home Assistant", "scope": "full"}) as resp:
            assert resp.status == 201, await resp.text()
            self.token = (await resp.json())["secret"]
        await self.allow_private(True)

    async def allow_private(self, allow: bool) -> None:
        async with self.owner.put(f"{OT_URL}/api/v1/settings/webhooks", json={"allowPrivate": allow}) as resp:
            assert resp.status == 200, await resp.text()

    async def api(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with self.session.request(method, f"{OT_URL}/api/v1{path}", json=body, headers=headers) as resp:
            text = await resp.text()
            assert resp.status < 400, f"{method} {path}: {resp.status} {text}"
            return json.loads(text) if text else None

    async def task(self, title: str) -> dict[str, Any] | None:
        tasks = (await self.api("GET", "/tasks"))["tasks"]
        return next((t for t in tasks if t["title"] == title), None)


class HomeAssistant:
    """The Home Assistant side: onboarding, REST and one websocket for events."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.token = ""
        self.events: list[dict[str, Any]] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_id = 0
        self._ws_waiters: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None

    async def onboard(self) -> None:
        body = {"client_id": CLIENT_ID, "name": "E2E", "username": "e2e", "password": "e2e-password-123", "language": "en"}
        async with self.session.post(f"{HA_URL}/api/onboarding/users", json=body) as resp:
            assert resp.status == 200, await resp.text()
            code = (await resp.json())["auth_code"]
        form = {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}
        async with self.session.post(f"{HA_URL}/auth/token", data=form) as resp:
            assert resp.status == 200, await resp.text()
            self.token = (await resp.json())["access_token"]

    async def rest(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with self.session.request(method, f"{HA_URL}{path}", json=body, headers=headers) as resp:
            text = await resp.text()
            assert resp.status < 400, f"{method} {path}: {resp.status} {text}"
            return json.loads(text) if text else None

    async def connect(self) -> None:
        self._ws = await self.session.ws_connect(f"{HA_URL}/api/websocket")
        assert (await self._ws.receive_json())["type"] == "auth_required"
        await self._ws.send_json({"type": "auth", "access_token": self.token})
        assert (await self._ws.receive_json())["type"] == "auth_ok"
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self._ws is not None
        async for message in self._ws:
            data = json.loads(message.data)
            if data.get("type") == "event" and "event_type" in data.get("event", {}):
                self.events.append(data["event"])
            elif data.get("id") in self._ws_waiters and data.get("type") == "result":
                self._ws_waiters.pop(data["id"]).set_result(data)

    async def ws(self, message: dict[str, Any]) -> Any:
        assert self._ws is not None
        self._ws_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._ws_waiters[self._ws_id] = future
        await self._ws.send_json({**message, "id": self._ws_id})
        result = await asyncio.wait_for(future, 30)
        assert result["success"], result
        return result.get("result")

    async def state(self, entity_id: str) -> dict[str, Any] | None:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with self.session.get(f"{HA_URL}/api/states/{entity_id}", headers=headers) as resp:
            return await resp.json() if resp.status == 200 else None

    async def items(self, entity_id: str) -> list[dict[str, Any]]:
        result = await self.rest("POST", "/api/services/todo/get_items?return_response", {"entity_id": entity_id})
        return result["service_response"][entity_id]["items"]

    def fired(self, event_type: str, title: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event_type"] == event_type and e["data"].get("title") == title]


async def run() -> None:
    async with aiohttp.ClientSession() as session:
        step(f"starting {OT_IMAGE} and {HA_IMAGE}")
        start_containers()
        await wait_for(session, f"{OT_URL}/healthz")
        await wait_for(session, f"{HA_URL}/api/onboarding", timeout=600)
        version = sh("docker", "exec", HA, "python", "-c", "from homeassistant.const import __version__; print(__version__)")
        step(f"Home Assistant {version} is up")

        ot = OpenTodo(session)
        await ot.setup()
        home = await ot.api("POST", "/projects", {"name": "Home"})
        await ot.api("POST", "/tasks", {"title": "Water plants", "project_id": home["id"], "due": {"date": date.today().isoformat()}})
        step("OpenTodo owner, full token, Home project and private network targets ready")

        ha = HomeAssistant(session)
        await ha.onboard()
        flow = await ha.rest("POST", "/api/config/config_entries/flow", {"handler": "opentodo"})
        assert flow["type"] == "form" and flow["step_id"] == "user", flow
        bad = await ha.rest(
            "POST", f"/api/config/config_entries/flow/{flow['flow_id']}",
            {"url": f"http://{OT}:8080", "token": "ot_wrong", "verify_ssl": True},
        )
        assert bad["errors"] == {"base": "invalid_auth"}, bad
        result = await ha.rest(
            "POST", f"/api/config/config_entries/flow/{flow['flow_id']}",
            {"url": f"http://{OT}:8080", "token": ot.token, "verify_ssl": True},
        )
        assert result["type"] == "create_entry", result
        entry_id = result["result"]["entry_id"]
        step(f"config entry created: {result['title']}")

        await ha.connect()
        for event_type in (
            "opentodo_task_created", "opentodo_task_completed", "opentodo_task_updated",
            "opentodo_task_reopened", "opentodo_task_deleted", "opentodo_task_assigned",
        ):
            await ha.ws({"type": "subscribe_events", "event_type": event_type})

        async def entity(entity_id: str) -> dict[str, Any] | None:
            state = await ha.state(entity_id)
            return state if state and state["state"] not in ("unavailable", "unknown") else None

        home_list = await eventually(lambda: entity("todo.home"), "todo.home")
        assert home_list["state"] == "1", home_list
        states = await ha.rest("GET", "/api/states")
        ours = {s["entity_id"] for s in states if s["entity_id"].split(".")[1].startswith("olivia_")}
        prefix = next(e for e in ours if e.endswith("_today") and e.startswith("sensor.")).removesuffix("_today")
        today = await eventually(lambda: entity(f"{prefix}_today"), "the Today sensor")
        assert today["state"] == "1", today
        for suffix in ("_earlier", "_inbox", "_home"):
            assert await entity(f"{prefix}{suffix}") is not None, suffix
        assert await entity(prefix.replace("sensor.", "calendar.") + "_tasks") is not None
        hooks = (await ot.api("GET", "/webhooks"))["webhooks"]
        assert len(hooks) == 1 and hooks[0]["url"].startswith(f"http://{HA}:8123/api/webhook/"), hooks
        step("entities present; OpenTodo webhook points at Home Assistant")

        # OpenTodo -> Home Assistant, pushed.
        started = time.monotonic()
        await ot.api("POST", "/tasks", {"title": "Buy batteries", "project_id": home["id"]})
        await eventually(lambda: _async(ha.fired("opentodo_task_created", "Buy batteries")), "the created event", 10)
        await eventually(
            lambda: _items_contain(ha, "todo.home", "Buy batteries", "needs_action"), "Buy batteries in todo.home", 10
        )
        step(f"task created in OpenTodo reached Home Assistant in {time.monotonic() - started:.1f}s (push)")

        batteries = await ot.task("Buy batteries")
        await ot.api("PATCH", f"/tasks/{batteries['id']}", {"completed_at": int(time.time() * 1000)})
        await eventually(lambda: _async(ha.fired("opentodo_task_completed", "Buy batteries")), "the completed event", 10)
        await eventually(lambda: _items_contain(ha, "todo.home", "Buy batteries", "completed"), "batteries completed", 10)
        step("task completed in OpenTodo: completed event and item in Home Assistant")

        # Home Assistant -> OpenTodo.
        await ha.rest("POST", "/api/services/todo/update_item", {"entity_id": "todo.home", "item": "Water plants", "status": "completed"})
        plants = await ot.task("Water plants")
        assert plants["completed_at"], plants
        await eventually(lambda: _async(ha.fired("opentodo_task_completed", "Water plants")), "the completed event (from HA)", 10)
        step("task completed in Home Assistant: completed in OpenTodo, event back through the webhook")

        await ha.rest("POST", "/api/services/todo/add_item", {"entity_id": "todo.inbox", "item": "milk tomorrow #Home p1"})
        milk = await ot.task("milk")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        assert milk and milk["project_id"] == home["id"] and milk["priority"] == 1 and milk["due"] == {"date": tomorrow}, milk
        step("todo.add_item with quick-add syntax: 'milk', Home, p1, due tomorrow")

        response = await ha.rest(
            "POST", "/api/services/opentodo/add_task?return_response",
            {"text": "Descale the kettle @kitchen", "project": "Home", "priority": 2},
        )
        added = response["service_response"]["task"]
        assert added["title"] == "Descale the kettle" and added["labels"] == ["kitchen"], added
        step("opentodo.add_task returned the created task")

        speech = await ha.rest("POST", "/api/conversation/process", {"text": "add bread to the Home list", "language": "en"})
        assert speech["response"]["response_type"] == "action_done", speech
        assert await ot.task("Bread"), "Assist did not add Bread"
        speech = await ha.rest("POST", "/api/conversation/process", {"text": "check off bread from the Home list", "language": "en"})
        assert speech["response"]["response_type"] == "action_done", speech
        assert (await ot.task("Bread"))["completed_at"]
        step("Assist: 'add bread to the Home list' and 'check off bread from the Home list'")

        # Owner switches private network targets off: polling and a repair issue.
        await ot.allow_private(False)
        for hook in (await ot.api("GET", "/webhooks"))["webhooks"]:
            await ot.api("DELETE", f"/webhooks/{hook['id']}")
        await ha.rest("POST", f"/api/config/config_entries/entry/{entry_id}/reload")

        async def issue() -> Any:
            issues = (await ha.ws({"type": "repairs/list_issues"}))["issues"]
            return [i for i in issues if i["domain"] == "opentodo" and i["issue_id"].startswith("push_private_network")]

        await eventually(issue, "the private network repair issue", 30)
        step("private targets off: repair issue raised, polling instead")

        await ot.owner.close()
        if KEEP:
            step("E2E_KEEP=1: containers left running")
        print(json.dumps({"home_assistant": version, "opentodo": OT_IMAGE, "result": "passed"}))


async def _async(value: Any) -> Any:
    return value


async def _items_contain(ha: HomeAssistant, entity_id: str, summary: str, status: str) -> bool:
    return any(i["summary"] == summary and i["status"] == status for i in await ha.items(entity_id))


def main() -> int:
    try:
        asyncio.run(run())
    except BaseException:
        print(sh("docker", "logs", "--tail", "80", HA, check=False), file=sys.stderr)
        raise
    finally:
        if not KEEP:
            cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
