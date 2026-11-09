"""Collect the three sources for one ISO week and render deterministic Markdown.

``collect`` gathers a plain model dict (planner, git, Life Mentor); ``render`` turns it into
Markdown: header, Mon-Sun table, Planner / Work (git) / Life Mentor sections, and a 3-6
line factual Summary (counts only - no ratings, no motivational claims). The same model and
``generated_on`` always give the same text. A final ``redact`` pass guarantees no raw PII.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

from life_os.life_mentor.finance.money import format_inr
from life_os.life_mentor.security import JournalTampered, assert_report_clean, redact
from life_os.life_mentor.tracking import METRICS

from .planner import Planner
from .sources.git_work import git_section
from .sources.life_mentor import life_mentor_section
from .week import DAY_LABELS, DAY_NAMES, IsoWeek

__all__ = ["collect", "render"]

DASH = "—"
_MARK = {"done": "[x]", "planned": "[ ]", "skipped": "[-]"}
_SHORT = {
    "sleep_hours": ("sleep", "h"),
    "exercise_minutes": ("exercise", "min"),
    "weight_kg": ("weight", "kg"),
}


def _not_requested() -> dict[str, Any]:
    return {"status": "not_requested", "reason": "not requested"}


def collect(
    week: IsoWeek,
    *,
    generated_on: date | str,
    planner: Planner | None,
    life_mentor_home: str | os.PathLike[str] | None,
    repo: str | os.PathLike[str] | None,
    git: bool = True,
    git_tz: str | None = None,
) -> dict[str, Any]:
    """Gather all sources for *week* (read-only)."""
    plan: dict[str, Any] = _not_requested()
    if planner is not None:
        try:
            plan = {
                "status": "ok",
                "reason": None,
                "tasks": [t.as_dict() for t in planner.tasks(week)],
                "summary": planner.summary(week),
            }
        except JournalTampered:
            plan = {"status": "unavailable", "reason": "planner log failed its integrity check"}
    lm = (
        _not_requested()
        if life_mentor_home is None
        else life_mentor_section(life_mentor_home, week, tz=git_tz)
    )
    work = git_section(repo, week, tz=git_tz) if git else _not_requested()
    return {
        "week": str(week),
        "start": week.start.isoformat(),
        "end": week.end.isoformat(),
        "generated_on": str(generated_on),
        "days": [d.isoformat() for d in week.days()],
        "planner": plan,
        "git": work,
        "life_mentor": lm,
    }


# -- helpers ----------------------------------------------------------------------------


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _num(value: Decimal | int | None, unit: str = "") -> str:
    if value is None:
        return DASH
    if unit == "INR":
        return format_inr(Decimal(value))
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _ok(section: dict[str, Any]) -> bool:
    return section.get("status") == "ok"


def _status_line(section: dict[str, Any]) -> str:
    if section.get("status") == "not_requested":
        return "not requested"
    return f"unavailable: {section.get('reason')}"


def _metric_order(metrics: dict[str, Any]) -> list[str]:
    order = list(METRICS)
    return sorted(metrics, key=lambda m: (order.index(m) if m in order else len(order), m))


def _day_label(day_name: str) -> str:
    return DAY_LABELS[DAY_NAMES.index(day_name)]


# -- table --------------------------------------------------------------------------------


def _table(model: dict[str, Any]) -> list[str]:
    plan, work, lm = model["planner"], model["git"], model["life_mentor"]
    lines = [
        "| Day | Date | Tasks done/total | Skipped | Commits | Key metrics |",
        "|---|---|---|---|---|---|",
    ]
    for i, day in enumerate(model["days"]):
        if _ok(plan):
            tasks = [t for t in plan["tasks"] if t["day"] == DAY_NAMES[i]]
            done = sum(t["status"] == "done" for t in tasks)
            done_total = f"{done}/{len(tasks)}"
            skipped = str(sum(t["status"] == "skipped" for t in tasks))
        else:
            done_total = skipped = DASH
        commits = str(len(work["by_day"].get(day, []))) if _ok(work) else DASH
        lines.append(
            f"| {DAY_LABELS[i]} | {day} | {done_total} | {skipped} | {commits} | "
            f"{_cell(_key_metrics(lm, day))} |"
        )
    return lines


def _key_metrics(lm: dict[str, Any], day: str) -> str:
    if not _ok(lm):
        return DASH
    parts: list[str] = []
    metrics = lm.get("metrics", {})
    for metric in _metric_order(metrics):
        if metric in _SHORT and day in metrics[metric]["by_day"]:
            label, unit = _SHORT[metric]
            parts.append(f"{label} {_num(metrics[metric]['by_day'][day])} {unit}")
    habits = lm.get("habits")
    if habits:
        recorded = [m for m in (h["by_day"].get(day) for h in habits["habits"]) if m in ("done", "missed")]
        if recorded:
            parts.append(f"habits {recorded.count('done')}/{len(recorded)} done")
    return "; ".join(parts) if parts else DASH


# -- sections -------------------------------------------------------------------------------


def _planner_section(model: dict[str, Any]) -> list[str]:
    plan = model["planner"]
    lines = ["## Planner", ""]
    if not _ok(plan):
        return [*lines, _status_line(plan)]
    if not plan["tasks"]:
        lines.append("No tasks planned this week.")
    for t in plan["tasks"]:
        i = DAY_NAMES.index(t["day"])
        tail = " (skipped)" if t["status"] == "skipped" else ""
        lines.append(
            f"- {DAY_LABELS[i]} {model['days'][i]}: {_MARK[t['status']]} "
            f"{t['task_id']} {_cell(t['text'])}{tail}"
        )
    s = plan["summary"]
    carry = (
        "; ".join(f"{c['task_id']} {_cell(c['text'])} ({_day_label(c['day'])})" for c in s["carry_over"])
        or "none"
    )
    earlier = (
        "; ".join(
            f"{c['task_id']} {_cell(c['text'])} ({c['week']} {_day_label(c['day'])})"
            for c in s["open_from_earlier"]
        )
        or "none"
    )
    return [
        *lines,
        "",
        f"Carry-over (still planned; listed only, not moved): {carry}.",
        f"Open from earlier weeks: {earlier}.",
    ]


def _git_section(model: dict[str, Any]) -> list[str]:
    work = model["git"]
    lines = ["## Work (git)", ""]
    if not _ok(work):
        return [*lines, _status_line(work)]
    lines += [f"Repository: {_cell(work['repo_name'])}", ""]
    if not work["by_day"]:
        lines.append("No commits in this week.")
    for i, day in enumerate(model["days"]):
        for c in work["by_day"].get(day, []):
            lines.append(
                f"- {DAY_LABELS[i]} {day}: {c['hash']} {_cell(c['subject'])} "
                f"({_plural(c['files'], 'file')}, +{c['insertions']}/-{c['deletions']})"
            )
    t = work["totals"]
    return [
        *lines,
        "",
        (
            f"Totals: {_plural(t['commits'], 'commit')} on {t['active_days']} of 7 days; "
            f"{_plural(t['files_changed'], 'file')} changed; +{t['insertions']} / "
            f"-{t['deletions']} lines; {_plural(t['test_files_touched'], 'test file')} touched."
        ),
    ]


def _lm_section(model: dict[str, Any]) -> list[str]:
    lm = model["life_mentor"]
    lines = ["## Life Mentor", ""]
    if not _ok(lm):
        return [*lines, _status_line(lm)]
    metrics = lm.get("metrics", {})
    lines += ["Values as recorded in Life Mentor; no week-to-week comparison is made.", ""]
    if metrics:
        lines.append("| Metric | Unit | " + " | ".join(DAY_LABELS) + " | Days | Average | Total |")
        lines.append("|" + "---|" * 12)
        for m in _metric_order(metrics):
            row = metrics[m]
            cells = [_num(row["by_day"].get(d), row["unit"]) for d in model["days"]]
            lines.append(
                f"| {m.replace('_', ' ')} | {row['unit']} | "
                + " | ".join(cells)
                + f" | {row['points']} | {_num(row['average'], row['unit'])} | "
                f"{_num(row['total'], row['unit'])} |"
            )
    else:
        lines.append("No metric values recorded this week (or none that may be shown).")
    habits = lm.get("habits")
    if habits is not None:
        lines += ["", "Habits:"]
        if not habits["habits"]:
            lines.append("- no habits")
        for h in habits["habits"]:
            marks = ", ".join(
                f"{DAY_LABELS[i]} {h['by_day'].get(d, DASH)}" for i, d in enumerate(model["days"])
            )
            lines.append(f"- {_cell(h['name'])}: {marks}")
        lines += [
            "",
            (f"Habit check-ins this week: {habits['done']} done of {habits['checked_in']} recorded."),
        ]
    decisions = lm.get("decisions")
    if decisions is not None:
        lines += [
            "",
            (f"Decisions: {decisions['analysed']} analysed, {decisions['confirmed']} confirmed by you."),
        ]
    notes = [*lm.get("hidden", []), *(f"error: {e}" for e in lm.get("errors", []))]
    if notes:
        lines += ["", *(f"- {n}" for n in notes)]
    return lines


# -- summary ---------------------------------------------------------------------------------


def _lm_days(lm: dict[str, Any]) -> int:
    days: set[str] = set()
    for row in lm.get("metrics", {}).values():
        days |= set(row["by_day"])
    for h in (lm.get("habits") or {}).get("habits", []):
        days |= {d for d, m in h["by_day"].items() if m in ("done", "missed")}
    return len(days)


def _summary(model: dict[str, Any]) -> list[str]:
    plan, work, lm = model["planner"], model["git"], model["life_mentor"]
    bullets: list[str] = []
    if _ok(plan):
        s = plan["summary"]
        bullets.append(
            f"Planner: {_plural(s['planned_total'], 'task')} planned, {s['done']} "
            f"done, {s['skipped']} skipped, {s['still_planned']} still planned "
            f"(carry-over listed, not moved)."
        )
    else:
        bullets.append(f"Planner: {_status_line(plan)}.")
    if _ok(work):
        t = work["totals"]
        bullets.append(
            f"Work (git): {_plural(t['commits'], 'commit')} on {t['active_days']} of "
            f"7 days; {_plural(t['files_changed'], 'file')} changed, "
            f"+{t['insertions']} / -{t['deletions']} lines; "
            f"{_plural(t['test_files_touched'], 'test file')} touched."
        )
    else:
        bullets.append(f"Work (git): {_status_line(work)}.")
    if _ok(lm):
        text = f"Life Mentor: values recorded on {_lm_days(lm)} of 7 days"
        if lm.get("habits") is not None:
            text += f"; habit check-ins {lm['habits']['done']} done of {lm['habits']['checked_in']} recorded"
        if lm.get("decisions") is not None:
            text += f"; {_plural(lm['decisions']['analysed'], 'decision')} analysed"
        bullets.append(text + ".")
        hidden = [h.rsplit(" ", 1)[-1] for h in lm.get("hidden", [])]
        if hidden:
            bullets.append(f"Not shown for lack of consent: {', '.join(hidden)}.")
    else:
        bullets.append(f"Life Mentor: {_status_line(lm)}.")
    if _ok(plan) and plan["summary"]["open_from_earlier"]:
        bullets.append(f"Open tasks from earlier weeks: {len(plan['summary']['open_from_earlier'])}.")
    return [f"- {b}" for b in bullets[:6]]


def render(model: dict[str, Any]) -> str:
    """Deterministic Markdown for a collected week."""
    sources = [
        label
        for key, label in (("planner", "manual planner"), ("git", "git"), ("life_mentor", "Life Mentor"))
        if model[key].get("status") != "not_requested"
    ]
    lines = [
        f"# Week {model['week']} ({model['start']} to {model['end']})",
        "",
        (
            f"Generated on {model['generated_on']}. Sources: {', '.join(sources) or 'none'}. "
            "Facts and counts only; no ratings."
        ),
        "",
        *_table(model),
        "",
        *_planner_section(model),
        "",
        *_git_section(model),
        "",
        *_lm_section(model),
        "",
        "## Summary",
        "",
        *_summary(model),
    ]
    text = redact("\n".join(lines) + "\n")
    assert_report_clean(text)  # LM02 scanner; raises ForbiddenFieldLeak (fail closed)
    return text
