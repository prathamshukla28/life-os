"""Regression tests (part 2): store/journal, decisions, CLI, redaction, health, lifestyle.

Each test names the bug it pins. See CHANGELOG.md (0.1.0, "Fixed").
"""

from __future__ import annotations

import csv
import json
import multiprocessing
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from life_os.core.gate import BLOCK, HOLD
from life_os.core.identity import Identity
from life_os.life_mentor.cli import main as lm_main
from life_os.life_mentor.decision_engine import (
    Option,
    Situation,
    analyze_decision,
    benefits,
    load_rates_pack,
    record_confirmation,
)
from life_os.life_mentor.health import load_interactions
from life_os.life_mentor.health.nutrition import load_nutrition_refs, protein_target
from life_os.life_mentor.health.safety import health_safety
from life_os.life_mentor.health.usda import UsdaImportError, build_subset
from life_os.life_mentor.lifestyle import SleepEntry, analyze_sleep, load_sleep_refs
from life_os.life_mentor.lifestyle.exercise import progression
from life_os.life_mentor.profile import ConsentLedger, Profile
from life_os.life_mentor.security import Journal, JournalTampered, PrivateStore, redact
from life_os.life_mentor.tracking import MetricLog, review

AS_OF = date(2026, 10, 11)
NOW = "2026-10-11T00:00:00Z"
ME = Identity("tester", "Test User", "")


# --- journal.py: concurrent appends interleaved and permanently broke the chain ----------------


def _append_many(path: str, n: int) -> None:
    j = Journal(Path(path))
    for i in range(n):
        j.append({"event": "x", "i": i})


def test_journal_concurrent_appends_keep_chain_intact(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_many, args=(str(path), 15)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    j = Journal(path)
    assert j.verify() == (True, None)
    assert len(j.entries()) == 60


# --- profile/store.py: a fact was written BEFORE the (tampered) journal refused --------------


def test_profile_write_refused_when_journal_tampered(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "lm")
    ConsentLedger(store, clock=lambda: NOW).grant("finance", purpose="test")
    prof = Profile(store, clock=lambda: NOW)
    prof.set_fact("k1", D(1), "finance", "VERIFIED", "payslip")
    (store.root / "profile" / "journal.jsonl.head").unlink()
    with pytest.raises(JournalTampered):
        prof.set_fact("k2", D(2), "finance", "VERIFIED", "payslip")
    with pytest.raises(JournalTampered):
        prof.correct("k1", D(999), "VERIFIED", "payslip")
    facts = json.loads((store.root / "profile" / "facts.json").read_text(encoding="utf-8"))["facts"]
    assert set(facts) == {"k1"}
    assert "999" not in json.dumps(facts)


# --- confirm.py: an option could be confirmed although the WHOLE report was BLOCK -----------


def _opt(name: str) -> Option:
    return Option(
        name=name,
        kind="course",
        upfront_cost=D(10000),
        recurring_monthly_cost=D(0),
        horizon_months=12,
        benefit_monthly=benefits(D(0), D(1000), D(2000), basis="own estimate", evidence_class="HYPOTHESIS"),
        downside_loss=D(10000),
        goal_tags=(),
    )


def _situation() -> Situation:
    return Situation(
        take_home_monthly=D(80000),
        essential_monthly=D(55000),
        emergency_fund_balance=D(300000),
        emergency_fund_months=6,
        goals=(),
    )


def test_cannot_confirm_option_of_blocked_report(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "lm")
    rep = analyze_decision(
        _opt("A"),
        situation=_situation(),
        rates=load_rates_pack(),
        as_of=AS_OF,
        alternatives=[_opt("A")],
        store=store,
    )
    assert rep["gate"]["disposition"] == BLOCK
    with pytest.raises(ValueError, match="as a whole is BLOCKed"):
        record_confirmation(store, rep, "A", identity=ME)


def test_absurd_horizon_blocks_instead_of_decimal_overflow() -> None:
    opt = Option(
        name="x",
        kind="subscription",
        upfront_cost=D(0),
        recurring_monthly_cost=D(100),
        horizon_months=10**9,
        benefit_monthly=None,
        downside_loss=None,
        goal_tags=(),
    )
    rep = analyze_decision(opt, situation=_situation(), rates=load_rates_pack(), as_of=AS_OF)
    assert rep["gate"]["disposition"] == BLOCK


# --- cli.py: tracebacks, temp files with PII left behind, unvalidated --generated-on --------


def _lm(tmp_path: Path, *args: str) -> int:
    return lm_main(["--home", str(tmp_path / "lm"), *args])


def test_cli_export_onto_directory_is_clean_error_and_leaves_no_temp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _lm(tmp_path, "init") == 0
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    code = _lm(tmp_path, "profile", "export", "--out", str(outdir), "--force")
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize(
    "spec",
    [
        {"option": {"name": "a", "kind": "spend", "benefit_monthly": 5}},
        {"option": {"name": "a", "kind": "spend"}, "alternatives": ["b"]},
        {"option": {"name": "a", "kind": "spend"}, "alternatives": "b"},
    ],
)
def test_cli_decide_malformed_options_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], spec: dict[str, Any]
) -> None:
    assert _lm(tmp_path, "init") == 0
    f = tmp_path / "opt.json"
    f.write_text(json.dumps(spec), encoding="utf-8")
    assert _lm(tmp_path, "decide", str(f)) == 2
    assert "error:" in capsys.readouterr().err


