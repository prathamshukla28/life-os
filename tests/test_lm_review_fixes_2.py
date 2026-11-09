"""Regression tests for the second review of LM06-LM10 + weekly_tracker.

One test (or parametrised test) per finding: test_<id>_... Every test failed before its fix.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.career import (
    JobPackage,
    UpliftScenario,
    compare_job_offer,
    evaluate_education,
)
from life_os.life_mentor.cli import main as lm_main
from life_os.life_mentor.decision_engine import DECISION_JOURNAL, Situation, load_rates_pack
from life_os.life_mentor.finance import income_tax, load_tax_pack
from life_os.life_mentor.health import (
    MealItem,
    amino_acid_profile,
    load_food_table,
    load_nutrition_refs,
)
from life_os.life_mentor.integrations import (
    apply_nav_valuation,
    load_mapping,
    preview_bank_csv,
    value_holdings,
)
from life_os.life_mentor.lifestyle import (
    HabitTracker,
    SleepEntry,
    analyze_sleep,
    check_activity,
    load_sleep_refs,
    progression,
)
from life_os.life_mentor.profile import ConsentLedger
from life_os.life_mentor.reports import (
    career_report,
    career_sections,
    health_report,
    life_plan_report,
)
from life_os.life_mentor.research import load_rule_pack
from life_os.life_mentor.security import ForbiddenFieldLeak, PrivateStore
from life_os.weekly_tracker.render import render
from life_os.weekly_tracker.sources.git_work import git_section, is_test_path
from life_os.weekly_tracker.sources.life_mentor import life_mentor_section
from life_os.weekly_tracker.week import IsoWeek
from tests.lm_persona import build_persona

D = Decimal
AS_OF = date(2026, 10, 11)
NOW = "2026-10-11T00:00:00Z"
FIX = Path(__file__).resolve().parent / "fixtures"
NAV = FIX / "lm_navall_sample.txt"
MAPPING = FIX / "lm_bank_mapping.json"
W41 = IsoWeek(2026, 41)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


# --- LM11: hostile repo-local git config -----------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "F",
        "GIT_AUTHOR_EMAIL": "f@example.invalid",
        "GIT_COMMITTER_NAME": "F",
        "GIT_COMMITTER_EMAIL": "f@example.invalid",
        "GIT_AUTHOR_DATE": "2026-10-06T10:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-10-06T10:00:00+00:00",
    }
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


HOSTILE = [
    ("[core]", "fsmonitor"),
    ("[core]", "pager"),
    ("[core]", "hooksPath"),
    ("[core]", "editor"),
    ("[core]", "sshCommand"),
    ("[core]", "alternateRefsCommand"),
    ("[core]", "gitProxy"),
    ("[diff]", "external"),
    ('[diff "x"]', "textconv"),
    ('[foo "y"]', "textconv"),
    ('[diff "x"]', "command"),
    ('[merge "x"]', "driver"),
    ('[filter "x"]', "clean"),
    ('[filter "x"]', "smudge"),
    ('[filter "x"]', "process"),
    ("[pager]", "log"),
    ("[uploadpack]", "packObjectsHook"),
    ("[safe]", "directory"),
    ("[include]", "path"),
    ('[includeIf "gitdir:/"]', "path"),
    ("[sequence]", "editor"),
    ("[credential]", "helper"),
    ("[CORE]", "FSMONITOR"),
]


@needs_git
@pytest.mark.parametrize(("section", "key"), HOSTILE)
def test_lm11_hostile_git_config_refused(tmp_path: Path, section: str, key: str) -> None:
    repo = _repo(tmp_path)
    pwned = tmp_path / "pwned"
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(f"{section}\n\t{key} = touch {pwned}\n")
    sec = git_section(repo, W41, tz="UTC")
    assert sec == {
        "status": "unavailable",
        "reason": "repository has a custom git config that could run commands",
    }
    assert not pwned.exists()


@needs_git
def test_lm11_worktree_config_subdir_and_gitfile(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    pwned = tmp_path / "pwned"
    (repo / ".git" / "config.worktree").write_text(f"[core]\n\tfsmonitor = touch {pwned}\n", encoding="utf-8")
    (repo / "sub").mkdir()
    assert git_section(repo / "sub", W41)["status"] == "unavailable"
    (repo / ".git" / "config.worktree").unlink()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {repo / '.git'}\n", encoding="utf-8")
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(f"[core]\n\tfsmonitor = touch {pwned}\n")
    assert git_section(linked, W41)["status"] == "unavailable"
    assert not pwned.exists()


@needs_git
def test_lm11_unparseable_config_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write("[core\n\tfsmonitor = x\n")
    assert git_section(repo, W41)["status"] == "unavailable"


@needs_git
def test_lm11_normal_repo_still_works(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write('[user]\n\tname = "Someone ; # not a comment"\n[branch "main"]\n\tremote = origin\n')
    sec = git_section(repo, W41, tz="UTC")
    assert sec["status"] == "ok" and sec["totals"]["commits"] == 1


# --- LM12: csv.Error ------------------------------------------------------------------------


def test_lm12_huge_csv_field_blocks_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    big = tmp_path / "big.csv"
    big.write_text(
        f'Txn Date,Narration,Withdrawal Amt,Deposit Amt\n01/09/2026,"{"x" * 140000}",1,\n', encoding="utf-8"
    )
    rep = preview_bank_csv(big, load_mapping(MAPPING))
    assert rep["gate"]["disposition"] == BLOCK
    assert any("could not be parsed" in r["reason"] for r in rep["gate"]["reasons"])
    home = tmp_path / "lm"
    lm_main(["--home", str(home), "init"])
    code = lm_main(["--home", str(home), "import", "bank-csv", str(big), "--mapping", str(MAPPING)])
    err = capsys.readouterr().err
    assert code == 2 and "Traceback" not in err


# --- F1: header-only / all-bad NAV file ---------------------------------------------------------


@pytest.mark.parametrize("body", ["", "900001;INF;-;BROKEN ROW;Direct;Growth;12.3\n"])
def test_f1_nav_file_without_scheme_rows_blocks(tmp_path: Path, body: str) -> None:
    path = tmp_path / "nav.txt"
    path.write_text(NAV.read_text(encoding="utf-8").split("\n", 1)[0] + "\n" + body, encoding="utf-8")
    rep = value_holdings(path, [{"scheme_code": "900001", "units": D(1)}], as_of=AS_OF)
    assert rep["gate"]["disposition"] == BLOCK
    assert any("no scheme rows" in r["reason"] for r in rep["gate"]["reasons"])


# --- F2: debit_is_negative must be a real bool ----------------------------------------------------


def test_f2_debit_is_negative_string_blocks(tmp_path: Path) -> None:
    csv_path = tmp_path / "s.csv"
    csv_path.write_text("Date,Desc,Amount\n01/09/2026,rent,-100\n", encoding="utf-8")
    mapping = {
        "date_column": "Date",
        "date_format": "%d/%m/%Y",
        "description_column": "Desc",
        "amount_column": "Amount",
        "debit_is_negative": "false",
        "category_rules": [{"contains": "rent", "category": "rent"}],
    }
    assert preview_bank_csv(csv_path, mapping)["gate"]["disposition"] == BLOCK
    mapping["debit_is_negative"] = True
    assert preview_bank_csv(csv_path, mapping)["gate"]["disposition"] == SHIP


# --- F3: comma-decimal amounts are not silently mangled ---------------------------------------------


def test_f3_european_decimal_comma_rejected(tmp_path: Path) -> None:
    csv_path = tmp_path / "s.csv"
    csv_path.write_text(
        "Txn Date,Narration,Withdrawal Amt,Deposit Amt\n"
        '01/09/2026,rent,"1.234,50",\n02/09/2026,rent,"1,04,366.67",\n',
        encoding="utf-8",
    )
    rep = preview_bank_csv(csv_path, load_mapping(MAPPING))
    r = rep["result"]
    assert r["rejected"] == [{"line": 2, "reason": "amount is not a number"}]
    assert r["months"]["2026-09"]["rent"] == D("104366.67")


# --- F4: tax year not covered by the pack -> no result -------------------------------------------------


@pytest.mark.parametrize("fy", ["FY2019-20", "FY2025-26", "FY2031-32"])
def test_f4_uncovered_tax_year_has_no_result(fy: str) -> None:
    rep = income_tax(load_tax_pack(), as_of=AS_OF, tax_year=fy, regime="new", gross_salary=D(1300000))
    assert rep["gate"]["disposition"] == HOLD and rep["result"] is None


# --- F5: string meal grams / benchmark figures do not crash --------------------------------------------


def test_f5_bad_meal_and_benchmark_values_do_not_crash(tmp_path: Path) -> None:
    prof = build_persona(tmp_path / "lm")
    prof.set_fact(
        "typical_day_meals",
        [{"food": "lentils_cooked", "grams": "two bowls"}],
        "health",
        "USER_ESTIMATE",
        "diary",
    )
    prof.set_fact(
        "salary_benchmarks",
        [
            {
                "url": "https://example.org/s",
                "title": "Survey",
                "publisher": "Ex",
                "published": "2026-08-01",
                "retrieved_on": "2026-09-01",
                "tier": "secondary",
                "evidence_class": "EMPIRICAL",
                "role": "analyst",
                "location": "Pune",
                "low": "13 lakh",
                "median": None,
                "high": D(1800000),
                "sample_notes": "n=10",
            }
        ],
        "career",
        "USER_ESTIMATE",
        "survey",
    )
    meals = next(
        s
        for s in health_report(prof, as_of=AS_OF, generated_on="x").sections
        if s.title == "Typical day meals"
    )
    assert meals.disposition == BLOCK
    bench = next(
        s
        for s in career_report(prof, as_of=AS_OF, generated_on="x").sections
        if s.title == "Salary benchmarks"
    )
    assert bench.disposition == BLOCK
    life_plan_report(prof, as_of=AS_OF, generated_on="x")


# --- F6: old regime with age through career paths ---------------------------------------------------------


def _pkg(name: str, gross: Decimal) -> JobPackage:
    return JobPackage(
        name=name,
        gross_salary_annual=gross,
        payroll_deductions_annual=D(0),
        commute_monthly=D(0),
        other_monthly_costs=D(0),
        probation_months=0,
    )


def test_f6_old_regime_offer_and_course_accept_age(tmp_path: Path) -> None:
    rep = compare_job_offer(
        _pkg("current", D(1000000)),
        _pkg("offer", D(1200000)),
        tax_pack=load_tax_pack(),
        as_of=AS_OF,
        regime="old",
        age=35,
    )
    assert rep["gate"]["disposition"] == SHIP
    sit = Situation(
        take_home_monthly=D(80000),
        essential_monthly=D(40000),
        emergency_fund_balance=D(600000),
        emergency_fund_months=6,
        goals=("goal_upskill",),
    )
    up = {
        s: UpliftScenario(D(g), "ref", "HYPOTHESIS")
        for s, g in (("conservative", 1000000), ("base", 1100000), ("optimistic", 1200000))
    }
    dec = evaluate_education(
        "c",
        upfront_cost=D(10000),
        recurring_monthly_cost=D(0),
        horizon_months=12,
        downside_loss=D(10000),
        goal_tags=("goal_upskill",),
        current_gross_salary_annual=D(1000000),
        payroll_deductions_annual=D(0),
        uplift=up,
        tax_pack=load_tax_pack(),
        regime="old",
        situation=sit,
        rates=load_rates_pack(),
        as_of=AS_OF,
        age=35,
    )
    course = dec["result"]["options"][0]
    assert course["criteria"][3]["values"]["benefit_monthly"]["base"] > 0
    prof = build_persona(tmp_path / "lm")
    prof.set_fact(
        "job_offer",
        {
            "name": "offer",
            "gross_salary_annual": D(1500000),
            "payroll_deductions_annual": D(0),
            "commute_monthly": D(0),
            "other_monthly_costs": D(0),
            "probation_months": 0,
        },
        "career",
        "USER_ESTIMATE",
        "offer letter",
    )
    sec = next(
        s for s in career_sections(prof, as_of=AS_OF, regime="old") if s.title == "Job offer comparison"
    )
    assert sec.report is not None
    assert not any("age" in r["check"] for r in sec.report["gate"]["reasons"])


# --- F7: progression that cannot progress / is not needed ----------------------------------------------


@pytest.mark.parametrize(("current", "target"), [(D(5), D(150)), (D(300), D(150)), (D(150), D(150))])
def test_f7_progression_without_progress_holds(current: Decimal, target: Decimal) -> None:
    rep = progression(current_weekly_minutes=current, target_weekly_minutes=target, weeks=8)
    assert rep["gate"]["disposition"] == HOLD and rep["result"] is None


# --- F8: NAV valuation is never overwritten ---------------------------------------------------------------


def test_f8_nav_valuation_never_overwritten(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "lm")
    ConsentLedger(store, clock=lambda: NOW).grant("finance", purpose="x")
    rep = value_holdings(NAV, [{"scheme_code": "900001", "units": D(1)}], as_of=AS_OF)
    first = apply_nav_valuation(store, rep, clock=lambda: NOW)
    assert first["result"]["written"] is True
    [path] = list((store.root / "integrations" / "mf").iterdir())
    assert rep["data_source"]["file_sha256"][:12] in path.name
    before = path.read_bytes()
    second = apply_nav_valuation(store, rep, clock=lambda: NOW)
    assert second["gate"]["disposition"] == BLOCK and second["result"]["written"] is False
    assert path.read_bytes() == before


# --- F9: missing WHO rule does not crash -----------------------------------------------------------------


def test_f9_check_activity_missing_rule_holds() -> None:
    from life_os.life_mentor.lifestyle import ACTIVITY_REFS_PATH

    raw = json.loads(ACTIVITY_REFS_PATH.read_text(encoding="utf-8"))
    raw["values"] = [v for v in raw["values"] if v["id"] != "who_adult_aerobic_vigorous_minutes_per_week"]
    rep = check_activity(
        moderate_minutes=D(100), vigorous_minutes=D(0), strength_days=2, refs=load_rule_pack(raw), as_of=AS_OF
    )
    assert rep["gate"]["disposition"] == HOLD and rep["result"] is None


# --- F10: retired habits are not shown ------------------------------------------------------------------


def test_f10_retired_habits_hidden(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "lm")
    ConsentLedger(store, clock=lambda: NOW).grant("lifestyle", purpose="x")
    t = HabitTracker(store, clock=lambda: NOW)
    t.add_habit("walk", "walk", started_on=date(2026, 10, 1))
    t.add_habit("read", "read", started_on=date(2026, 10, 1))
    t.retire_habit("read")
    sec = life_mentor_section(tmp_path / "lm", W41)
    assert [h["habit_id"] for h in sec["habits"]["habits"]] == ["walk"]


# --- F11: amino-acid percent rounded once -----------------------------------------------------------------


def test_f11_amino_percent_from_unrounded_ratio() -> None:
    table, refs = load_food_table(), load_nutrition_refs()
    rep = amino_acid_profile([MealItem("rice_white_cooked", D(200))], table=table, refs=refs, as_of=AS_OF)
    n = table.get("rice_white_cooked").nutrients
    pattern = refs.get("iaa_scoring_pattern_older_child_adolescent_adult").value
    raw_lys = n["1214"] * 1000 / n["1003"] / pattern["lys"]
    assert rep["result"]["uncorrected_percent"] == (raw_lys * 100).quantize(D("0.1"))


# --- F12: circular SD across midnight / noon ----------------------------------------------------------------


def test_f12_sleep_sd_is_circular() -> None:
    start = date(2026, 10, 1)
    entries = [
        SleepEntry(start + timedelta(days=i), "11:50" if i % 2 else "12:10", "23:50" if i % 2 else "00:10")
        for i in range(8)
    ]
    r = analyze_sleep(entries, refs=load_sleep_refs(), as_of=AS_OF, age=30)["result"]
    assert D(9) <= r["bedtime_sd_minutes"] <= D(14)
    assert D(9) <= r["wake_sd_minutes"] <= D(14)


# --- F13: git rename brace forms ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/{old.py => new.py}", True),
        ("{src => tests}/x.py", True),
        ("pkg/tests/{a => b}/c.py", True),
        ("src/{a.py => b.py}", False),
        ("{tests => src}/x.py", False),
        ("src/{ => tests}/y.py", True),
    ],
)
def test_f13_rename_paths(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


# --- F14: decisions bucketed in local time ------------------------------------------------------------------


def test_f14_decisions_use_local_day(tmp_path: Path) -> None:
    home = tmp_path / "lm"
    store = PrivateStore(home)
    ConsentLedger(store, clock=lambda: NOW).grant("finance", purpose="x")
    store.journal(DECISION_JOURNAL, clock=lambda: "2026-10-11T20:00:00Z").append(
        {"event": "decision_analyzed", "decision_id": "b" * 64, "recommendation_status": "x"}
    )
    assert life_mentor_section(home, W41, tz="Asia/Kolkata")["decisions"]["analysed"] == 0
    assert life_mentor_section(home, W41.next(), tz="Asia/Kolkata")["decisions"]["analysed"] == 1
    assert life_mentor_section(home, W41, tz="UTC")["decisions"]["analysed"] == 1


# --- F15: CLI number parsing ---------------------------------------------------------------------------------


def test_f15_cli_parsing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "lm"
    build_persona(home)
    base = ["--home", str(home), "--as-of", "2026-10-11"]
    assert lm_main([*base, "fact", "set", "half", ".5", "--category", "finance"]) == 0
    capsys.readouterr()
    lm_main([*base, "profile", "export"])
    assert json.loads(capsys.readouterr().out)["facts"]["half"]["value"] == {"$decimal": "0.5"}

    def decide(**over: Any) -> int:
        spec: dict[str, Any] = {
            "option": {
                "name": "c",
                "kind": "course",
                "upfront_cost": 1000,
                "recurring_monthly_cost": 0,
                "horizon_months": 12,
                "downside_loss": 1000,
                "goal_tags": ["goal_upskill"],
                "benefit_monthly": {
                    "conservative": 0,
                    "base": 1,
                    "optimistic": 2,
                    "basis": "x",
                    "evidence_class": "HYPOTHESIS",
                },
            }
        }
        spec["option"].update(over.pop("option", {}))
        spec.update(over)
        path = tmp_path / "o.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        code = lm_main([*base, "decide", str(path)])
        capsys.readouterr()
        return code

    assert decide(option={"horizon_months": 12.7}) == 2
    assert decide(emergency_fund_months=0) == 2


# --- F16: offer cost facts are declared requirements ---------------------------------------------------------


def test_f16_offer_cost_questions_and_names(tmp_path: Path) -> None:
    prof = build_persona(tmp_path / "lm")
    prof.set_fact(
        "job_offer",
        {
            "name": "offer",
            "gross_salary_annual": D(1500000),
            "payroll_deductions_annual": D(0),
            "commute_monthly": D(0),
            "other_monthly_costs": D(0),
            "probation_months": 0,
        },
        "career",
        "USER_ESTIMATE",
        "offer letter",
    )
    sec = next(s for s in career_sections(prof, as_of=AS_OF) if s.title == "Job offer comparison")
    assert {"commute_monthly", "other_monthly_work_costs"} <= {q["key"] for q in sec.questions}
    reasons = " ".join(r["reason"] for r in sec.report["gate"]["reasons"])  # type: ignore[index]
    assert "current.other_monthly_work_costs" in reasons
    assert "current.other_monthly_costs" not in reasons


# --- F17: sleep recency window ----------------------------------------------------------------------------------


def test_f17_sleep_recency_window() -> None:
    refs = load_sleep_refs()
    old = [SleepEntry(date(2026, 9, 1) + timedelta(days=i), "23:00", "06:30") for i in range(7)]
    rep = analyze_sleep(old, refs=refs, as_of=AS_OF, age=30)
    assert rep["gate"]["disposition"] == HOLD
    assert any("14 days" in r["reason"] for r in rep["gate"]["reasons"])
    recent = [SleepEntry(date(2026, 10, 3) + timedelta(days=i), "23:00", "06:30") for i in range(7)]
    rep = analyze_sleep(old[:3] + recent, refs=refs, as_of=AS_OF, age=30)
    assert rep["gate"]["disposition"] == SHIP and rep["result"]["nights"] == 7


# --- nits ---------------------------------------------------------------------------------------------------------


def test_nit_amfi_plan_option_redacted(tmp_path: Path) -> None:
    path = tmp_path / "nav.txt"
    header = NAV.read_text(encoding="utf-8").split("\n", 1)[0]
    path.write_text(
        f"{header}\n900001;INF1;-;FUND;Plan me@example.com;Growth;10;09-Oct-2026\n", encoding="utf-8"
    )
    rep = value_holdings(path, [{"scheme_code": "900001", "units": D(1)}], as_of=AS_OF)
    assert "me@example.com" not in json.dumps(rep, default=str)


def test_nit_weekly_render_runs_scanner() -> None:
    from tests.test_weekly_tracker import fixture_model

    model = fixture_model()
    model["planner"]["tasks"][0]["text"] = "returns are guaranteed"
    with pytest.raises(ForbiddenFieldLeak):
        render(model)
