"""Regression tests for bugs fixed while extracting Life OS from the Asur monorepo.

Each test names the bug it pins. See CHANGELOG.md (0.1.0, "Fixed").
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from life_os.life_mentor.ics import CalendarEvent, build_calendar
from life_os.weekly_tracker.cli import main as wt_main
from life_os.weekly_tracker.ics import planner_to_calendar
from life_os.weekly_tracker.planner import Planner
from life_os.weekly_tracker.sources.git_guard import (
    GitConfigError,
    dangerous_keys,
    parse_git_config,
    repo_config_problem,
)
from life_os.weekly_tracker.sources.git_work import git_section
from life_os.weekly_tracker.week import IsoWeek

FIXED = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _unfold(ics: str) -> list[str]:
    return ics.replace("\r\n ", "").split("\r\n")


# --- ics.py: folding split multi-byte UTF-8 characters and silently dropped them ---------


@pytest.mark.parametrize("summary", ["é" * 80, "a" + "€" * 60, "नमस्ते " * 20, "🙂" * 40])
def test_ics_fold_never_drops_multibyte_characters(summary: str) -> None:
    ics = build_calendar([CalendarEvent("u@x", summary, date(2026, 1, 1))], cal_name="c", dtstamp=FIXED)
    for physical in ics.split("\r\n"):
        assert len(physical.encode("utf-8")) <= 75
    assert f"SUMMARY:{summary}" in _unfold(ics)


# --- ics.py: a lone CR (and other controls) was emitted raw, breaking the line structure ---


def test_ics_escape_handles_lone_cr_and_controls() -> None:
    ics = build_calendar(
        [CalendarEvent("u@x", "a\rb\x00c\x1bd\te", date(2026, 1, 1))], cal_name="c", dtstamp=FIXED
    )
    assert "SUMMARY:a\\nbcd\te" in _unfold(ics)
    assert "\r" not in ics.replace("\r\n", "")


def test_ics_rejects_non_positive_span_and_control_chars_in_uid() -> None:
    with pytest.raises(ValueError):
        CalendarEvent("u@x", "s", date(2026, 1, 2), end=date(2026, 1, 2))
    with pytest.raises(ValueError):
        CalendarEvent("u@x\r\nX-EVIL:1", "s", date(2026, 1, 2))


def test_ics_dates_are_four_digit_years() -> None:
    ics = build_calendar(
        [CalendarEvent("u@x", "s", date(999, 1, 2))],
        cal_name="c",
        dtstamp=datetime(999, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    assert "DTSTART;VALUE=DATE:09990102" in ics
    assert "DTSTAMP:09990102T030405Z" in ics


# --- weekly_tracker/ics.py: UID changed when a task moved, duplicating calendar events ----


def test_planner_uid_is_stable_across_move_and_status(tmp_path: Path) -> None:
    planner = Planner(tmp_path / "wt")
    w = IsoWeek(2026, 41)
    task = planner.add(w, "mon", "write report")

    def uid() -> str:
        ics = planner_to_calendar(planner.tasks(), dtstamp=FIXED)
        return next(line for line in ics.split("\r\n") if line.startswith("UID:"))

    before = uid()
    planner.move(task.task_id, IsoWeek(2026, 42), "wed")
    assert uid() == before
    planner.done(task.task_id)
    assert uid() == before


# --- week.py ------------------------------------------------------------------------------


def test_isoweek_rejects_weeks_ending_past_year_9999() -> None:
    with pytest.raises(ValueError):
        IsoWeek.parse("9999-W52")  # Sunday is 10000-01-02; .end used to crash later


def test_isoweek_rejects_non_ascii_digits() -> None:
    with pytest.raises(ValueError):
        IsoWeek.parse("٢٠٢٦-W41")


# --- weekly_tracker/cli.py ------------------------------------------------------------------


def test_cli_write_to_unwritable_dir_is_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    code = wt_main(
        [
            "--home",
            str(tmp_path / "wt"),
            "--no-life-mentor",
            "write",
            "--no-git",
            "--out",
            str(blocker / "sub"),
        ]
    )
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_cli_write_never_clobbers_and_leaves_no_temp(tmp_path: Path) -> None:
    home, out = tmp_path / "wt", tmp_path / "out"
    args = [
        "--home",
        str(home),
        "--no-life-mentor",
        "write",
        "--no-git",
        "--week",
        "2026-W41",
        "--out",
        str(out),
    ]
    assert wt_main(args) == 0
    first = (out / "2026-W41.md").read_bytes()
    (out / "2026-W41.md").write_bytes(b"mine")
    assert wt_main(args) == 1
    assert (out / "2026-W41.md").read_bytes() == b"mine"
    assert wt_main([*args, "--force"]) == 0
    assert (out / "2026-W41.md").read_bytes() == first
    assert (out / "2026-W41.md").stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in out.iterdir()) == ["2026-W41.md"]


def test_cli_ics_stdout_keeps_crlf(tmp_path: Path, capfdbinary: pytest.CaptureFixture[bytes]) -> None:
    home = str(tmp_path / "wt")
    assert wt_main(["--home", home, "plan", "add", "--day", "mon", "--text", "x", "--week", "2026-W41"]) == 0
    capfdbinary.readouterr()
    assert wt_main(["--home", home, "plan", "ics", "--week", "2026-W41"]) == 0
    out = capfdbinary.readouterr().out
    assert out.startswith(b"BEGIN:VCALENDAR\r\n") and out.endswith(b"END:VCALENDAR\r\n")
    assert b"\n" not in out.replace(b"\r\n", b"")


# --- git_guard.py: a backslash at the end of a COMMENT hid the next line from the guard ----


@pytest.mark.parametrize(
    "text",
    [
        '[core]\n\tx = y ;\\\n\tfsmonitor = "touch PWNED"\n',
        '[core]\n\tx = y #\\\n\tfsmonitor = "touch PWNED"\n',
        '[core]\n\tx = a \\"; \\\n\tpager = evil\n',
    ],
)
def test_git_guard_comment_backslash_does_not_hide_next_line(text: str) -> None:
    assert dangerous_keys(parse_git_config(text)), parse_git_config(text)


def test_git_guard_quoted_continuation_still_skipped() -> None:
    # Inside quotes ';' is literal and the backslash really does continue the value.
    text = '[user]\n\tname = "a ;\\\n\tfsmonitor = not-a-key"\n'
    assert parse_git_config(text) == [("user", None, "name")]


def test_git_guard_unterminated_quote_fails_closed() -> None:
    with pytest.raises(GitConfigError):
        parse_git_config('[user]\n\tname = "open\n')


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("[gpg]", "program"),
        ('[gpg "ssh"]', "program"),
        ('[difftool "x"]', "cmd"),
        ('[mergetool "x"]', "cmd"),
        ('[remote "origin"]', "uploadpack"),
        ('[remote "origin"]', "vcs"),
        ('[submodule "m"]', "update"),
        ('[trailer "t"]', "command"),
    ],
)
def test_git_guard_flags_more_command_keys(section: str, key: str) -> None:
    assert dangerous_keys(parse_git_config(f"{section}\n\t{key} = x\n"))


@needs_git
def test_git_guard_bypass_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(f"[core]\n\tx = y ;\\\n\tfsmonitor = touch {tmp_path / 'pwned'}\n")
    # git itself sees core.fsmonitor here:
    listed = subprocess.run(
        ["git", "config", "--file", str(repo / ".git" / "config"), "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "core.fsmonitor=" in listed
    loc, reason = repo_config_problem(repo)
    assert loc is None and reason is not None
    assert git_section(repo, IsoWeek(2026, 41))["status"] == "unavailable"
    assert not (tmp_path / "pwned").exists()


@needs_git
def test_git_guard_refuses_fifo_config(tmp_path: Path) -> None:
    import os

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    os.mkfifo(repo / ".git" / "config.worktree")
    loc, reason = repo_config_problem(repo)  # must not block reading the FIFO
    assert loc is None and reason is not None


# --- finance ----------------------------------------------------------------------------------

from decimal import Decimal as D  # noqa: E402

from life_os.core.gate import BLOCK, HOLD, SHIP  # noqa: E402
from life_os.life_mentor.finance import debt_payoff, emergency_fund, goal_sip  # noqa: E402
from life_os.life_mentor.finance.debt import Debt  # noqa: E402
from life_os.life_mentor.finance.money import format_inr  # noqa: E402
from life_os.life_mentor.integrations.amfi_nav import parse_navall  # noqa: E402
from life_os.life_mentor.integrations.bank_csv import _amount  # noqa: E402


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (D("12.999"), "Rs 13"),
        (D("1234567.995"), "Rs 12,34,568"),
        (D("-0.001"), "Rs 0"),
        (D("-1250000.5"), "-Rs 12,50,000.50"),
        (D("999.995"), "Rs 1,000"),
        (D("12.994"), "Rs 12.99"),
    ],
)
def test_format_inr_carries_rounding_into_rupees(amount: D, text: str) -> None:
    assert format_inr(amount) == text


def test_debt_avalanche_rising_then_falling_total_is_not_never() -> None:
    # Total balance rises for a while (card interest > budget) but the plan does clear.
    rep = debt_payoff([Debt("card", 1000, 120, 0), Debt("home", 20000, D("1.2"), 0)], monthly_budget=110)
    assert rep["result"]["avalanche"]["status"] == "paid_off"
    assert rep["result"]["avalanche"]["months"] is not None


def test_debt_budget_below_interest_still_never() -> None:
    rep = debt_payoff([Debt("card", 100000, 36, 1000)], monthly_budget=1000)
    assert rep["result"]["avalanche"]["status"] == "never"
    assert rep["gate"]["disposition"] == HOLD


@pytest.mark.parametrize(
    ("months", "rates"),
    [
        (120, {"conservative": 8, "base": 12, "optimistic": 1200}),
        (10**7, {"conservative": 8, "base": 10, "optimistic": 12}),
    ],
)
def test_goal_sip_extreme_inputs_block_not_crash(months: int, rates: dict[str, int]) -> None:
    rep = goal_sip(target_corpus=1000000, months=months, annual_return_percent=rates)
    assert rep["gate"]["disposition"] == BLOCK and rep["result"] is None


def test_goal_sip_long_horizon_inside_bounds_computes() -> None:
    rep = goal_sip(
        target_corpus=10**9,
        months=1200,
        annual_return_percent={"conservative": 50, "base": 80, "optimistic": 100},
    )
    assert rep["gate"]["disposition"] == SHIP


def test_emergency_fund_zero_expenses_holds() -> None:
    rep = emergency_fund(essential_monthly=0, current_balance=1000, monthly_surplus=100)
    assert rep["gate"]["disposition"] == HOLD


@pytest.mark.parametrize("text", ["12,50", "0,50", "1,234,56", "1.234,50"])
def test_bank_amount_rejects_decimal_comma(text: str) -> None:
    with pytest.raises(ValueError):
        _amount(text)


@pytest.mark.parametrize(
    ("text", "value"), [("1,23,456.00", D("123456.00")), ("25,000", D(25000)), ("7", D(7))]
)
def test_bank_amount_accepts_grouping(text: str, value: D) -> None:
    assert _amount(text) == value


_NAV_HEADER = "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date\n"


def test_navall_duplicate_code_and_lenient_numbers_are_unpriced(tmp_path: Path) -> None:
    f = tmp_path / "NAVAll.txt"
    f.write_text(
        _NAV_HEADER
        + "101;INF000000001;-;Fund A;Direct;Growth;10.50;06-Oct-2026\n"
        + "101;INF000000001;-;Fund A;Direct;Growth;9.00;06-Oct-2020\n"
        + "102;INF000000002;-;Fund B;Direct;Growth;1_0.5;06-Oct-2026\n"
        + "103;INF000000003;-;Fund C;Direct;Growth;1e2;06-Oct-2026\n"
        + "104;INF000000004;-;Fund D;Direct;Growth;12.25;06-Oct-2026\n",
        encoding="utf-8",
    )
    navs = parse_navall(f)
    assert navs["101"]["nav"] is None
    assert navs["102"]["nav"] is None and navs["103"]["nav"] is None
    assert navs["104"]["nav"] == D("12.25")


def test_bank_csv_row_with_extra_cells_is_rejected(tmp_path: Path) -> None:
    from life_os.life_mentor.integrations import preview_bank_csv

    csv_path = tmp_path / "s.csv"
    csv_path.write_text("Date,Desc,Debit\n01/04/2026,rent,25,000.00\n02/04/2026,rent,100\n", encoding="utf-8")
    mapping = {
        "date_column": "Date",
        "date_format": "%d/%m/%Y",
        "description_column": "Desc",
        "debit_column": "Debit",
        "category_rules": [{"contains": "rent", "category": "rent"}],
    }
    rep = preview_bank_csv(csv_path, mapping)
    assert rep["gate"]["disposition"] != SHIP
    assert any("more cells" in r["reason"] for r in rep["result"]["rejected"])
