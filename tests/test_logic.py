"""The pure task rules, pinned to the app where it has shared vectors."""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.opentodo import logic
from custom_components.opentodo.api import normalize_url

ROME = ZoneInfo("Europe/Rome")
VECTORS = json.loads((Path(__file__).parent / "fixtures" / "inbox.vectors.json").read_text())


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_inbox_vectors(vector: dict[str, Any]) -> None:
    """The Inbox rule matches the app's vectors (web/src/lib/inbox.vectors.json in OpenTodo)."""
    tasks: dict[str, dict[str, Any]] = {}
    task = dict(vector["task"], id="t")
    parent_id = None
    for index, parent in enumerate(vector.get("parents", [])):
        pid = f"p{index}"
        tasks[pid] = dict(parent, id=pid)
        if parent_id:
            tasks[parent_id]["parent_id"] = pid
        else:
            task["parent_id"] = pid
        parent_id = pid
    if vector.get("parent_missing"):
        task["parent_id"] = "missing"
    tasks["t"] = task
    assert logic.is_inbox_task(task, tasks.get) is vector["inbox"]


def test_due_moment() -> None:
    """Floating dates and times are read in the zone; zoned ones are converted."""
    assert logic.due_moment({"date": "2026-10-25"}, ROME) == date(2026, 10, 25)
    assert logic.due_moment({"date": "2026-10-25", "time": "18:30"}, ROME) == datetime(
        2026, 10, 25, 18, 30, tzinfo=ROME
    )
    tokyo = logic.due_moment({"date": "2026-10-26", "time": "17:00", "tz": "Asia/Tokyo"}, ROME)
    assert tokyo.isoformat() == "2026-10-26T09:00:00+01:00"
    assert logic.due_moment({"date": "2026-10-26", "time": "17:00", "tz": "Nowhere/Land"}, ROME).hour == 17
    assert logic.due_moment(None, ROME) is None
    assert logic.due_moment({"date": "nope"}, ROME) is None
    assert logic.due_moment({"date": "2026-10-25", "time": "xx"}, ROME) == date(2026, 10, 25)
    assert logic.local_due_date({"date": "2026-10-26", "time": "07:00", "tz": "Asia/Tokyo"}, ROME) == "2026-10-25"
    assert logic.local_due_date(None, ROME) is None


def test_due_from_ha() -> None:
    """Home Assistant values become floating due values in the zone."""
    assert logic.due_from_ha(None, ROME) is None
    assert logic.due_from_ha(date(2026, 10, 27), ROME) == {"date": "2026-10-27"}
    utc = datetime.fromisoformat("2026-10-27T08:15:00+00:00")
    assert logic.due_from_ha(utc, ROME) == {"date": "2026-10-27", "time": "09:15"}
    assert logic.due_from_ha(datetime(2026, 10, 27, 9, 15), ROME) == {"date": "2026-10-27", "time": "09:15"}


def test_same_due() -> None:
    """Two values name the same minute or day."""
    a = datetime(2026, 10, 27, 9, 15, 30, tzinfo=ROME)
    assert logic.same_due(a, a.replace(second=0))
    assert not logic.same_due(a, date(2026, 10, 27))
    assert logic.same_due(None, None)
    assert not logic.same_due(None, date(2026, 10, 27))
    assert logic.same_due(date(2026, 10, 27), date(2026, 10, 27))


def test_position_between() -> None:
    """The midpoint, a gap beyond one neighbour, or no room."""
    assert logic.position_between(None, None) is None
    assert logic.position_between(None, 5000.0) == 4000.0
    assert logic.position_between(5000.0, None) == 6000.0
    assert logic.position_between(1000.0, 2000.0) == 1500.0
    assert logic.position_between(7.0, 7.0) is None
    assert logic.position_between(1.0, 1.0 + 1e-16) is None


def test_today_and_earlier() -> None:
    """Started tasks are in Today; Someday and deferred ones are not active."""
    today = "2026-10-25"
    assert logic.in_today({"start": "2026-10-20"}, today, ROME)
    assert not logic.in_today({"start": "someday"}, today, ROME)
    assert not logic.in_today({"due": {"date": "2026-10-20"}, "completed_at": 1}, today, ROME)
    assert logic.in_today({"deadline": "2026-10-25", "start": "someday"}, today, ROME)
    assert not logic.is_active({"start": "2026-11-01"}, today)
    assert logic.is_deferred({"start": "2026-11-01"}, today)
    assert logic.counts_open({"start": None}, today)
    assert logic.is_earlier({"deadline": "2026-10-24"}, today, ROME)
    assert not logic.is_earlier({"deadline": "2026-10-24", "due": {"date": "2026-10-25"}}, today, ROME)


def test_tree_order_survives_a_cycle() -> None:
    """Tasks that are each other's parent still show."""
    tasks = [{"id": "a", "parent_id": "b", "position": 1}, {"id": "b", "parent_id": "a", "position": 2}]
    assert sorted(t["id"] for t in logic.tree_order(tasks)) == ["a", "b"]


def test_ciphertext() -> None:
    """Encrypted values are recognised."""
    assert logic.is_ciphertext("enc:v1:abc")
    assert not logic.is_ciphertext("milk")
    assert not logic.is_ciphertext(None)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("todo.example.com", "http://todo.example.com"),
        ("https://todo.example.com/", "https://todo.example.com"),
        ("http://10.0.0.2:8080/api/v1", "http://10.0.0.2:8080"),
        ("https://example.com/todo/?x=1#y", "https://example.com/todo"),
    ],
)
def test_normalize_url(given: str, expected: str) -> None:
    """Addresses are stored without a trailing slash or the API path."""
    assert normalize_url(given) == expected
