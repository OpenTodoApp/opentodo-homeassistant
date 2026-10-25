"""Pure functions over OpenTodo tasks: lists, counts, order and dates.

They follow the web app's definitions (web/src/lib/inbox.ts, views.ts, dayLists.ts
and ordering.ts in OpenTodo), so a count in Home Assistant matches the app's.
The Inbox rule is pinned by the app's own vectors (tests/fixtures/inbox.vectors.json).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import CIPHERTEXT_PREFIX, POSITION_GAP

Task = Mapping[str, Any]

MAX_DEPTH = 64


def is_ciphertext(value: Any) -> bool:
    """True for an end-to-end encrypted value the server cannot read."""
    return isinstance(value, str) and value.startswith(CIPHERTEXT_PREFIX)


def is_open(task: Task) -> bool:
    """Open and live."""
    return not task.get("completed_at") and not task.get("deleted_at")


# ---- dates -------------------------------------------------------------------


def due_moment(due: Mapping[str, Any] | None, zone: tzinfo) -> date | datetime | None:
    """A task's due value as Home Assistant shows it: a date, or an aware datetime in `zone`.

    A floating time is read in `zone`; a zoned one (`tz`) is converted to it.
    """
    if not due or not isinstance(due, Mapping) or not due.get("date"):
        return None
    try:
        day = date.fromisoformat(due["date"])
    except (TypeError, ValueError):
        return None
    clock = due.get("time")
    if not clock:
        return day
    try:
        hour, minute = (int(part) for part in str(clock).split(":")[:2])
        at = time(hour, minute)
    except ValueError:
        return day
    source: tzinfo = zone
    if due.get("tz"):
        try:
            source = ZoneInfo(due["tz"])
        except (ZoneInfoNotFoundError, ValueError):
            source = zone
    return datetime.combine(day, at, tzinfo=source).astimezone(zone)


def local_due_date(due: Mapping[str, Any] | None, zone: tzinfo) -> str | None:
    """The due's calendar date in `zone`, as `YYYY-MM-DD`."""
    moment = due_moment(due, zone)
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return moment.date().isoformat()
    return moment.isoformat()


def due_from_ha(value: date | datetime | None, zone: tzinfo) -> dict[str, str] | None:
    """A Home Assistant date or datetime as an OpenTodo floating due value in `zone`."""
    if value is None:
        return None
    if isinstance(value, datetime):
        local = value.astimezone(zone) if value.tzinfo else value
        return {"date": local.date().isoformat(), "time": local.strftime("%H:%M")}
    return {"date": value.isoformat()}


def same_due(a: date | datetime | None, b: date | datetime | None) -> bool:
    """Whether two Home Assistant due values name the same minute (or day)."""
    if a is None or b is None:
        return a is b
    if isinstance(a, datetime) != isinstance(b, datetime):
        return False
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a.replace(second=0, microsecond=0) == b.replace(second=0, microsecond=0)
    return a == b


# ---- membership (web/src/lib/inbox.ts and views.ts) --------------------------


def is_scheduled(task: Task) -> bool:
    """A do-on date, a start date or Someday."""
    return bool(task.get("due")) or bool(task.get("start"))


def is_placed(task: Task) -> bool:
    """A project or a schedule."""
    return task.get("project_id") is not None or is_scheduled(task)


def is_inbox_task(task: Task, parent_of: Callable[[str], Task | None] | None = None) -> bool:
    """In the Inbox: open, live, not placed, and no parent placed either."""
    if task.get("completed_at") or task.get("deleted_at") or is_placed(task):
        return False
    parent = task.get("parent_id")
    depth = 0
    while parent and parent_of and depth < MAX_DEPTH:
        found = parent_of(parent)
        if found is None:
            break
        if is_placed(found):
            return False
        parent = found.get("parent_id")
        depth += 1
    return True


def is_someday(task: Task) -> bool:
    """Parked in Someday."""
    return task.get("start") == "someday"


def is_deferred(task: Task, today: str) -> bool:
    """A start date still ahead."""
    start = task.get("start")
    return isinstance(start, str) and start != "someday" and start > today


def is_started(task: Task, today: str) -> bool:
    """A start date that has come."""
    start = task.get("start")
    return isinstance(start, str) and start != "someday" and start <= today


def is_active(task: Task, today: str) -> bool:
    """Neither parked nor deferred."""
    return not is_someday(task) and not is_deferred(task, today)


