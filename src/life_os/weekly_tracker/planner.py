"""Manual weekly planner backed by an append-only, hash-chained event log.

Events (``planner/events.jsonl`` in a private store): ``task_add``, ``task_done``,
``task_skip``, ``task_move``. The current plan is always the replay of that log, so
history is never silently rewritten; a tampered log raises ``JournalTampered`` on read
and refuses appends. ``plans/<week>.json`` is a derived, human-readable snapshot
rewritten after each event (edits to it are ignored).

Storage: explicit path > ``$WEEKLY_TRACKER_HOME`` > ``~/.weekly_tracker``; dirs 0700,
files 0600 (``life_os.life_mentor.security.PrivateStore``). Task text is PII-redacted before it
is stored. Undone tasks are listed as carry-over, never moved automatically.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from life_os.core.clock import utc_now_iso
from life_os.life_mentor.security import Journal, PrivateStore

from .week import DAY_NAMES, IsoWeek, day_index

__all__ = [
    "DEFAULT_HOME_DIRNAME",
    "ENV_HOME",
    "EVENTS_JOURNAL",
    "MAX_TEXT",
    "STATUSES",
    "Planner",
    "Task",
    "resolve_home",
]

DEFAULT_HOME_DIRNAME = ".weekly_tracker"
ENV_HOME = "WEEKLY_TRACKER_HOME"
EVENTS_JOURNAL = "planner/events.jsonl"
STATUSES = ("planned", "done", "skipped")
MAX_TEXT = 200


def resolve_home(
    explicit: str | os.PathLike[str] | None = None, *, env: Mapping[str, str] | None = None
) -> Path:
    """Weekly Tracker home WITHOUT creating it."""
    if explicit is not None:
        return Path(explicit).expanduser()
    environ = os.environ if env is None else env
    value = environ.get(ENV_HOME, "").strip()
    return Path(value).expanduser() if value else Path.home() / DEFAULT_HOME_DIRNAME


@dataclass(frozen=True)
class Task:
    task_id: str
    week: str
    day: str
    text: str
    status: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class Planner:
    def __init__(
        self,
        home: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], str] = utc_now_iso,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.home = resolve_home(home, env=env)
        self.store = PrivateStore(self.home)
        self.journal: Journal = self.store.journal(EVENTS_JOURNAL, clock=clock)

    # -- replay -------------------------------------------------------------------

    def _state(self) -> dict[str, Task]:
        tasks: dict[str, Task] = {}
        for entry in self.journal.entries():
            ev = entry["event"]
            kind, tid = ev.get("event"), ev.get("task_id")
            if kind == "task_add":
                tasks[tid] = Task(tid, ev["week"], ev["day"], ev["text"], "planned")
            elif tid in tasks:
                t = tasks[tid]
                if kind == "task_done":
                    tasks[tid] = Task(t.task_id, t.week, t.day, t.text, "done")
                elif kind == "task_skip":
                    tasks[tid] = Task(t.task_id, t.week, t.day, t.text, "skipped")
                elif kind == "task_move":
                    tasks[tid] = Task(t.task_id, ev["week"], ev["day"], t.text, t.status)
        return tasks

    def _sorted(self, tasks: list[Task]) -> list[Task]:
        return sorted(tasks, key=lambda t: (t.week, DAY_NAMES.index(t.day), int(t.task_id[1:])))

    def tasks(self, week: IsoWeek | None = None) -> list[Task]:
        state = self._state().values()
        return self._sorted([t for t in state if week is None or t.week == str(week)])

    def _snapshot(self, *weeks: str) -> None:
        state = self._state()
        for w in sorted(set(weeks)):
            rows = self._sorted([t for t in state.values() if t.week == w])
            self.store.write_json(
                f"plans/{w}.json",
                {
                    "week": w,
                    "derived_from": EVENTS_JOURNAL,
                    "note": "derived view; edits here are ignored",
                    "tasks": [t.as_dict() for t in rows],
                },
            )

    # -- events -------------------------------------------------------------------

    def add(self, week: IsoWeek, day: str, text: str) -> Task:
        day_name = DAY_NAMES[day_index(day)]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("task text must not be empty")
        if len(text) > MAX_TEXT:
            raise ValueError(f"task text is limited to {MAX_TEXT} characters")
        tid = f"t{sum(1 for _ in self._state()) + 1}"
        self.journal.append(
            {
                "event": "task_add",
                "task_id": tid,
                "week": str(week),
                "day": day_name,
                "text": " ".join(text.split()),
            }
        )
        self._snapshot(str(week))
        return self._state()[tid]

    def _open(self, task_id: str) -> Task:
        task = self._state().get(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id!r}")
        if task.status != "planned":
            raise ValueError(
                f"task {task_id} is already {task.status}; history is append-only - add a new task instead"
            )
        return task

    def done(self, task_id: str) -> Task:
        task = self._open(task_id)
        self.journal.append({"event": "task_done", "task_id": task_id})
        self._snapshot(task.week)
        return self._state()[task_id]

    def skip(self, task_id: str) -> Task:
        task = self._open(task_id)
        self.journal.append({"event": "task_skip", "task_id": task_id})
        self._snapshot(task.week)
        return self._state()[task_id]

    def move(self, task_id: str, week: IsoWeek, day: str) -> Task:
        task = self._open(task_id)
        day_name = DAY_NAMES[day_index(day)]
        if (task.week, task.day) == (str(week), day_name):
            raise ValueError(f"task {task_id} is already on {week} {day_name}")
        self.journal.append({"event": "task_move", "task_id": task_id, "week": str(week), "day": day_name})
        self._snapshot(task.week, str(week))
        return self._state()[task_id]

    # -- summary ------------------------------------------------------------------

    def summary(self, week: IsoWeek) -> dict[str, Any]:
        state = self._state().values()
        this = self._sorted([t for t in state if t.week == str(week)])
        earlier = self._sorted([t for t in state if t.week < str(week) and t.status == "planned"])
        by_day = {d: {"planned": 0, "done": 0, "skipped": 0} for d in DAY_NAMES}
        for t in this:
            by_day[t.day]["planned"] += 1
            if t.status != "planned":
                by_day[t.day][t.status] += 1
        return {
            "planned_total": len(this),
            "done": sum(1 for t in this if t.status == "done"),
            "skipped": sum(1 for t in this if t.status == "skipped"),
            "still_planned": sum(1 for t in this if t.status == "planned"),
            "carry_over": [
                {"task_id": t.task_id, "day": t.day, "text": t.text} for t in this if t.status == "planned"
            ],
            "open_from_earlier": [
                {"task_id": t.task_id, "week": t.week, "day": t.day, "text": t.text} for t in earlier
            ],
            "by_day": by_day,
        }
