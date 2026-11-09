"""Export the weekly planner to an iCalendar (.ics) file - local, no sync.

Turns the planner's tasks into all-day calendar events you can import into any
calendar app. It writes a file; it never signs in to a calendar or syncs over
the network. The planner stays the source of truth - this is a
one-way, on-demand export.
"""

from __future__ import annotations

from datetime import date, datetime

from life_os.life_mentor.ics import CalendarEvent, build_calendar, make_uid

from .planner import Task
from .week import IsoWeek, day_index

__all__ = ["planner_to_calendar", "tasks_to_events"]

_STATUS_MARK = {"planned": "", "done": "✓ ", "skipped": "⨯ "}


def _task_date(task: Task) -> date:
    week = IsoWeek.parse(task.week)
    return date.fromisocalendar(week.year, week.week, day_index(task.day) + 1)


def tasks_to_events(tasks: list[Task]) -> list[CalendarEvent]:
    """One all-day event per task, on its ISO-week/day date, status in the title."""
    events: list[CalendarEvent] = []
    for task in tasks:
        day = _task_date(task)
        mark = _STATUS_MARK.get(task.status, "")
        events.append(
            CalendarEvent(
                # Stable across moves and status changes (task id + immutable text), so
                # re-importing after `plan move` updates the event instead of leaving a
                # stale copy on the old date.
                uid=make_uid("planner", task.task_id, task.text),
                summary=f"{mark}{task.text}",
                start=day,
                description=f"Weekly Tracker task {task.task_id} ({task.status})",
            )
        )
    return events


def planner_to_calendar(
    tasks: list[Task],
    *,
    cal_name: str = "Weekly Tracker",
    dtstamp: datetime | None = None,
) -> str:
    """Render the given planner *tasks* as a single .ics calendar string."""
    return build_calendar(tasks_to_events(tasks), cal_name=cal_name, dtstamp=dtstamp)