def _hard_date(task: Task, today: str, zone: tzinfo) -> bool:
    due = local_due_date(task.get("due"), zone)
    deadline = task.get("deadline")
    return (due is not None and due <= today) or (deadline is not None and deadline <= today)


def in_today(task: Task, today: str, zone: tzinfo) -> bool:
    """Today: open and due today or earlier, a deadline reached, or a start date come."""
    if task.get("completed_at") or task.get("deleted_at"):
        return False
    return _hard_date(task, today, zone) or is_started(task, today)


def is_earlier(task: Task, today: str, zone: tzinfo) -> bool:
    """Today's Earlier group: due before today, or a deadline passed and not planned for today."""
    due = local_due_date(task.get("due"), zone)
    deadline = task.get("deadline")
    return (due is not None and due < today) or (deadline is not None and deadline < today and due != today)


def counts_open(task: Task, today: str) -> bool:
    """Counted in the sidebar (Inbox, projects): open and active."""
    return is_open(task) and is_active(task, today)


def without_archived[T: Mapping[str, Any]](tasks: Iterable[T], archived: set[str]) -> list[T]:
    """Leave out the tasks of archived projects, as the app's cross-project views do."""
    return [t for t in tasks if not t.get("project_id") or t["project_id"] not in archived]


def blocked_ids(tasks: Mapping[str, Task]) -> set[str]:
    """Open tasks that wait for another open task (web/src/lib/dependencies.ts)."""
    blocked: set[str] = set()
    for task in tasks.values():
        if not is_open(task):
            continue
        for other in task.get("blocked_by") or ():
            dep = tasks.get(other)
            if dep is not None and is_open(dep):
                blocked.add(task["id"])
                break
    return blocked


# ---- order (web/src/lib/ordering.ts) -----------------------------------------


def position_key(task: Task) -> tuple[float, str]:
    """Manual order: ascending position, ties by id."""
    position = task.get("position")
    return (float(position) if isinstance(position, (int, float)) else 0.0, str(task.get("id", "")))


def due_key(task: Task, zone: tzinfo) -> tuple[int, str, str]:
    """Due order: undated last; on one day, untimed after timed."""
    moment = due_moment(task.get("due"), zone)
    if moment is None:
        return (1, "", "")
    if isinstance(moment, datetime):
        return (0, moment.date().isoformat(), moment.strftime("%H:%M"))
    return (0, moment.isoformat(), "99:99")


def due_then_priority_key(task: Task, zone: tzinfo) -> tuple[Any, ...]:
    """Today's order: due, then priority, then the manual order."""
    priority = task.get("priority")
    return (*due_key(task, zone), priority if isinstance(priority, int) else 4, *position_key(task))


def tree_order[T: Mapping[str, Any]](
    tasks: list[T], sections: Mapping[str, Mapping[str, Any]] | None = None
) -> list[T]:
    """A project list's order: no section first, then sections in order; subtasks after their parent."""
    ids = {t["id"] for t in tasks}
    children: dict[str, list[T]] = {}
    roots: list[T] = []
    for task in tasks:
        parent = task.get("parent_id")
        if parent and parent in ids and parent != task["id"]:
            children.setdefault(parent, []).append(task)
        else:
            roots.append(task)

    def section_key(task: Task) -> tuple[int, float, str]:
        section = (sections or {}).get(task.get("section_id") or "")
        if section is None:
            return (0, 0.0, "")
        position = section.get("position")
        return (1, float(position) if isinstance(position, (int, float)) else 0.0, section.get("id", ""))

    roots.sort(key=lambda t: (*section_key(t), *position_key(t)))
    out: list[T] = []
    seen: set[str] = set()

    def walk(task: T, depth: int) -> None:
        if task["id"] in seen or depth > MAX_DEPTH:
            return
        seen.add(task["id"])
        out.append(task)
        for child in sorted(children.get(task["id"], []), key=position_key):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    # Anything left over (a cycle in synced data) still shows, at the end.
    out.extend(t for t in tasks if t["id"] not in seen)
    return out


def position_between(before: float | None, after: float | None) -> float | None:
    """The midpoint between two neighbours, GAP beyond a single one; None when they leave no room."""
    if before is None and after is None:
        return None
    if before is None:
        return float(after) - POSITION_GAP  # type: ignore[arg-type]
    if after is None:
        return float(before) + POSITION_GAP
    if not after > before:
        return None
    mid = before + (after - before) / 2
    if not before < mid < after:
        return None
    return mid
