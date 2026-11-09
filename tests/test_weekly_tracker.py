"""weekly_tracker: ISO weeks, manual planner (append-only), renderer golden snapshot."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from life_os.life_mentor.security import JournalTampered, redact
from life_os.weekly_tracker.planner import ENV_HOME, Planner, resolve_home
from life_os.weekly_tracker.render import render
from life_os.weekly_tracker.week import DAY_NAMES, IsoWeek, day_index, weeks_in_year

REPO = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "wt_golden_2026-W41.md"
NOW = "2026-10-11T00:00:00Z"
W41 = IsoWeek(2026, 41)


# --- ISO weeks -----------------------------------------------------------------------


def test_parse_format_and_days() -> None:
    w = IsoWeek.parse("2026-W41")
    assert w == W41 and str(w) == "2026-W41"
    assert (w.start, w.end) == (date(2026, 10, 5), date(2026, 10, 11))
    days = w.days()
    assert len(days) == 7 and days[0].weekday() == 0 and days[-1].weekday() == 6
    assert IsoWeek.containing(date(2026, 10, 11)) == W41
    assert w.contains(date(2026, 10, 5)) and not w.contains(date(2026, 10, 12))


@pytest.mark.parametrize(
    ("day", "week"),
    [
        (date(2026, 12, 28), "2026-W53"),
        (date(2027, 1, 3), "2026-W53"),
        (date(2027, 1, 4), "2027-W01"),
        (date(2025, 12, 29), "2026-W01"),
        (date(2020, 12, 31), "2020-W53"),
    ],
)
def test_year_boundaries(day: date, week: str) -> None:
    assert str(IsoWeek.containing(day)) == week


def test_w53_and_navigation() -> None:
    assert weeks_in_year(2026) == 53 and weeks_in_year(2027) == 52
    w53 = IsoWeek.parse("2026-W53")
    assert (w53.start, w53.end) == (date(2026, 12, 28), date(2027, 1, 3))
    assert w53.next() == IsoWeek(2027, 1) and IsoWeek(2027, 1).previous() == w53
    with pytest.raises(ValueError):
        IsoWeek.parse("2027-W53")


@pytest.mark.parametrize("bad", ["2026-41", "2026W41", "2026-W00", "2026-w41", "26-W41", "", "2026-W5"])
def test_parse_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        IsoWeek.parse(bad)


def test_day_names() -> None:
    assert DAY_NAMES == ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    assert day_index("Mon") == 0 and day_index("sunday") == 6
    with pytest.raises(ValueError):
        day_index("funday")


# --- planner ------------------------------------------------------------------------------


def _planner(tmp_path: Path) -> Planner:
    return Planner(tmp_path / "wt", clock=lambda: NOW)


def test_resolve_home(tmp_path: Path) -> None:
    assert ENV_HOME == "WEEKLY_TRACKER_HOME"
    assert resolve_home(tmp_path / "x", env={}) == tmp_path / "x"
    assert resolve_home(None, env={ENV_HOME: str(tmp_path / "e")}) == tmp_path / "e"
    assert resolve_home(None, env={}) == Path.home() / ".weekly_tracker"
    assert not (tmp_path / "x").exists()


def test_planner_add_done_skip_summary(tmp_path: Path) -> None:
    p = _planner(tmp_path)
    t1 = p.add(W41, "mon", "Write weekly report")
    t2 = p.add(W41, "mon", "Call bank")
    t3 = p.add(W41, "tue", "Gym")
    assert [t.task_id for t in (t1, t2, t3)] == ["t1", "t2", "t3"]
    p.done("t1")
    p.skip("t3")
    s = p.summary(W41)
    assert (s["planned_total"], s["done"], s["skipped"], s["still_planned"]) == (3, 1, 1, 1)
    assert [t["task_id"] for t in s["carry_over"]] == ["t2"]
    assert s["by_day"]["mon"] == {"planned": 2, "done": 1, "skipped": 0}
    assert s["open_from_earlier"] == []
    # carry-over is listed, never moved
    assert [t.week for t in p.tasks(W41.next())] == []


def test_open_from_earlier_weeks(tmp_path: Path) -> None:
    p = _planner(tmp_path)
    p.add(W41.previous(), "fri", "Renew insurance")
    old_done = p.add(W41.previous(), "fri", "Pay rent")
    p.done(old_done.task_id)
    s = p.summary(W41)
    assert [t["text"] for t in s["open_from_earlier"]] == ["Renew insurance"]


def test_move_keeps_history(tmp_path: Path) -> None:
    p = _planner(tmp_path)
    t = p.add(W41, "mon", "Read")
    moved = p.move(t.task_id, W41, "wed")
    assert moved.day == "wed"
    events = [e["event"]["event"] for e in p.journal.entries()]
    assert events == ["task_add", "task_move"]
    with pytest.raises(ValueError):
        p.move(t.task_id, W41, "wed")


@pytest.mark.parametrize("action", ["done", "skip"])
def test_status_is_final(tmp_path: Path, action: str) -> None:
    p = _planner(tmp_path)
    p.add(W41, "mon", "x")
    getattr(p, action)("t1")
    for again in ("done", "skip"):
        with pytest.raises(ValueError):
            getattr(p, again)("t1")
    with pytest.raises(ValueError):
        p.move("t1", W41, "tue")


@pytest.mark.parametrize(("day", "text"), [("funday", "x"), ("mon", ""), ("mon", "  "), ("mon", "x" * 201)])
def test_add_validation(tmp_path: Path, day: str, text: str) -> None:
    with pytest.raises(ValueError):
        _planner(tmp_path).add(W41, day, text)


def test_unknown_task(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        _planner(tmp_path).done("t9")


def test_append_only_and_private_files(tmp_path: Path) -> None:
    p = _planner(tmp_path)
    p.add(W41, "mon", "a")
    first = p.journal.path.read_text(encoding="utf-8")
    p.done("t1")
    second = p.journal.path.read_text(encoding="utf-8")
    assert second.startswith(first) and len(second.splitlines()) == 2
    assert stat.S_IMODE((tmp_path / "wt").stat().st_mode) == 0o700
    for f in (tmp_path / "wt").rglob("*"):
        if f.is_file():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    snap = json.loads((tmp_path / "wt" / "plans" / "2026-W41.json").read_text())
    assert snap["tasks"][0]["status"] == "done"


def test_tampered_log_refuses(tmp_path: Path) -> None:
    p = _planner(tmp_path)
    p.add(W41, "mon", "a")
    p.add(W41, "tue", "b")
    lines = p.journal.path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["event"]["text"] = "changed"
    lines[0] = json.dumps(rec)
    p.journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalTampered):
        p.tasks(W41)
    with pytest.raises(JournalTampered):
        p.add(W41, "wed", "c")


def test_task_text_redacted(tmp_path: Path) -> None:
    p = _planner(tmp_path)
    t = p.add(W41, "mon", "email me@example.com about PAN ABCDE1234F")
    assert "me@example.com" not in t.text and "ABCDE1234F" not in t.text
    assert "me@example.com" not in p.journal.path.read_text(encoding="utf-8")


def test_reading_does_not_create_home(tmp_path: Path) -> None:
    p = Planner(tmp_path / "nope")
    assert p.tasks(W41) == [] and p.summary(W41)["planned_total"] == 0
    assert not (tmp_path / "nope").exists()


# --- renderer -----------------------------------------------------------------------------


def fixture_model() -> dict[str, Any]:
    """A fully collected week, as ``life_os.weekly_tracker.render.collect`` would return it."""
    days = [d.isoformat() for d in W41.days()]
    return {
        "week": "2026-W41",
        "start": days[0],
        "end": days[6],
        "generated_on": "2026-10-11",
        "days": days,
        "planner": {
            "status": "ok",
            "reason": None,
            "tasks": [
                {
                    "task_id": "t1",
                    "week": "2026-W41",
                    "day": "mon",
                    "text": "Write weekly report",
                    "status": "done",
                },
                {"task_id": "t2", "week": "2026-W41", "day": "mon", "text": "Call bank", "status": "planned"},
                {
                    "task_id": "t3",
                    "week": "2026-W41",
                    "day": "tue",
                    "text": "Gym | legs",
                    "status": "skipped",
                },
            ],
            "summary": {
                "planned_total": 3,
                "done": 1,
                "skipped": 1,
                "still_planned": 1,
                "carry_over": [{"task_id": "t2", "day": "mon", "text": "Call bank"}],
                "open_from_earlier": [
                    {"task_id": "t0", "week": "2026-W40", "day": "fri", "text": "Renew insurance"}
                ],
            },
        },
        "git": {
            "status": "ok",
            "reason": None,
            "repo_name": "demo",
            "by_day": {
                days[0]: [
                    {
                        "hash": "abc1234",
                        "subject": "Add planner",
                        "files": 2,
                        "insertions": 30,
                        "deletions": 4,
                    },
                    {"hash": "def5678", "subject": "Fix tests", "files": 1, "insertions": 10, "deletions": 1},
                ],
                days[2]: [
                    {
                        "hash": "0123abc",
                        "subject": "Docs for [EMAIL]",
                        "files": 1,
                        "insertions": 5,
                        "deletions": 0,
                    }
                ],
            },
            "totals": {
                "commits": 3,
                "active_days": 2,
                "files_changed": 3,
                "insertions": 45,
                "deletions": 5,
                "test_files_touched": 1,
            },
        },
        "life_mentor": {
            "status": "ok",
            "reason": None,
            "hidden": ["not shown: no consent for finance"],
            "errors": [],
            "metrics": {
                "sleep_hours": {
                    "unit": "hours",
                    "by_day": {days[0]: Decimal("7.5"), days[1]: Decimal("6.25")},
                    "points": 2,
                    "average": Decimal("6.88"),
                    "total": None,
                },
                "exercise_minutes": {
                    "unit": "minutes",
                    "by_day": {days[0]: Decimal(30), days[2]: Decimal(45)},
                    "points": 2,
                    "average": Decimal("37.50"),
                    "total": Decimal(75),
                },
            },
            "habits": {
                "habits": [
                    {
                        "habit_id": "walk",
                        "name": "walk 20 min",
                        "by_day": {days[0]: "done", days[1]: "missed", days[2]: "done"},
                    }
                ],
                "done": 2,
                "checked_in": 3,
            },
            "decisions": None,
        },
    }


def test_render_golden_snapshot() -> None:
    text = render(fixture_model())
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_render_is_deterministic_and_clean() -> None:
    a, b = render(fixture_model()), render(fixture_model())
    assert a == b
    assert redact(a) == a
    assert "score" not in a.casefold()
    assert "Gym \\| legs" in a  # table/markdown-safe
    summary = a.split("## Summary", 1)[1]
    bullets = [line for line in summary.splitlines() if line.startswith("- ")]
    assert 3 <= len(bullets) <= 6


def test_render_unavailable_sections() -> None:
    m = fixture_model()
    m["git"] = {"status": "unavailable", "reason": "not a git repository"}
    m["life_mentor"] = {"status": "unavailable", "reason": "no Life Mentor data directory"}
    m["planner"] = {"status": "unavailable", "reason": "planner log failed its integrity check"}
    text = render(m)
    assert "unavailable: not a git repository" in text
    assert "unavailable: no Life Mentor data directory" in text
    assert "unavailable: planner log failed its integrity check" in text
    assert "| Mon | 2026-10-05 | — | — | — | — |" in text


def test_render_final_redaction_pass() -> None:
    m = fixture_model()
    m["git"]["by_day"][m["days"][0]][0]["subject"] = "call 9876543210"
    assert "9876543210" not in render(m)


def test_importing_weekly_tracker_pulls_no_network_module() -> None:
    code = (
        "import sys\nbefore=set(sys.modules)\n"
        "import life_os.weekly_tracker, life_os.weekly_tracker.cli, life_os.weekly_tracker.render\n"
        # Full dotted names: forbid network-capable modules but allow urllib.parse,
        # which the stdlib pulls transitively on some platforms (no I/O).
        "mods=set(sys.modules)-before\n"
        "forbidden={'socket','ssl','requests','httpx','aiohttp','asyncio',"
        "'http.client','urllib.request','urllib.error'}\n"
        "print(sorted(mods & forbidden))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=False, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_packaging_wiring() -> None:
    py = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = ["life_os*"]' in py
    assert 'weekly-tracker = "life_os.weekly_tracker.cli:main"' in py
    assert "dependencies = []" in py
    assert ".weekly_tracker/" in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert (REPO / "src" / "life_os" / "weekly_tracker" / "README.md").is_file()
