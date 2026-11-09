"""weekly_tracker sources: read-only Life Mentor adapter and read-only git adapter."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from life_os.life_mentor.decision_engine import DECISION_JOURNAL
from life_os.life_mentor.lifestyle import HabitTracker
from life_os.life_mentor.profile import ConsentLedger
from life_os.life_mentor.security import PrivateStore
from life_os.life_mentor.tracking import MetricLog
from life_os.weekly_tracker.sources.git_work import git_section
from life_os.weekly_tracker.sources.life_mentor import life_mentor_section
from life_os.weekly_tracker.week import IsoWeek

D = Decimal
NOW = "2026-10-11T00:00:00Z"
W41 = IsoWeek(2026, 41)
MON = date(2026, 10, 5)


# --- Life Mentor adapter ---------------------------------------------------------------


def _lm_home(tmp_path: Path, *cats: str) -> Path:
    home = tmp_path / "lm"
    store = PrivateStore(home)
    ledger = ConsentLedger(store, clock=lambda: NOW)
    for c in ("health", "lifestyle", "finance"):
        ledger.grant(c, purpose="fixture")
    log = MetricLog(store, clock=lambda: NOW)
    log.record("sleep_hours", D("7.5"), MON, provenance="USER_ESTIMATE", basis="diary")
    log.record("sleep_hours", D("6.25"), MON + timedelta(days=1), provenance="USER_ESTIMATE", basis="diary")
    log.record(
        "sleep_hours", D(8), MON - timedelta(days=1), provenance="USER_ESTIMATE", basis="previous week"
    )
    log.record("exercise_minutes", D(30), MON, provenance="USER_ESTIMATE", basis="watch")
    log.record("exercise_minutes", D(45), MON + timedelta(days=2), provenance="USER_ESTIMATE", basis="watch")
    log.record("income", D(100000), MON, provenance="VERIFIED", basis="payslip")
    habits = HabitTracker(store, clock=lambda: NOW)
    habits.add_habit("walk", "walk 20 min", started_on=MON)
    habits.check_in("walk", MON, done=True, as_of=MON + timedelta(days=6))
    habits.check_in("walk", MON + timedelta(days=1), done=False, as_of=MON + timedelta(days=6))
    store.journal(DECISION_JOURNAL, clock=lambda: NOW).append(
        {
            "event": "decision_analyzed",
            "decision_id": "d" * 64,
            "options": ["course", "x"],
            "dispositions": {"course": "HOLD"},
            "overall_disposition": "HOLD",
            "recommendation_status": "recommend",
            "recommended": "x",
        }
    )
    for c in ("health", "lifestyle", "finance"):
        if c not in cats:
            ledger.revoke(c, reason="fixture")
    return home


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_lm_with_full_consent(tmp_path: Path) -> None:
    home = _lm_home(tmp_path, "health", "lifestyle", "finance")
    before = _snapshot(home)
    sec = life_mentor_section(home, W41)
    assert _snapshot(home) == before  # strictly read-only
    assert sec["status"] == "ok" and sec["hidden"] == [] and sec["errors"] == []
    sleep = sec["metrics"]["sleep_hours"]
    assert sleep["by_day"] == {"2026-10-05": D("7.5"), "2026-10-06": D("6.25")}
    assert (sleep["points"], sleep["average"], sleep["total"]) == (2, D("6.88"), None)
    ex = sec["metrics"]["exercise_minutes"]
    assert (ex["total"], ex["average"]) == (D(75), D("37.50"))
    assert sec["metrics"]["income"]["total"] == D(100000)
    [walk] = sec["habits"]["habits"]
    assert walk["by_day"]["2026-10-05"] == "done" and walk["by_day"]["2026-10-06"] == "missed"
    assert walk["by_day"]["2026-10-07"] == "no check-in"
    assert (sec["habits"]["done"], sec["habits"]["checked_in"]) == (1, 2)
    assert sec["decisions"]["analysed"] == 1 and sec["decisions"]["confirmed"] == 0


def test_lm_without_consent_never_reads_values(tmp_path: Path) -> None:
    home = _lm_home(tmp_path, "lifestyle")
    sec = life_mentor_section(home, W41)
    assert sec["status"] == "ok"
    assert sec["hidden"] == ["not shown: no consent for finance", "not shown: no consent for health"]
    assert "sleep_hours" not in sec["metrics"] and "income" not in sec["metrics"]
    assert sec["decisions"] is None
    assert "exercise_minutes" in sec["metrics"] and sec["habits"] is not None
    assert "Decimal('7.5')" not in repr(sec) and "6.25" not in repr(sec)


def test_lm_missing_home_is_unavailable_and_not_created(tmp_path: Path) -> None:
    sec = life_mentor_section(tmp_path / "absent", W41)
    assert sec["status"] == "unavailable" and "no Life Mentor data" in sec["reason"]
    assert not (tmp_path / "absent").exists()


def test_lm_tampered_consent_is_unavailable(tmp_path: Path) -> None:
    home = _lm_home(tmp_path, "health")
    consent = home / "consent.jsonl"
    consent.write_text(consent.read_text(encoding="utf-8").replace("fixture", "forged"), encoding="utf-8")
    sec = life_mentor_section(home, W41)
    assert sec["status"] == "unavailable" and "integrity" in sec["reason"]


def test_lm_tampered_metrics_reported_as_error(tmp_path: Path) -> None:
    home = _lm_home(tmp_path, "health", "lifestyle", "finance")
    (home / "tracking" / "metrics.jsonl.head").unlink()
    sec = life_mentor_section(home, W41)
    assert sec["status"] == "ok" and sec["metrics"] == {}
    assert any("metrics" in e and "integrity" in e for e in sec["errors"])


# --- git adapter ------------------------------------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )


def _commit(repo: Path, files: dict[str, str], subject: str, when: str) -> None:
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject, when=when)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "demo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.name", "Fixture")
    _git(r, "config", "user.email", "fixture@example.invalid")
    _commit(r, {"old.txt": "x\n"}, "Before the week", "2026-10-04T12:00:00+00:00")
    _commit(r, {"app.py": "a\nb\nc\n", "tests/test_app.py": "t\n"}, "Add app", "2026-10-05T10:00:00+00:00")
    _commit(r, {"app.py": "a\nB\nc\nd\n"}, "Edit app, mail me@example.com", "2026-10-05T15:00:00+00:00")
    _commit(r, {"README.md": "hi\n"}, "Docs", "2026-10-07T09:30:00+00:00")
    _commit(r, {"later.txt": "z\n"}, "After the week", "2026-10-12T08:00:00+00:00")
    return r


@needs_git
def test_git_week_summary(repo: Path) -> None:
    sec = git_section(repo, W41, tz="UTC")
    assert sec["status"] == "ok" and sec["repo_name"] == "demo"
    mon = sec["by_day"]["2026-10-05"]
    assert [c["subject"] for c in mon] == ["Add app", "Edit app, mail [EMAIL]"]
    assert all(len(c["hash"]) >= 7 for c in mon)
    assert mon[0]["files"] == 2 and mon[0]["insertions"] == 4
    assert (mon[1]["insertions"], mon[1]["deletions"]) == (2, 1)
    assert [c["subject"] for c in sec["by_day"]["2026-10-07"]] == ["Docs"]
    t = sec["totals"]
    assert t == {
        "commits": 3,
        "active_days": 2,
        "files_changed": 3,
        "insertions": 7,
        "deletions": 1,
        "test_files_touched": 1,
    }


@needs_git
def test_git_does_not_modify_repo(repo: Path) -> None:
    before = {str(p): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    git_section(repo, W41, tz="UTC")
    assert {str(p): p.read_bytes() for p in repo.rglob("*") if p.is_file()} == before


@needs_git
def test_git_empty_week_and_empty_repo(repo: Path, tmp_path: Path) -> None:
    sec = git_section(repo, IsoWeek(2026, 30), tz="UTC")
    assert sec["status"] == "ok" and sec["totals"]["commits"] == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    sec = git_section(empty, W41, tz="UTC")
    assert sec["status"] == "ok" and sec["totals"]["commits"] == 0


@needs_git
def test_git_unavailable_reasons(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_section(plain, W41)["reason"] == "not a git repository"
    assert git_section(tmp_path / "missing", W41)["reason"] == "repository path does not exist"
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    assert git_section(f, W41)["reason"] == "repository path is not a directory"
    assert git_section(None, W41)["reason"] == "no repository given"


def test_git_missing_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    sec = git_section(tmp_path, W41)
    assert sec == {"status": "unavailable", "reason": "git is not installed"}


@needs_git
def test_git_non_ascii_paths_are_not_c_quoted(tmp_path: Path) -> None:
    # Regression: with git's default core.quotePath, "tests/café_test.py" came back as
    # "\"tests/caf\\303\\251_test.py\"", so it was not recognised as a test file.
    r = tmp_path / "u"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, {"tests/café.py": "t\n", "naïve.txt": "x\n"}, "Unicode paths", "2026-10-06T10:00:00+00:00")
    sec = git_section(r, IsoWeek(2026, 41), tz="UTC")
    assert sec["status"] == "ok"
    assert sec["totals"]["files_changed"] == 2
    assert sec["totals"]["test_files_touched"] == 1
