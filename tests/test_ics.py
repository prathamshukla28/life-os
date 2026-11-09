"""Tests for the stdlib iCalendar writer and its two exporters (WT-G8, LM-G54).

Covers RFC 5545 structure, escaping/folding, deterministic output, and the two
exporters (weekly planner tasks, active habit schedule). Local only - these
tests assert the writer builds a *string*; nothing here touches the network.
"""

from datetime import date, datetime, timezone

from life_os.life_mentor.ics import CalendarEvent, build_calendar, make_uid
from life_os.life_mentor.lifestyle.habit_ics import habits_to_calendar
from life_os.weekly_tracker.ics import planner_to_calendar
from life_os.weekly_tracker.planner import Task

FIXED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# --- generic writer --------------------------------------------------------


def test_calendar_has_required_envelope():
    ics = build_calendar([], cal_name="X", dtstamp=FIXED)
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.rstrip().endswith("END:VCALENDAR")
    assert "VERSION:2.0" in ics
    assert "PRODID:" in ics
    assert ics.endswith("\r\n")


def test_event_emits_all_day_vevent():
    ev = CalendarEvent(uid="u1@life-os.local", summary="Ship it", start=date(2026, 10, 12))
    ics = build_calendar([ev], cal_name="Cal", dtstamp=FIXED)
    assert "BEGIN:VEVENT\r\n" in ics
    assert "UID:u1@life-os.local" in ics
    assert "DTSTAMP:20260102T030405Z" in ics
    assert "DTSTART;VALUE=DATE:20261012" in ics
    assert "SUMMARY:Ship it" in ics
    assert "END:VEVENT" in ics


def test_text_is_escaped_per_rfc5545():
    ev = CalendarEvent(
        uid="u@life-os.local",
        summary="pay rent; buy milk, eggs\\now",
        start=date(2026, 1, 1),
    )
    ics = build_calendar([ev], cal_name="C", dtstamp=FIXED)
    line = next(ln for ln in ics.split("\r\n") if ln.startswith("SUMMARY:"))
    assert "\\;" in line and "\\," in line and "\\\\" in line


def test_long_line_is_folded_to_75_octets():
    ev = CalendarEvent(uid="u@life-os.local", summary="A" * 200, start=date(2026, 1, 1))
    ics = build_calendar([ev], cal_name="C", dtstamp=FIXED)
    for line in ics.split("\r\n"):
        # Continuation lines begin with a space; no content line exceeds 75 octets.
        assert len(line.encode("utf-8")) <= 75


def test_rrule_is_emitted_for_recurring_events():
    ev = CalendarEvent(uid="u@life-os.local", summary="Meditate", start=date(2026, 1, 1), rrule="FREQ=DAILY")
    ics = build_calendar([ev], cal_name="C", dtstamp=FIXED)
    assert "RRULE:FREQ=DAILY" in ics


def test_output_is_deterministic_for_a_fixed_dtstamp():
    ev = CalendarEvent(uid="u@life-os.local", summary="x", start=date(2026, 1, 1))
    a = build_calendar([ev], cal_name="C", dtstamp=FIXED)
    b = build_calendar([ev], cal_name="C", dtstamp=FIXED)
    assert a == b


def test_make_uid_is_stable_and_content_derived():
    assert make_uid("a", "b") == make_uid("a", "b")
    assert make_uid("a", "b") != make_uid("a", "c")
    assert make_uid("a").endswith("@life-os.local")


# --- planner exporter (WT-G8) ---------------------------------------------


def test_planner_export_places_task_on_its_iso_date():
    # 2026-W42, Monday -> a concrete calendar date via ISO week rules.
    task = Task(task_id="t1", week="2026-W42", day="mon", text="demo day", status="planned")
    ics = planner_to_calendar([task], dtstamp=FIXED)
    expected = date.fromisocalendar(2026, 42, 1).strftime("%Y%m%d")
    assert f"DTSTART;VALUE=DATE:{expected}" in ics
    assert "SUMMARY:demo day" in ics


def test_planner_export_marks_status():
    done = Task(task_id="t1", week="2026-W42", day="tue", text="done thing", status="done")
    ics = planner_to_calendar([done], dtstamp=FIXED)
    summary = next(ln for ln in ics.split("\r\n") if ln.startswith("SUMMARY:"))
    assert "done thing" in summary
    assert "(done)" in ics  # status recorded in the description


# --- habit exporter (LM-G54) ----------------------------------------------


def test_habit_export_is_daily_recurring_from_start():
    history = [
        {
            "habit_id": "water",
            "name": "Drink water",
            "started_on": date(2026, 3, 1),
            "active": True,
            "checkins": {},
        },
    ]
    ics = habits_to_calendar(history, dtstamp=FIXED)
    assert "DTSTART;VALUE=DATE:20260301" in ics
    assert "RRULE:FREQ=DAILY" in ics
    assert "SUMMARY:Habit: Drink water" in ics


def test_habit_export_omits_retired_habits():
    history = [
        {
            "habit_id": "old",
            "name": "Retired one",
            "started_on": date(2026, 1, 1),
            "active": False,
            "checkins": {},
        },
        {
            "habit_id": "new",
            "name": "Active one",
            "started_on": date(2026, 2, 1),
            "active": True,
            "checkins": {},
        },
    ]
    ics = habits_to_calendar(history, dtstamp=FIXED)
    assert "Active one" in ics
    assert "Retired one" not in ics


def test_habit_export_accepts_iso_string_start():
    history = [
        {"habit_id": "read", "name": "Read", "started_on": "2026-04-05", "active": True, "checkins": {}},
    ]
    ics = habits_to_calendar(history, dtstamp=FIXED)
    assert "DTSTART;VALUE=DATE:20260405" in ics