def test_cli_generated_on_must_be_a_date(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _lm(tmp_path, "--generated-on", "1|2|3", "init")
    assert exc.value.code == 2


# --- redact.py -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("pan abcde1234f", "abcde1234f"),
        ("call 98765 - 43210", "43210"),
        ("https://x.in/?phone=98765 43210", "43210"),
        ("https://wa.me/919876543210", "9876543210"),
        ("mail o'brien@example.com", "brien@"),
        ("mail a.b@例え.jp", "例え"),
    ],
)
def test_redact_catches_previous_misses(text: str, leak: str) -> None:
    assert leak not in redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-11/2026-10-12",
        "2026-10-11 2026-10-12",
        "Total 12,592.20.\n- 100001 FUND",
        "/item/9789240015128",
    ],
)
def test_redact_leaves_dates_amounts_and_paths(text: str) -> None:
    assert redact(f"see https://who.int{text}" if text.startswith("/") else text) == (
        f"see https://who.int{text}" if text.startswith("/") else text
    )


# --- health/safety.py: hyphens / doubled spaces hid a cited interaction --------------------------


@pytest.mark.parametrize(
    ("meds", "conds", "flag"),
    [
        (["ACE-inhibitor (ramipril)"], [], "ace_inhibitor_potassium"),
        (["ace  inhibitor"], [], "ace_inhibitor_potassium"),
        (["ACE inhibitors"], [], "ace_inhibitor_potassium"),
        ([], ["kidney-disease stage 3"], "kidney_disease_potassium"),
    ],
)
def test_safety_matching_normalises_punctuation(meds: list[str], conds: list[str], flag: str) -> None:
    rep = health_safety(medications=meds, conditions=conds, interactions=load_interactions(), as_of=AS_OF)
    assert flag in {f["id"] for f in rep["result"]["flags"]}


# --- finance/inputs.py: NaN / Infinity raised out of every calculator --------------------------


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity")])
def test_non_finite_input_blocks(bad: D) -> None:
    rep = protein_target(body_weight_kg=bad, refs=load_nutrition_refs(), as_of=AS_OF)
    assert rep["gate"]["disposition"] == BLOCK


# --- exercise.py: a non-integer current value could SHIP a stalled / lower plan ---------------


@pytest.mark.parametrize(("cur", "pct"), [(D("9.99"), D(5)), (D("95.5"), D(1))])
def test_progression_stall_holds(cur: D, pct: D) -> None:
    rep = progression(
        current_weekly_minutes=cur, target_weekly_minutes=150, weeks=6, max_increase_percent=pct
    )
    assert rep["gate"]["disposition"] == HOLD


# --- usda.py: a short CSV row raised AttributeError instead of UsdaImportError -----------------


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def test_usda_short_row_is_import_error(tmp_path: Path) -> None:
    _write(
        tmp_path / "nutrient.csv",
        ["id", "name", "unit_name", "nutrient_nbr", "rank"],
        [["1003", "Protein", "G", "203", "600"]],
    )
    _write(
        tmp_path / "food.csv",
        ["fdc_id", "data_type", "description", "food_category_id", "publication_date"],
        [["1", "sr_legacy_food", "Rice", "20", "2019-04-01"]],
    )
    hdr = "id,fdc_id,nutrient_id,amount,data_points,derivation_id,min,max,median,footnote,min_year_acquired"
    (tmp_path / "food_nutrient.csv").write_text(f"{hdr}\n10,1,1003\n", encoding="utf-8")
    with pytest.raises(UsdaImportError):
        build_subset(
            tmp_path,
            {"rice": "1"},
            release="r",
            download_url="https://example.org/x.zip",
            sha256="0" * 64,
            retrieved_on="2026-10-01",
            dataset_name="d",
        )


# --- tracking/metrics.py ------------------------------------------------------------------------


def _log(tmp_path: Path) -> MetricLog:
    store = PrivateStore(tmp_path / "lm")
    ConsentLedger(store, clock=lambda: NOW).grant("health", purpose="test")
    return MetricLog(store, clock=lambda: NOW)


def test_review_min_points_zero_blocks_not_crash(tmp_path: Path) -> None:
    rep = review(_log(tmp_path), "sleep_hours", as_of=AS_OF, min_points=0)
    assert rep["gate"]["disposition"] == BLOCK


def test_metric_values_have_physical_bounds(tmp_path: Path) -> None:
    log = _log(tmp_path)
    with pytest.raises(ValueError):
        log.record("sleep_hours", D(30), AS_OF, provenance="USER_ESTIMATE", basis="typo")


# --- sleep.py: the "14-day" window held 15 nights ---------------------------------------------


def test_sleep_window_is_fourteen_nights() -> None:
    nights = [SleepEntry(AS_OF - timedelta(days=i), "23:00", "07:00") for i in range(15)]
    rep = analyze_sleep(nights, refs=load_sleep_refs(), as_of=AS_OF, age=30)
    assert rep["result"]["nights"] == 14
