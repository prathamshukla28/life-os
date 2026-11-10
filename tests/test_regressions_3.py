"""Regression tests (part 3): CLI messages, money bounds, typed money facts, habits CLI.

Each test names the bug it pins. See CHANGELOG.md (0.1.0, "Fixed").
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from life_os.core.gate import BLOCK
from life_os.life_mentor.cli import main as lm_main
from life_os.life_mentor.finance.inputs import input_checks, resolve
from life_os.life_mentor.finance.money import MAX_AMOUNT, to_money
from life_os.life_mentor.integrations import parse_navall, preview_bank_csv
from life_os.life_mentor.profile import FACTS_FILE, Fact, FactExists, Profile
from life_os.life_mentor.security import PrivateStore
from life_os.life_mentor.tracking import MetricLog
from life_os.weekly_tracker.cli import main as wt_main

NOW = "2026-11-10T00:00:00Z"
HUGE = "99999999999999999999999999999"  # 29 digits: used to overflow Decimal's 28-digit context


def _lm(tmp_path: Path, *args: str) -> int:
    return lm_main(["--home", str(tmp_path / "lm"), *args])


def _wt(tmp_path: Path, *args: str) -> int:
    return wt_main(["--home", str(tmp_path / "wt"), "--no-life-mentor", *args])


def _lm_with(tmp_path: Path, *categories: str) -> None:
    assert _lm(tmp_path, "init") == 0
    for cat in categories:
        assert _lm(tmp_path, "consent", "grant", cat, "--purpose", "test") == 0


# --- weekly_tracker/cli.py: "error: unknown task unknown task 't9'" ----------------------------


def test_wt_unknown_task_is_named_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _wt(tmp_path, "plan", "done", "t9") == 1
    assert capsys.readouterr().err.strip() == "error: unknown task 't9'"


# --- CLI errors named Python calls (ConsentLedger.grant, correct()) instead of commands --------


def test_lm_missing_consent_names_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _lm_with(tmp_path)
    capsys.readouterr()
    assert _lm(tmp_path, "fact", "set", "gross_salary_annual", "1300000", "--category", "finance") == 2
    err = capsys.readouterr().err
    assert 'life-mentor consent grant finance --purpose "..."' in err
    assert "ConsentLedger" not in err


def test_lm_existing_fact_names_fact_correct(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _lm_with(tmp_path, "finance")
    args = ("fact", "set", "gross_salary_annual", "1300000", "--category", "finance")
    assert _lm(tmp_path, *args) == 0
    capsys.readouterr()
    assert _lm(tmp_path, *args) == 2
    err = capsys.readouterr().err
    assert "life-mentor fact correct gross_salary_annual VALUE" in err
    assert "correct()" not in err


def test_lm_correct_missing_fact_names_fact_set(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _lm_with(tmp_path, "finance")
    capsys.readouterr()
    assert _lm(tmp_path, "fact", "correct", "gross_salary_annual", "1300000") == 2
    assert "life-mentor fact set gross_salary_annual VALUE" in capsys.readouterr().err


def test_library_errors_name_the_real_methods(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "home").ensure()
    prof = Profile(store, clock=lambda: NOW)
    prof.consent.grant("finance", purpose="t")
    prof.consent.grant("health", purpose="t")
    prof.set_fact("gross_salary_annual", D(1300000), "finance", "VERIFIED", "Form 16")
    with pytest.raises(FactExists, match=r"Profile\.correct\(\)"):
        prof.set_fact("gross_salary_annual", D(1), "finance", "VERIFIED", "x")
    log = MetricLog(store, clock=lambda: NOW)
    log.record("sleep_hours", D(7), date(2026, 11, 9), provenance="USER_ESTIMATE", basis="x")
    with pytest.raises(ValueError, match=r"MetricLog\.correct\(\)"):
        log.record("sleep_hours", D(8), date(2026, 11, 9), provenance="USER_ESTIMATE", basis="x")


# --- integrations/bank_csv.py: HOLD reasons said "allow_partial", not the CLI flag -------------


def test_bank_csv_hold_names_the_cli_flag(tmp_path: Path) -> None:
    csv_file = tmp_path / "s.csv"
    csv_file.write_text("Date,Desc,Debit\n01/09/2026,RENT,100\n01/09/2026,CAFE,50\nbad,RENT,1\n", "utf-8")
    mapping = {
        "date_column": "Date",
        "date_format": "%d/%m/%Y",
        "description_column": "Desc",
        "debit_column": "Debit",
        "category_rules": [{"contains": "rent", "category": "rent"}],
    }
    reasons = [r["reason"] for r in preview_bank_csv(csv_file, mapping)["gate"]["reasons"]]
    held = [r for r in reasons if "row(s) could not be read" in r or "match no category rule" in r]
    assert len(held) == 2
    assert all("--allow-partial" in r for r in held)
    assert not any("allow_partial" in r for r in reasons)


# --- weekly_tracker/cli.py: --out onto a directory named the hidden temp file -----------------


@pytest.mark.parametrize("force", [False, True])
def test_wt_ics_out_directory_names_the_users_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], force: bool
) -> None:
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    args = ["plan", "ics", "--week", "2026-W46", "--out", str(outdir)] + (["--force"] if force else [])
    assert _wt(tmp_path, *args) == 1
    err = capsys.readouterr().err
    assert err.strip() == f"error: {outdir} is a directory; give a file path"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes")
def test_wt_write_failure_names_the_target_not_the_temp_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        assert _wt(tmp_path, "plan", "ics", "--week", "2026-W46", "--out", str(locked / "w.ics")) == 1
        err = capsys.readouterr().err
        assert f"({locked / 'w.ics'})" in err and ".tmp" not in err
    finally:
        locked.chmod(0o700)


# --- money bounds: a 29-digit salary printed "error: [<class 'decimal.InvalidOperation'>]" -----


def test_to_money_has_one_bound() -> None:
    assert to_money("x", MAX_AMOUNT) == MAX_AMOUNT
    assert to_money("x", -MAX_AMOUNT) == -MAX_AMOUNT
    for bad in (MAX_AMOUNT + 1, -MAX_AMOUNT - 1, D(HUGE)):
        with pytest.raises(ValueError, match="out of range"):
            to_money("x", bad)


def test_out_of_range_input_blocks_instead_of_raising() -> None:
    direct = resolve("upfront_cost", D(HUGE))
    stored = resolve("gross_salary", Fact("gross_salary_annual", D(HUGE), "finance", "VERIFIED", "x", NOW))
    for iv in (direct, stored):
        disposition, reason = input_checks([iv])[0].fn()
        assert disposition == BLOCK and "out of range" in reason


def test_lm_tax_on_a_stored_huge_salary_is_a_clear_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A store written before money facts were validated may still hold such a value.
    _lm_with(tmp_path, "finance")
    store = PrivateStore(tmp_path / "lm")
    fact = Fact("gross_salary_annual", D(HUGE), "finance", "VERIFIED", "x", NOW)
    store.write_json(FACTS_FILE, {"facts": {fact.key: fact.to_dict()}})
    capsys.readouterr()
    assert _lm(tmp_path, "--as-of", "2026-11-10", "tax", "--regime", "new") == 2
    out, err = capsys.readouterr()
    assert "Overall status: BLOCK" in out and "out of range" in out
    assert "InvalidOperation" not in out + err


def test_bank_csv_amount_beyond_the_bound_is_a_rejected_row(tmp_path: Path) -> None:
    csv_file = tmp_path / "s.csv"
    csv_file.write_text(f"Date,Desc,Debit\n01/09/2026,RENT,{HUGE}\n02/09/2026,RENT,100\n", "utf-8")
    mapping = {
        "date_column": "Date",
        "date_format": "%d/%m/%Y",
        "description_column": "Desc",
        "debit_column": "Debit",
        "category_rules": [{"contains": "rent", "category": "rent"}],
    }
    result = preview_bank_csv(csv_file, mapping)["result"]
    assert result["rejected"] == [{"line": 2, "reason": "amount is out of range (limit 10^15)"}]
    assert result["rows_used"] == 1


def test_nav_beyond_the_bound_is_unpriced(tmp_path: Path) -> None:
    nav_file = tmp_path / "NAVAll.txt"
    header = "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date"
    rows = [
        f"900001;INF000000001;-;SYNTHETIC FUND;Direct Plan;Growth;{HUGE};09-Oct-2026",
        "900002;INF000000002;-;SYNTHETIC FUND;Direct Plan;IDCW;18.48;09-Oct-2026",
    ]
    nav_file.write_text("\n".join([header, *rows]) + "\n", "utf-8")
    navs = parse_navall(nav_file)
    assert navs["900001"]["nav"] is None
    assert navs["900002"]["nav"] == D("18.48")


# --- profile: "fact set gross_salary_annual abc" was stored and only failed later -------------


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("abc", "is an amount in rupees"),
        ("1,300,000", "is an amount in rupees"),
        ("true", "is an amount in rupees"),
        ("-5", "cannot be negative"),
        (HUGE, "out of range"),
    ],
)
def test_lm_money_facts_are_validated_when_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str, message: str
) -> None:
    _lm_with(tmp_path, "finance")
    capsys.readouterr()
    assert _lm(tmp_path, "fact", "set", "gross_salary_annual", value, "--category", "finance") == 2
    assert message in capsys.readouterr().err
    assert _lm(tmp_path, "fact", "set", "gross_salary_annual", "1300000", "--category", "finance") == 0
    capsys.readouterr()
    assert _lm(tmp_path, "fact", "correct", "gross_salary_annual", value) == 2
    assert message in capsys.readouterr().err
    assert _lm(tmp_path, "fact", "list") == 0
    assert "gross_salary_annual\tfinance\tUSER_ESTIMATE\t13,00,000" in capsys.readouterr().out


def test_money_fact_validation_in_the_library(tmp_path: Path) -> None:
    prof = Profile(PrivateStore(tmp_path / "home").ensure(), clock=lambda: NOW)
    prof.consent.grant("finance", purpose="t")
    with pytest.raises(ValueError, match="is an amount in rupees"):
        prof.set_fact("essential_spend_monthly", "lots", "finance", "USER_ESTIMATE", "x")
    with pytest.raises(ValueError, match="is an amount in rupees"):
        prof.set_fact("essential_spend_monthly", 1.5, "finance", "USER_ESTIMATE", "x")
    prof.set_fact("essential_spend_monthly", None, "finance", "UNKNOWN", "not sure")  # still allowed
    prof.set_fact("employer", "Acme", "finance", "USER_ESTIMATE", "x")  # only money keys are typed


# --- habits: "habits ics" always exported an empty calendar (no CLI could add a habit) --------


def test_lm_habits_cli_add_checkin_list_retire_ics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _lm_with(tmp_path)
    capsys.readouterr()
    assert _lm(tmp_path, "habits", "add", "walk", "--name", "Walk") == 2
    assert "life-mentor consent grant lifestyle" in capsys.readouterr().err

    assert _lm(tmp_path, "consent", "grant", "lifestyle", "--purpose", "habits") == 0
    assert _lm(tmp_path, "habits", "list") == 0
    assert "no habits yet" in capsys.readouterr().out
    lm = ("--as-of", "2026-11-10", "habits")
    assert _lm(tmp_path, *lm, "add", "walk", "--name", "Walk 20 min ☕", "--start", "2026-11-01") == 0
    assert _lm(tmp_path, *lm, "add", "read", "--name", "Read") == 0
    assert _lm(tmp_path, *lm, "add", "stretch", "--name", "Stretch") == 0
    capsys.readouterr()
    assert _lm(tmp_path, *lm, "add", "fourth", "--name", "Too many") == 1  # 3 active: HOLD
    assert capsys.readouterr().out.startswith("HOLD: 3 habits are already active")
    assert _lm(tmp_path, *lm, "add", "walk", "--name", "Again") == 2  # ids are never reused
    assert _lm(tmp_path, *lm, "checkin", "walk", "--day", "2026-11-09") == 0
    assert _lm(tmp_path, *lm, "checkin", "walk", "--missed") == 0  # defaults to --as-of
    assert _lm(tmp_path, *lm, "checkin", "walk", "--day", "2026-11-09") == 2  # never overwritten
    assert _lm(tmp_path, *lm, "checkin", "walk", "--day", "2026-11-11") == 2  # future
    assert _lm(tmp_path, *lm, "retire", "stretch") == 0
    assert _lm(tmp_path, *lm, "retire", "stretch") == 2
    capsys.readouterr()

    assert _lm(tmp_path, "habits", "list") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "walk\tWalk 20 min ☕\tsince 2026-11-01\tactive\t1/2 check-ins done",
        "read\tRead\tsince 2026-11-10\tactive\t0/0 check-ins done",
        "stretch\tStretch\tsince 2026-11-10\tretired\t0/0 check-ins done",
    ]
    ics = tmp_path / "h.ics"
    assert _lm(tmp_path, "habits", "ics", "--out", str(ics)) == 0
    text = ics.read_bytes().decode("utf-8")
    assert text.count("BEGIN:VEVENT") == 2  # the retired habit is left out
    assert "SUMMARY:Habit: Walk 20 min ☕" in text and "Stretch" not in text
    assert _lm(tmp_path, "journal", "verify") == 0
