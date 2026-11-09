"""Read-only Life Mentor adapter for one ISO week.

Reads the Life Mentor private store (metric log, habits journal, decision journal) and
never writes to it - only journal *reads* are performed, and a missing store directory is
reported, not created. Consent is respected per category: without consent the section
says "not shown: no consent for <category>" and the values are never read. A journal
that fails its integrity check is reported as an error, never shown.

No week-to-week comparison is made, so no causal wording is needed; values are shown as
recorded.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from life_os.life_mentor.decision_engine import DECISION_JOURNAL
from life_os.life_mentor.lifestyle import HabitTracker
from life_os.life_mentor.profile import ConsentLedger
from life_os.life_mentor.security import JournalTampered, PrivateStore, redact
from life_os.life_mentor.tracking import METRICS, MetricLog

from ..week import IsoWeek

__all__ = ["ADDITIVE_METRICS", "DECISIONS_CATEGORY", "life_mentor_section"]

ADDITIVE_METRICS = ("exercise_minutes", "income", "expenses")
DECISIONS_CATEGORY = "finance"
_Q = Decimal("0.01")


def _hidden(category: str) -> str:
    return f"not shown: no consent for {category}"


def _metrics(
    store: PrivateStore, consent: dict[str, bool], week: IsoWeek, hidden: set[str], errors: list[str]
) -> dict[str, Any]:
    log = MetricLog(store)
    out: dict[str, Any] = {}
    for metric, (category, unit, _neg) in METRICS.items():
        if not consent.get(category, False):
            hidden.add(_hidden(category))
            continue
        try:
            series = log.series(metric)
        except JournalTampered:
            errors.append("metrics: the metric log failed its integrity check; not shown")
            return {}
        points = [(d, v) for d, v in series if week.contains(d)]
        if not points:
            continue
        values = [v for _, v in points]
        out[metric] = {
            "unit": unit,
            "by_day": {d.isoformat(): v for d, v in points},
            "points": len(points),
            "average": (sum(values, Decimal(0)) / len(values)).quantize(_Q, ROUND_HALF_UP),
            "total": sum(values, Decimal(0)) if metric in ADDITIVE_METRICS else None,
        }
    return out


def _habits(store: PrivateStore, week: IsoWeek, errors: list[str]) -> dict[str, Any] | None:
    try:
        history = HabitTracker(store).habit_history()
    except JournalTampered:
        errors.append("habits: the habits journal failed its integrity check; not shown")
        return None
    rows: list[dict[str, Any]] = []
    done = checked = 0
    for h in history:
        if not h["active"]:
            continue  # retired habits are not shown (WT-G9)
        started: date = h["started_on"]
        if started > week.end:
            continue
        by_day: dict[str, str] = {}
        for d in week.days():
            if d < started:
                by_day[d.isoformat()] = "not started"
            elif d in h["checkins"]:
                ok = h["checkins"][d]
                by_day[d.isoformat()] = "done" if ok else "missed"
                checked += 1
                done += int(ok)
            else:
                by_day[d.isoformat()] = "no check-in"
        rows.append({"habit_id": h["habit_id"], "name": redact(h["name"]), "by_day": by_day})
    return {"habits": rows, "done": done, "checked_in": checked}


def _local_day(recorded_at: str, tz: tzinfo | None) -> date:
    """UTC journal timestamp -> calendar day in *tz* (default: this machine's local time)."""
    stamp = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(tz).date()


def _decisions(
    store: PrivateStore, week: IsoWeek, errors: list[str], tz: tzinfo | None = None
) -> dict[str, Any] | None:
    try:
        entries = store.journal(DECISION_JOURNAL).entries()
    except JournalTampered:
        errors.append("decisions: the decision journal failed its integrity check; not shown")
        return None
    analysed: list[dict[str, Any]] = []
    confirmed = 0
    for e in entries:
        try:
            day = _local_day(str(e.get("recorded_at", "")), tz)
        except ValueError:
            errors.append("decisions: an entry has an unreadable timestamp; skipped")
            continue
        if not week.contains(day):
            continue
        ev = e["event"]
        if ev.get("event") == "decision_analyzed":
            analysed.append(
                {
                    "date": day.isoformat(),
                    "recommendation_status": ev.get("recommendation_status"),
                    "overall_disposition": ev.get("overall_disposition"),
                }
            )
        elif ev.get("event") == "decision_confirmed_by_human":
            confirmed += 1
    return {"analysed": len(analysed), "confirmed": confirmed, "items": analysed}


def life_mentor_section(
    home: str | os.PathLike[str], week: IsoWeek, *, tz: str | None = None
) -> dict[str, Any]:
    """Life Mentor data for *week* from the store at *home* (read-only).

    Decision timestamps are bucketed in *tz* (the same zone given to the git source) or,
    by default, this machine's local time.
    """
    root = Path(home)
    if not root.is_dir():
        return {"status": "unavailable", "reason": "no Life Mentor data directory"}
    store = PrivateStore(root)
    try:
        consent = ConsentLedger(store).status()
    except JournalTampered:
        return {"status": "unavailable", "reason": "Life Mentor consent journal failed its integrity check"}
    hidden: set[str] = set()
    errors: list[str] = []
    metrics = _metrics(store, consent, week, hidden, errors)
    habits = None
    if consent.get("lifestyle", False):
        habits = _habits(store, week, errors)
    else:
        hidden.add(_hidden("lifestyle"))
    decisions = None
    if consent.get(DECISIONS_CATEGORY, False):
        zone: tzinfo | None = None
        if tz is not None:
            try:
                zone = ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                errors.append(f"decisions: unknown time zone {tz!r}; local time used")
        decisions = _decisions(store, week, errors, zone)
    else:
        hidden.add(_hidden(DECISIONS_CATEGORY))
    return {
        "status": "ok",
        "reason": None,
        "hidden": sorted(hidden),
        "errors": errors,
        "metrics": metrics,
        "habits": habits,
        "decisions": decisions,
    }
