"""Export the active habit schedule to an iCalendar (.ics) file (LM-G54).

Each active habit becomes a daily-recurring all-day event from the day it
started, so your calendar carries a gentle daily reminder. Local export only: it
writes a file you import yourself, makes no network call, and links no account
(ASUR-LOCAL-01). Reading habits requires ``lifestyle`` consent, which the caller
(the tracker) has already cleared.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..ics import CalendarEvent, build_calendar, make_uid

__all__ = ["habits_to_calendar", "habit_history_to_events"]


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def habit_history_to_events(history: list[dict[str, Any]]) -> list[CalendarEvent]:
    """A daily-recurring all-day event per ACTIVE habit, from its start date.

    Retired habits are left out - the schedule reflects what you are tracking
    now, not what you have stopped (and nothing from history is rewritten).
    """
    events: list[CalendarEvent] = []
    for habit in history:
        if not habit.get("active", False):
            continue
        started = _as_date(habit.get("started_on"))
        habit_id = str(habit.get("habit_id", ""))
        name = str(habit.get("name", habit_id))
        events.append(
            CalendarEvent(
                uid=make_uid("habit", habit_id, started.isoformat()),
                summary=f"Habit: {name}",
                start=started,
                description=f"Daily habit {habit_id} (Life Mentor)",
                rrule="FREQ=DAILY",
            )
        )
    return events


def habits_to_calendar(
    history: list[dict[str, Any]],
    *,
    cal_name: str = "Life Mentor Habits",
    dtstamp: datetime | None = None,
) -> str:
    """Render the active habits in *history* as a single .ics calendar string."""
    return build_calendar(habit_history_to_events(history), cal_name=cal_name, dtstamp=dtstamp)
