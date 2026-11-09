"""LM05 decision engine: the plan's 8-step salary-based affordability analysis.

Worked example (hand-verified, see docstring of ``test_worked_example_rows``):
a Rs 60,000 course, take-home Rs 80,000/month, essential spend Rs 55,000/month,
emergency fund Rs 3,00,000 against a 6-month target.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.core.identity import Identity
from life_os.life_mentor.decision_engine import (
    BASELINE_NAME,
    CRITERIA,
    DEFAULT_RATE_ID,
    RATES_PACK_PATH,
    Option,
    Situation,
    analyze_decision,
    benefits,
    load_rates_pack,
    record_confirmation,
    situation_from_profile,
)
from life_os.life_mentor.finance import load_tax_pack, take_home
from life_os.life_mentor.profile import Fact, Profile
from life_os.life_mentor.research import load_rule_pack
from life_os.life_mentor.security import PrivateStore, find_forbidden

D = Decimal
AS_OF = date(2026, 10, 11)
NOW = "2026-10-11T00:00:00Z"
ME = Identity("tester", "Test User", "")


@pytest.fixture(scope="module")
def rates():
    return load_rates_pack()


def _situation(**over: Any) -> Situation:
    kw: dict[str, Any] = {
        "take_home_monthly": D(80000),
        "essential_monthly": D(55000),
        "emergency_fund_balance": D(300000),
        "emergency_fund_months": 6,
        "goals": ("goal_upskill", "goal_house"),
    }
    kw.update(over)
    return Situation(**kw)


def _course(**over: Any) -> Option:
    kw: dict[str, Any] = {
        "name": "data_course",
        "kind": "course",
        "upfront_cost": D(60000),
        "recurring_monthly_cost": D(0),
        "horizon_months": 12,
        "benefit_monthly": benefits(
            D(0),
            D(5000),
            D(10000),
            basis="salary uplift after course (own estimate)",
            evidence_class="HYPOTHESIS",
        ),
        "downside_loss": D(60000),
        "goal_tags": ("goal_upskill",),
    }
    kw.update(over)
    return Option(**kw)


def _run(rates, option: Option | None = None, **kw: Any) -> dict:
    kw.setdefault("situation", _situation())
    rep = analyze_decision(option or _course(), rates=rates, as_of=AS_OF, **kw)
    assert find_forbidden(rep) == []
    return rep


def _opt(rep: dict, name: str) -> dict:
    [o] = [o for o in rep["result"]["options"] if o["name"] == name]
    return o


def _row(option: dict, step: int) -> dict:
    [r] = [r for r in option["criteria"] if r["step"] == step]
    return r


def _keys(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _keys(v)
    return out


# --- rule pack -------------------------------------------------------------


def test_rates_pack_file(rates) -> None:
    assert RATES_PACK_PATH.name == "in_low_risk_rates_2026q3.json"
    raw = json.loads(RATES_PACK_PATH.read_text(encoding="utf-8"))
    vals = {v["id"]: v for v in raw["values"]}
    assert set(vals) >= {"ppf", "post_office_time_deposit_1y", "post_office_savings_deposit"}
    for v in raw["values"]:
        assert v["source_url"] == "https://dea.gov.in/budget-division/475"
        assert v["source_tier"] == "secondary"
        assert v["retrieved_on"] == "2026-10-11"
        assert v["verified_by_human"] is False
        assert (v["effective_from"], v["effective_to"]) == ("2026-10-01", "2026-12-31")
    assert rates.get("ppf").value == D("7.1")
    assert rates.get("post_office_time_deposit_1y").value == D("6.9")
    assert rates.get("post_office_savings_deposit").value == D("4.0")
    assert DEFAULT_RATE_ID == "post_office_time_deposit_1y"


# --- worked example --------------------------------------------------------


def test_worked_example_rows(rates) -> None:
    """Hand-verified:
    1 total = 60,000 + 0 x 12 = 60,000
    2 recurring 0% of 80,000; upfront = 60,000 / 80,000 = 0.75 months of take-home
    3 surplus 80,000 - 55,000 - 0 = 25,000; EF target 6 x 55,000 = 3,30,000;
      EF after 3,00,000 - 60,000 = 2,40,000 -> shortfall 90,000 -> HOLD;
      cover 2,40,000 / 55,000 = 4.36 -> 4.4 months
    4 benefit x 12 = 0 / 60,000 / 1,20,000; net of cost -60,000 / 0 / 60,000
    5 downside 60,000 = 0.75 months; opportunity cost 60,000 x ((1 + 6.9/1200)^12 - 1)
      = 4,273.4697... -> 4,273.47 (float cross-check 4273.4697)
    6 payback: conservative never; base 60,000/5,000 = 12; optimistic 6
    7 vs keeping the cash at 6.9%: net - 4,273.47 = -64,273.47 / -4,273.47 / 55,726.53
    8 goal_upskill matches a stated goal
    """
    rep = _run(rates)
    course = _opt(rep, "data_course")
    assert [r["step"] for r in course["criteria"]] == list(range(1, 9))
    assert [r["criterion"] for r in course["criteria"]] == list(CRITERIA)

    r1 = _row(course, 1)["values"]
    assert (r1["upfront_cost"], r1["recurring_total"], r1["total_cost"]) == (D(60000), D(0), D(60000))
    r2 = _row(course, 2)["values"]
    assert r2["recurring_percent_of_take_home"] == D("0.00")
    assert r2["upfront_months_of_take_home"] == D("0.75")
    row3 = _row(course, 3)
    r3 = row3["values"]
    assert (r3["surplus_before"], r3["surplus_after"]) == (D(25000), D(25000))
    assert (r3["emergency_fund_target"], r3["emergency_fund_after"]) == (D(330000), D(240000))
    assert r3["emergency_fund_shortfall"] == D(90000)
    assert r3["months_of_cover_after"] == D("4.4")
    assert row3["disposition"] == HOLD
    r4 = _row(course, 4)["values"]
    assert r4["benefit_total"] == {"conservative": D(0), "base": D(60000), "optimistic": D(120000)}
    assert r4["net_benefit"] == {"conservative": D(-60000), "base": D(0), "optimistic": D(60000)}
    assert "HYPOTHESIS" in _row(course, 4)["reason"]
    r5 = _row(course, 5)["values"]
    assert r5["downside_months_of_take_home"] == D("0.75")
    assert r5["low_risk_rate_percent"] == D("6.9")
    assert r5["opportunity_cost"] == D("4273.47")
    assert _row(course, 6)["values"]["payback_months"] == {
        "conservative": "never",
        "base": 12,
        "optimistic": 6,
    }
    assert _row(course, 7)["values"]["excess_over_low_risk"] == {
        "conservative": D("-64273.47"),
        "base": D("-4273.47"),
        "optimistic": D("55726.53"),
    }
    r8 = _row(course, 8)
    assert r8["values"]["matched_goals"] == ["goal_upskill"] and r8["disposition"] == SHIP

    assert course["gate"]["disposition"] == HOLD
    assert rep["gate"]["disposition"] == HOLD  # the proposal itself


def test_worked_example_baseline_and_recommendation(rates) -> None:
    rep = _run(rates)
    base = _opt(rep, BASELINE_NAME)
    assert base["is_baseline"] is True
    assert len(base["criteria"]) == 8
    assert _row(base, 1)["values"]["total_cost"] == D(0)
    assert _row(base, 4)["values"]["benefit_total"]["base"] == D("4273.47")
    assert _row(base, 3)["disposition"] == SHIP  # already below target, not made worse
    assert "below" in _row(base, 3)["reason"]
    assert base["gate"]["disposition"] == SHIP
    rec = rep["result"]["recommendation"]
    assert rec["status"] == "recommend" and rec["option"] == BASELINE_NAME
    conds = " ".join(rep["result"]["change_conditions"])
    assert "3,90,000" in conds  # EF target 3,30,000 + upfront 60,000
    assert "5,000" in conds  # break-even monthly benefit 60,000 / 12


def test_report_contract(rates) -> None:
    rep = _run(rates)
    assert set(rep) >= {"calculator", "inputs", "rules_used", "assumptions", "result", "gate", "decision_id"}
    assert not any("score" in k.casefold() or "rank" in k.casefold() for k in _keys(rep))
    [cite] = rep["rules_used"]
    assert cite["id"] == DEFAULT_RATE_ID and cite["unverified"] is True
    assert cite["source_url"] == "https://dea.gov.in/budget-division/475"
    assert rep["result"]["execution"] == {
        "executed": False,
        "requires_human_confirmation": True,
        "note": rep["result"]["execution"]["note"],
    }
    steps = " ".join(rep["result"]["next_steps"]).casefold()
    assert "confirm" in steps and "never" in steps
    text = " ".join(rep["assumptions"]).casefold()
    assert "not promises" in text and "6 months" in text
    assert "UNVERIFIED" in " ".join(r["reason"] for r in rep["gate"]["reasons"])
    comp = rep["result"]["comparison"]
    assert set(comp) == set(CRITERIA)
    assert set(comp["total_cost"]) == {"data_course", BASELINE_NAME}


# --- QA scenarios from the plan --------------------------------------------


def test_option_plus_alternative_gives_eight_rows_each(rates) -> None:
    alt = _course(
        name="online_course",
        upfront_cost=D(15000),
        benefit_monthly=benefits(D(0), D(2000), D(4000), basis="peer reports", evidence_class="EMPIRICAL"),
        downside_loss=D(15000),
    )
    rep = _run(rates, alternatives=[alt], situation=_situation(emergency_fund_balance=D(450000)))
    names = [o["name"] for o in rep["result"]["options"]]
    assert names == ["data_course", "online_course", BASELINE_NAME]
    for o in rep["result"]["options"]:
        assert len(o["criteria"]) == 8
        assert o["gate"]["disposition"] in (SHIP, HOLD, BLOCK)
        assert o["gate"]["reasons"]
    # Review fix #1: the proposal's base case (-4,273.47 vs keeping the cash) now HOLDs
    # at step 7, so the report (the proposal's disposition) is HOLD, not SHIP.
    assert rep["gate"]["disposition"] == HOLD
    assert _row(_opt(rep, "data_course"), 7)["disposition"] == HOLD
    assert _opt(rep, "online_course")["gate"]["disposition"] == SHIP
    assert rep["gate"]["reasons"]
    rec = rep["result"]["recommendation"]
    # Only online_course clears every check (and does better than keeping the cash).
    assert rec["status"] == "recommend" and rec["option"] == "online_course"
    assert "goal_upskill" in rec["text"]


def test_negative_surplus_blocks(rates) -> None:
    sub = _course(
        name="gym_premium",
        kind="subscription",
        upfront_cost=D(0),
        recurring_monthly_cost=D(30000),
        goal_tags=(),
    )
    rep = _run(rates, sub)
    opt = _opt(rep, "gym_premium")
    assert _row(opt, 3)["disposition"] == BLOCK
    assert _row(opt, 3)["values"]["surplus_after"] == D(-5000)
    assert rep["gate"]["disposition"] == BLOCK
    assert any("surplus" in c for c in rep["result"]["change_conditions"])


def test_unknown_take_home_holds_insufficient_information(rates) -> None:
    th = Fact("take_home_monthly", None, "finance", "UNKNOWN", "not sure", NOW)
    rep = _run(rates, situation=_situation(take_home_monthly=th))
    assert rep["gate"]["disposition"] == HOLD
    assert _row(_opt(rep, "data_course"), 2)["disposition"] == HOLD
    assert _opt(rep, BASELINE_NAME)["gate"]["disposition"] == HOLD
    rec = rep["result"]["recommendation"]
    assert rec["status"] == "insufficient_information" and rec["option"] is None
    assert "take_home_monthly" in rec["missing"]
    assert "insufficient information" in rec["text"].casefold()


# --- per-step gates --------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "step", "disp"),
    [
        ({"upfront_cost": None}, 1, HOLD),
        ({"upfront_cost": D(-1)}, 1, BLOCK),
        ({"horizon_months": None}, 1, HOLD),
        ({"horizon_months": 0}, 1, BLOCK),
        ({"benefit_monthly": None}, 4, HOLD),
        ({"benefit_monthly": benefits(D(1), D(2), D(3), basis="", evidence_class="EMPIRICAL")}, 4, HOLD),
        ({"benefit_monthly": benefits(D(3), D(2), D(1), basis="x", evidence_class="EMPIRICAL")}, 4, BLOCK),
        ({"benefit_monthly": benefits(D(-1), D(2), D(3), basis="x", evidence_class="EMPIRICAL")}, 4, BLOCK),
        ({"benefit_monthly": benefits(D(1), D(2), D(3), basis="x", evidence_class="RUMOUR")}, 4, BLOCK),
        ({"downside_loss": None}, 5, HOLD),
        ({"downside_loss": D(-5)}, 5, BLOCK),
        ({"goal_tags": ("goal_yacht",)}, 8, HOLD),
    ],
)
def test_step_gates(rates, over: dict, step: int, disp: str) -> None:
    rep = _run(rates, _course(**over), situation=_situation(emergency_fund_balance=D(500000)))
    opt = _opt(rep, "data_course")
    assert _row(opt, step)["disposition"] == disp, _row(opt, step)
    assert opt["gate"]["disposition"] == disp


def test_untagged_option_is_flagged_not_held(rates) -> None:
    rep = _run(rates, _course(goal_tags=()), situation=_situation(emergency_fund_balance=D(500000)))
    row = _row(_opt(rep, "data_course"), 8)
    assert row["disposition"] == SHIP and "no stated goal" in row["reason"]


@pytest.mark.parametrize("goals", [None, ()])
def test_no_goals_holds(rates, goals: Any) -> None:
    rep = _run(rates, situation=_situation(goals=goals, emergency_fund_balance=D(500000)))
    assert _row(_opt(rep, "data_course"), 8)["disposition"] == HOLD
    assert rep["result"]["recommendation"]["status"] != "recommend"


def test_payback_beyond_horizon_and_never(rates) -> None:
    opt = _course(
        recurring_monthly_cost=D(1000),
        benefit_monthly=benefits(D(1000), D(3000), D(11000), basis="x", evidence_class="EMPIRICAL"),
    )
    rep = _run(rates, opt, situation=_situation(emergency_fund_balance=D(500000)))
    vals = _row(_opt(rep, "data_course"), 6)["values"]
    assert vals["payback_months"] == {"conservative": "never", "base": 30, "optimistic": 6}
    assert vals["within_horizon"] == {"conservative": False, "base": False, "optimistic": True}


def test_recurring_cost_percent_and_opportunity_cost(rates) -> None:
    sub = _course(
        name="sub",
        kind="subscription",
        upfront_cost=D(0),
        recurring_monthly_cost=D(2000),
        horizon_months=12,
        goal_tags=(),
    )
    rep = _run(rates, sub)
    opt = _opt(rep, "sub")
    assert _row(opt, 2)["values"]["recurring_percent_of_take_home"] == D("2.50")
    assert _row(opt, 1)["values"]["total_cost"] == D(24000)
    # 2000 at start of each month for 12 months at 6.9%/12, FV minus paid.
    i = D("6.9") / 1200
    fv = sum((D(2000) * (1 + i) ** k for k in range(1, 13)), D(0))
    assert _row(opt, 5)["values"]["opportunity_cost"] == (fv - 24000).quantize(D("0.01"))


def test_stale_or_out_of_period_rates_hold(rates) -> None:
    rep = analyze_decision(
        _course(),
        situation=_situation(emergency_fund_balance=D(500000)),
        rates=rates,
        as_of=date(2027, 1, 15),
    )
    assert _row(_opt(rep, "data_course"), 5)["disposition"] == HOLD
    assert rep["gate"]["disposition"] == HOLD
    raw = json.loads(RATES_PACK_PATH.read_text(encoding="utf-8"))
    for v in raw["values"]:
        v["retrieved_on"] = "2026-10-01"
        v["refresh_cadence_days"] = 5
    rep = analyze_decision(
        _course(),
        situation=_situation(emergency_fund_balance=D(500000)),
        rates=load_rule_pack(raw),
        as_of=AS_OF,
    )
    assert any("stale" in r["reason"] for r in _row(_opt(rep, "data_course"), 5)["values"]["rule_reasons"])
    assert rep["gate"]["disposition"] == HOLD


def test_unknown_rate_id_holds(rates) -> None:
    rep = _run(
        rates, situation=_situation(emergency_fund_balance=D(500000)), opportunity_rate_id="nps_tier_1"
    )
    assert _row(_opt(rep, "data_course"), 5)["disposition"] == HOLD


def test_duplicate_or_reserved_names_block(rates) -> None:
    rep = _run(rates, alternatives=[_course()])
    assert rep["gate"]["disposition"] == BLOCK
    rep = _run(rates, alternatives=[_course(name=BASELINE_NAME)])
    assert rep["gate"]["disposition"] == BLOCK


def test_floats_rejected(rates) -> None:
    with pytest.raises(TypeError):
        _run(rates, _course(upfront_cost=60000.0))
    with pytest.raises(TypeError):
        _run(rates, situation=_situation(take_home_monthly=80000.0))


def test_option_validation() -> None:
    with pytest.raises(ValueError):
        _course(kind="lottery")
    with pytest.raises(ValueError):
        _course(name="  ")


def test_no_option_clears_lists_blockers(rates) -> None:
    sit = _situation(take_home_monthly=D(50000))  # essentials 55,000 -> deficit for all
    rep = _run(rates, situation=sit)
    rec = rep["result"]["recommendation"]
    assert rec["status"] == "no_option_clears" and rec["option"] is None
    assert "data_course" in rec["text"] and BASELINE_NAME in rec["text"]


def test_report_has_no_promise_language(rates) -> None:
    rep = _run(rates)
    blob = json.dumps(rep, default=str).casefold()
    assert "guarantee" not in blob and "risk-free" not in blob


# --- profile + LM04 integration --------------------------------------------


def _profile(tmp_path: Path) -> Profile:
    prof = Profile(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    for cat in ("finance", "goals"):
        prof.consent.grant(cat, purpose="decision")
    return prof


def test_situation_from_profile_uses_take_home_report(tmp_path: Path, rates) -> None:
    prof = _profile(tmp_path)
    prof.set_fact("essential_spend_monthly", D(55000), "finance", "VERIFIED", "bank")
    prof.set_fact("emergency_fund_balance", D(300000), "finance", "VERIFIED", "statement")
    prof.set_fact("goal_upskill", "learn data science", "goals", "USER_ESTIMATE", "stated")
    th = take_home(
        load_tax_pack(), as_of=AS_OF, regime="new", gross_salary=D(1300000), payroll_deductions=D(21600)
    )
    sit = situation_from_profile(prof, take_home_report=th)
    assert sit.goals == ("goal_upskill",)
    assert sit.questions == ()
    rep = analyze_decision(_course(), situation=sit, rates=rates, as_of=AS_OF)
    assert rep["inputs"]["take_home_monthly"]["value"] == D("104366.67")
    assert rep["inputs"]["take_home_monthly"]["provenance"] == "DERIVED"
    ids = {r["id"] for r in rep["rules_used"]}
    assert DEFAULT_RATE_ID in ids and "new_regime_slabs" in ids


def test_situation_from_profile_missing_facts_hold_with_questions(tmp_path: Path, rates) -> None:
    prof = Profile(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    prof.consent.grant("finance", purpose="decision")
    sit = situation_from_profile(prof)
    keys = {q["key"] for q in sit.questions}
    assert {"take_home_monthly", "essential_spend_monthly", "emergency_fund_balance"} <= keys
    assert sit.goals is None
    rep = analyze_decision(_course(), situation=sit, rates=rates, as_of=AS_OF)
    assert rep["gate"]["disposition"] == HOLD
    assert rep["result"]["recommendation"]["status"] == "insufficient_information"
    assert rep["result"]["questions"]


def test_held_take_home_report_becomes_unknown(tmp_path: Path, rates) -> None:
    prof = _profile(tmp_path)
    th = take_home(load_tax_pack(), as_of=AS_OF, regime="new", gross_salary=D(1300000))
    assert th["gate"]["disposition"] == HOLD
    sit = situation_from_profile(prof, take_home_report=th)
    rep = analyze_decision(_course(), situation=sit, rates=rates, as_of=AS_OF)
    assert _row(_opt(rep, "data_course"), 2)["disposition"] == HOLD


# --- journal & human confirmation ------------------------------------------


def test_decision_journaled_without_values(tmp_path: Path, rates) -> None:
    store = PrivateStore(tmp_path / "home")
    rep = _run(rates, store=store)
    j = store.journal("decisions/journal.jsonl")
    [entry] = j.entries()
    ev = entry["event"]
    assert ev["event"] == "decision_analyzed"
    assert ev["decision_id"] == rep["decision_id"]
    assert ev["options"] == ["data_course", BASELINE_NAME]
    assert ev["dispositions"] == {"data_course": HOLD, BASELINE_NAME: SHIP}
    raw = j.path.read_text(encoding="utf-8")
    for money in ("60000", "80000", "55000", "300000", "4273"):
        assert money not in raw
    assert j.verify() == (True, None)
    assert rep["journal"] == {"recorded": True, "index": 0}


def test_no_store_means_no_journal(rates) -> None:
    assert _run(rates)["journal"] == {"recorded": False, "index": None}


def test_record_confirmation_never_executes(tmp_path: Path, rates) -> None:
    store = PrivateStore(tmp_path / "home")
    rep = _run(rates, store=store)
    entry = record_confirmation(store, rep, "data_course", identity=ME)
    ev = entry["event"]
    assert ev["event"] == "decision_confirmed_by_human"
    assert ev["executed"] is False and ev["override_of_hold"] is True
    assert ev["option"] == "data_course" and ev["decision_id"] == rep["decision_id"]
    with pytest.raises(ValueError):
        record_confirmation(store, rep, "nonexistent", identity=ME)


def test_cannot_confirm_blocked_option(tmp_path: Path, rates) -> None:
    store = PrivateStore(tmp_path / "home")
    sub = _course(name="gym_premium", kind="subscription", upfront_cost=D(0), recurring_monthly_cost=D(30000))
    rep = _run(rates, sub, store=store)
    with pytest.raises(ValueError):
        record_confirmation(store, rep, "gym_premium", identity=ME)
