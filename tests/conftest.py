"""Fixtures: a fake OpenTodo server and a set-up config entry."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.opentodo.const import CONF_PUSH, CONF_VERIFY_SSL, DOMAIN

from .fake_opentodo import BASE, TOKEN, USER_ID, FakeOpenTodo

FROZEN = "2026-10-25 10:00:00+01:00"
# 08:00 in Rome on the frozen day, in Unix milliseconds.
DONE_AT = 1792911600000


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture(autouse=True)
async def time_zone(hass: HomeAssistant) -> None:
    """Run in a fixed zone, so dates are predictable."""
    await hass.config.async_set_time_zone("Europe/Rome")


async def tick(hass: HomeAssistant, freezer: Any, minutes: float) -> None:
    """Move the clock and run what became due."""
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture
def fake(aioclient_mock: AiohttpClientMocker) -> FakeOpenTodo:
    """The fake server."""
    return FakeOpenTodo(aioclient_mock)


@pytest.fixture
def options() -> dict[str, Any]:
    """Entry options; push off unless a test turns it on."""
    return {CONF_PUSH: False}


@pytest.fixture
def config_entry(options: dict[str, Any]) -> MockConfigEntry:
    """A config entry for the fake server."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Olivia (opentodo.test)",
        unique_id=USER_ID,
        data={CONF_URL: BASE, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
        options=options,
    )


@pytest.fixture
def seeded(fake: FakeOpenTodo) -> FakeOpenTodo:
    """A small account: a Home project, an Inbox task, tasks for today and earlier."""
    home = fake.add_project("Home", id="home")
    fake.add_project("Work", id="work")
    fake.add_project("Old", id="old", archived=True)
    fake.add_task("Inbox thing", id="t-inbox")
    fake.add_task("Water plants", id="t-today", project_id=home["id"], due={"date": "2026-10-25"}, priority=2)
    fake.add_task("Call plumber", id="t-late", project_id=home["id"], due={"date": "2026-10-20"})
    fake.add_task(
        "Pay rent",
        id="t-timed",
        project_id=home["id"],
        due={"date": "2026-10-25", "time": "18:30"},
        estimate_minutes=15,
    )
    fake.add_task("Later", id="t-later", project_id=home["id"], start="someday")
    fake.add_task("Report", id="t-work", project_id="work", due={"date": "2026-10-28"})
    fake.add_task("Archived task", id="t-arch", project_id="old", due={"date": "2026-10-20"})
    fake.add_task("Done this morning", id="t-done", project_id=home["id"], completed_at=DONE_AT)
    return fake


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: Any, seeded: FakeOpenTodo
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the entry at a fixed time."""
    freezer.move_to(FROZEN)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
