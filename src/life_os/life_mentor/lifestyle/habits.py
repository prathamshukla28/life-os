"""Habits: at most 3 active at once, daily check-ins, streaks and completion rates (LM07).

State is the replay of a hash-chained journal (``lifestyle/habits.jsonl`` in the
private store); nothing is overwritten. Writing requires ``lifestyle`` consent. Adding
a 4th active habit HOLDs (the brief: avoid overwhelming) and is not recorded.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from life_os.core.clock import utc_now_iso
from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.report import finalize, fixed_check
from ..profile import ConsentLedger
from ..security import Journal, PrivateStore, redact

__all__ = ["HABITS_JOURNAL", "MAX_ACTIVE_HABITS", "HabitTracker"]

HABITS_JOURNAL = "lifestyle/habits.jsonl"
MAX_ACTIVE_HABITS = 3
_ID = re.compile(r"[a-z0-9_]{1,40}")


@dataclass
class _Habit:
    habit_id: str
    name: str
    started_on: date
    active: bool = True
    checkins: dict[date, bool] = field(default_factory=dict)


class HabitTracker:
    def __init__(self, store: PrivateStore, *, clock: Callable[[], str] = utc_now_iso) -> None:
        self.consent = ConsentLedger(store, clock=clock)
        self.journal: Journal = store.journal(HABITS_JOURNAL, clock=clock)

    def _state(self) -> dict[str, _Habit]:
        habits: dict[str, _Habit] = {}
        for entry in self.journal.entries():
            ev = entry["event"]
            kind = ev.get("event")
            if kind == "habit_add":
                habits[ev["habit_id"]] = _Habit(
                    ev["habit_id"], ev["name"], date.fromisoformat(ev["started_on"])
                )
            elif kind == "habit_retire" and ev["habit_id"] in habits:
                habits[ev["habit_id"]].active = False
            elif kind == "habit_checkin" and ev["habit_id"] in habits:
                habits[ev["habit_id"]].checkins[date.fromisoformat(ev["day"])] = ev["done"]
        return habits

    def habit_history(self) -> list[dict[str, Any]]:
        """Read-only snapshot of every habit and its check-ins (consent required)."""
        self.consent.require("lifestyle")
        return [
            {
                "habit_id": h.habit_id,
                "name": h.name,
                "started_on": h.started_on,
                "active": h.active,
                "checkins": dict(h.checkins),
            }
            for h in self._state().values()
        ]

    def active_habits(self) -> list[str]:
        self.consent.require("lifestyle")
        return [h for h, v in self._state().items() if v.active]

    def add_habit(self, habit_id: str, name: str, *, started_on: date) -> dict[str, Any]:
        """Add a habit (HOLD and not recorded if 3 are already active)."""
        self.consent.require("lifestyle")
        state = self._state()
        active = [h for h, v in state.items() if v.active]
        checks: list[Check] = []
        if not isinstance(habit_id, str) or not _ID.fullmatch(habit_id):
            checks.append(fixed_check(BLOCK, "habit_id", "habit_id must be 1-40 chars of a-z, 0-9 or _"))
        elif habit_id in state:
            checks.append(
                fixed_check(
                    BLOCK,
                    "habit_id",
                    f"habit {habit_id!r} already exists; history is never overwritten - choose a new id",
                )
            )
        if not isinstance(name, str) or not name.strip():
            checks.append(fixed_check(BLOCK, "name", "habit name must not be empty"))
        if len(active) >= MAX_ACTIVE_HABITS:
            checks.append(
                fixed_check(
                    HOLD,
                    "active_limit",
                    f"{len(active)} habits are already active (limit "
                    f"{MAX_ACTIVE_HABITS}, to avoid overwhelming); retire one "
                    f"first",
                )
            )
        recorded = not checks
        if recorded:
            self.journal.append(
                {
                    "event": "habit_add",
                    "habit_id": habit_id,
                    "name": name,
                    "started_on": started_on.isoformat(),
                }
            )
            active.append(habit_id)
            checks.append(fixed_check(SHIP, "added", f"habit {habit_id} added"))
        return finalize(
            "habit_add",
            inputs=[],
            assumptions=[f"At most {MAX_ACTIVE_HABITS} habits may be active at once (owner policy)."],
            result={"habit_id": redact(str(habit_id)), "recorded": recorded, "active_habits": active},
            checks=checks,
        )

    def retire_habit(self, habit_id: str) -> dict[str, Any]:
        self.consent.require("lifestyle")
        state = self._state()
        if habit_id not in state or not state[habit_id].active:
            checks = [fixed_check(BLOCK, "habit_id", f"no active habit {redact(habit_id)!r}")]
        else:
            self.journal.append({"event": "habit_retire", "habit_id": habit_id})
            checks = [fixed_check(SHIP, "retired", f"habit {habit_id} retired")]
        return finalize("habit_retire", inputs=[], assumptions=[], result=None, checks=checks)

    def check_in(self, habit_id: str, day: date, *, done: bool, as_of: date) -> dict[str, Any]:
        """Record one day's check-in. Unknown habit, out-of-range or duplicate day -> BLOCK."""
        self.consent.require("lifestyle")
        if not isinstance(done, bool):
            raise TypeError("done must be True or False")
        state = self._state()
        habit = state.get(habit_id)
        checks: list[Check] = []
        if habit is None or not habit.active:
            checks.append(fixed_check(BLOCK, "habit_id", f"no active habit {redact(habit_id)!r}"))
        elif day < habit.started_on:
            checks.append(fixed_check(BLOCK, "day", "day is before the habit started"))
        elif day > as_of:
            checks.append(fixed_check(BLOCK, "day", "day is in the future"))
        elif day in habit.checkins:
            checks.append(
                fixed_check(BLOCK, "day", "already checked in for this day; history is never overwritten")
            )
        if not checks:
            self.journal.append(
                {"event": "habit_checkin", "habit_id": habit_id, "day": day.isoformat(), "done": done}
            )
            checks.append(fixed_check(SHIP, "checked_in", f"{habit_id} {day.isoformat()}"))
        return finalize("habit_checkin", inputs=[], assumptions=[], result=None, checks=checks)

    def summary(self, *, as_of: date, window_days: int = 28) -> dict[str, Any]:
        """Per active habit: current streak, days tracked/done, completion % in the window."""
        self.consent.require("lifestyle")
        habits = [h for h in self._state().values() if h.active]
        checks: list[Check] = []
        rows: list[dict[str, Any]] = []
        if not habits:
            checks.append(fixed_check(HOLD, "habits", "no active habits to summarise"))
        for h in habits:
            first = max(h.started_on, as_of - timedelta(days=window_days - 1))
            tracked = (as_of - first).days + 1 if as_of >= first else 0
            done = sum(1 for d, ok in h.checkins.items() if ok and first <= d <= as_of)
            day = as_of if as_of in h.checkins else as_of - timedelta(days=1)
            streak = 0
            while h.checkins.get(day) is True:
                streak += 1
                day -= timedelta(days=1)
            rows.append(
                {
                    "habit_id": h.habit_id,
                    "name": redact(h.name),
                    "started_on": h.started_on.isoformat(),
                    "days_tracked": tracked,
                    "days_done": done,
                    "current_streak_days": streak,
                    "completion_percent": (Decimal(done) * 100 / tracked).quantize(
                        Decimal("0.01"), ROUND_HALF_UP
                    )
                    if tracked
                    else None,
                }
            )
        if rows:
            checks.append(fixed_check(SHIP, "computed", f"{len(rows)} habits summarised"))
        return finalize(
            "habit_summary",
            inputs=[],
            assumptions=[
                "A day with a missing check-in counts as not done.",
                f"Completion covers at most the last {window_days} days, from the habit's start.",
                "If today has no check-in yet, the streak is counted up to yesterday.",
            ],
            result={"habits": rows} if rows else None,
            checks=checks,
        )
