"""weekly_tracker CLI: plan add/done/skip, show, write (no overwrite without --force)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from life_os.weekly_tracker.cli import main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "wt"
    monkeypatch.setenv("WEEKLY_TRACKER_HOME", str(home))
    monkeypatch.setenv("LIFE_MENTOR_HOME", str(tmp_path / "lm_absent"))
    return home


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_plan_and_show(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        _run(capsys, "plan", "add", "--week", "2026-W41", "--day", "mon", "--text", "Write report")[1].strip()
        == "t1"
    )
    _run(capsys, "plan", "add", "--week", "2026-W41", "--day", "tue", "--text", "Gym")
    assert _run(capsys, "plan", "done", "t1")[0] == 0
    assert _run(capsys, "plan", "skip", "t2")[0] == 0
    code, out, _ = _run(capsys, "show", "--week", "2026-W41", "--no-git", "--generated-on", "2026-10-11")
    assert code == 0
    assert out.startswith("# Week 2026-W41 (2026-10-05 to 2026-10-11)")
    assert "| Mon | 2026-10-05 | 1/1 | 0 |" in out
    assert "[x] t1 Write report" in out and "[-] t2 Gym (skipped)" in out
    assert "unavailable: no Life Mentor data directory" in out
    assert "Work (git)" in out and "not requested" in out


def test_plan_errors(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _run(capsys, "plan", "done", "t9")
    assert code == 1 and "unknown task" in err
    _run(capsys, "plan", "add", "--week", "2026-W41", "--day", "mon", "--text", "x")
    _run(capsys, "plan", "done", "t1")
    code, _, err = _run(capsys, "plan", "skip", "t1")
    assert code == 1 and "already" in err
    with pytest.raises(SystemExit):
        main(["plan", "add", "--day", "funday", "--text", "x"])
    code, _, err = _run(capsys, "show", "--week", "2026-W99", "--no-git")
    assert code == 2 and "week" in err


def test_show_does_not_create_home(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = _run(capsys, "show", "--week", "2026-W41", "--no-git")
    assert code == 0 and not env.exists()


def test_write_default_dir_and_no_overwrite(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ("write", "--week", "2026-W41", "--no-git", "--generated-on", "2026-10-11")
    code, out, _ = _run(capsys, *args)
    target = env / "weeks" / "2026-W41.md"
    assert code == 0 and out.strip() == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE((env / "weeks").stat().st_mode) == 0o700
    first = target.read_text(encoding="utf-8")
    code, _, err = _run(capsys, *args)
    assert code == 1 and "--force" in err
    assert target.read_text(encoding="utf-8") == first
    _run(capsys, "plan", "add", "--week", "2026-W41", "--day", "wed", "--text", "New")
    assert _run(capsys, *args, "--force")[0] == 0
    assert "New" in target.read_text(encoding="utf-8")


def test_write_out_dir(env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "reports"
    code, out, _ = _run(
        capsys,
        "write",
        "--week",
        "2026-W53",
        "--out",
        str(out_dir),
        "--no-git",
        "--generated-on",
        "2026-10-11",
    )
    target = out_dir / "2026-W53.md"
    assert code == 0 and target.is_file() and out.strip() == str(target)
    assert "2026-12-28 to 2027-01-03" in target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert _run(capsys, "write", "--week", "2026-W53", "--out", str(out_dir), "--no-git")[0] == 1


def test_show_with_repo_not_git(env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    code, out, _ = _run(capsys, "show", "--week", "2026-W41", "--repo", str(plain))
    assert code == 0 and "unavailable: not a git repository" in out
