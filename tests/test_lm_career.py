"""LM08 career: profile snapshot, job-offer comparison via LM04, education via LM05,
salary benchmarks from user-cited sources only, negotiation checklist."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.career import (
    JobPackage,
    SalaryBenchmark,
    UpliftScenario,
    benchmark_summary,
    career_snapshot,
    compare_job_offer,
    evaluate_education,
    job_package_from_profile,
    negotiation_prep,
)
from life_os.life_mentor.decision_engine import BASELINE_NAME, Situation, load_rates_pack
from life_os.life_mentor.finance import load_tax_pack, take_home
from life_os.life_mentor.profile import Profile
from life_os.life_mentor.research import SourceRecord
from life_os.life_mentor.security import PrivateStore, find_forbidden

D = Decimal
AS_OF = date(2026, 10, 11)
NOW = "2026-10-11T00:00:00Z"


@pytest.fixture(scope="module")
def tax():
    return load_tax_pack()


def _clean(rep: dict) -> str:
    assert find_forbidden(rep) == [], find_forbidden(rep)
    assert set(rep) >= {"calculator", "inputs", "rules_used", "assumptions", "result", "gate"}
    blob = json.dumps(rep, default=str).casefold()
    assert "guarantee" not in blob
    return rep["gate"]["disposition"]


def _pkg(name: str, gross: Any, **over: Any) -> JobPackage:
    kw: dict[str, Any] = {
        "name": name,
        "gross_salary_annual": gross,
        "payroll_deductions_annual": D(0),
        "commute_monthly": D(0),
        "other_monthly_costs": D(0),
        "probation_months": 0,
    }
    kw.update(over)
    return JobPackage(**kw)


def _th_monthly(tax, gross: Decimal) -> Decimal:
    rep = take_home(tax, as_of=AS_OF, regime="new", gross_salary=gross, payroll_deductions=D(0))
    return rep["result"]["take_home_monthly"]


# --- job offer comparison (worked example) --------------------------------------------


def test_worked_example_12l_vs_15l(tax) -> None:
    """Hand check (new regime FY2026-27):
    12,00,000 - 75,000 = 11,25,000 taxable -> 87A rebate -> tax 0 -> 1,00,000.00/month.
    15,00,000 - 75,000 = 14,25,000 taxable -> 20,000 + 40,000 + 2,25,000 x 15% (33,750)
    = 93,750 (above 12L, excess 2,25,000 > 93,750, so no marginal relief) + 4% cess
    = 97,500 -> (15,00,000 - 97,500) / 12 = 1,16,875.00/month."""
    rep = compare_job_offer(
        _pkg("current", D(1200000)), _pkg("offer", D(1500000)), tax_pack=tax, as_of=AS_OF, regime="new"
    )
    assert _clean(rep) == SHIP
    r = rep["result"]
    cur, off = r["packages"]["current"], r["packages"]["offer"]
    assert cur["income_tax_annual"] == D(0) and off["income_tax_annual"] == D(97500)
    assert cur["take_home_monthly"] == D("100000.00") == _th_monthly(tax, D(1200000))
    assert off["take_home_monthly"] == D("116875.00") == _th_monthly(tax, D(1500000))
    assert r["difference_monthly"]["take_home"] == D("16875.00")
    assert r["difference_monthly"]["net_after_work_costs"] == D("16875.00")
    assert [row["criterion"] for row in r["criteria"]] == [
        "take_home_monthly",
        "income_tax_annual",
        "work_costs_monthly",
        "net_after_work_costs_monthly",
        "difference_vs_current",
        "probation_and_risk",
    ]
    assert r["batna"]["option"] == "current"
    assert "score" not in json.dumps(r, default=str)


def test_offer_with_commute_and_probation(tax) -> None:
    rep = compare_job_offer(
        _pkg("current", D(1200000)),
        _pkg(
            "offer",
            D(1500000),
            commute_monthly=D(6000),
            other_monthly_costs=D(2000),
            probation_months=6,
            notes=("variable pay not confirmed",),
        ),
        tax_pack=tax,
        as_of=AS_OF,
        regime="new",
    )
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["difference_monthly"]["net_after_work_costs"] == D("8875.00")
    risk = next(row for row in r["criteria"] if row["criterion"] == "probation_and_risk")
    assert "6-month probation" in risk["values"]["offer"]
    assert "variable pay not confirmed" in json.dumps(risk)


@pytest.mark.parametrize(
    ("over", "disp"),
    [
        ({"commute_monthly": None}, HOLD),
        ({"gross_salary_annual": None}, HOLD),
        ({"gross_salary_annual": D(-1)}, BLOCK),
        ({"probation_months": -1}, BLOCK),
    ],
)
def test_offer_gates(tax, over: dict, disp: str) -> None:
    rep = compare_job_offer(
        _pkg("current", D(1200000)),
        _pkg("offer", D(1500000), **over),
        tax_pack=tax,
        as_of=AS_OF,
        regime="new",
    )
    assert _clean(rep) == disp


def test_offer_float_rejected(tax) -> None:
    with pytest.raises(TypeError):
        compare_job_offer(
            _pkg("current", 1200000.0), _pkg("offer", D(1500000)), tax_pack=tax, as_of=AS_OF, regime="new"
        )


# --- profile ------------------------------------------------------------------------------


def _profile(tmp_path: Path, *cats: str) -> Profile:
    prof = Profile(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    for c in cats:
        prof.consent.grant(c, purpose="career")
    return prof


def test_career_snapshot(tmp_path: Path) -> None:
    prof = _profile(tmp_path)
    rep = career_snapshot(prof)
    assert _clean(rep) == HOLD and rep["questions"][0]["reason"] == "no_consent"
    prof.consent.grant("career", purpose="career")
    prof.consent.grant("finance", purpose="career")
    prof.set_fact("role", "data analyst", "career", "VERIFIED", "offer letter")
    prof.set_fact("years_experience", 4, "career", "USER_ESTIMATE", "self")
    prof.set_fact("skills", ["sql", "python"], "career", "USER_ESTIMATE", "self")
    prof.set_fact("industry", "fintech", "career", "USER_ESTIMATE", "self")
    prof.set_fact("location", "Pune", "career", "USER_ESTIMATE", "self")
    prof.set_fact("ambitions", "lead a data team", "career", "USER_ESTIMATE", "self")
    prof.set_fact("gross_salary_annual", D(1200000), "finance", "VERIFIED", "Form 16")
    rep = career_snapshot(prof)
    assert _clean(rep) == SHIP
    assert rep["result"]["facts"]["skills"]["value"] == ["sql", "python"]
    assert rep["result"]["facts"]["gross_salary_annual"]["provenance"] == "VERIFIED"


def test_job_package_from_profile(tmp_path: Path, tax) -> None:
    prof = _profile(tmp_path, "finance")
    prof.set_fact("gross_salary_annual", D(1200000), "finance", "VERIFIED", "Form 16")
    prof.set_fact("payroll_deductions_annual", D(0), "finance", "VERIFIED", "payslip")
    pkg = job_package_from_profile(prof)
    rep = compare_job_offer(pkg, _pkg("offer", D(1500000)), tax_pack=tax, as_of=AS_OF, regime="new")
    assert _clean(rep) == HOLD  # commute cost for the current job is not in the profile
    prof.set_fact("commute_monthly", D(0), "finance", "USER_ESTIMATE", "walk")
    prof.set_fact("other_monthly_work_costs", D(0), "finance", "USER_ESTIMATE", "none")
    rep = compare_job_offer(
        job_package_from_profile(prof), _pkg("offer", D(1500000)), tax_pack=tax, as_of=AS_OF, regime="new"
    )
    assert _clean(rep) == SHIP
    assert rep["inputs"]["current.gross_salary_annual"]["provenance"] == "VERIFIED"


# --- education via the decision engine ---------------------------------------------------------


def _situation() -> Situation:
    return Situation(
        take_home_monthly=D(100000),
        essential_monthly=D(55000),
        emergency_fund_balance=D(600000),
        emergency_fund_months=6,
        goals=("goal_promotion",),
    )


def _uplift(gross: Decimal, ref: str = "user note: peer offers 2026") -> UpliftScenario:
    return UpliftScenario(gross_salary_annual=gross, source_ref=ref, evidence_class="HYPOTHESIS")


def test_education_uses_lm04_take_home_uplift(tax) -> None:
    rep = evaluate_education(
        "ml_certificate",
        upfront_cost=D(100000),
        recurring_monthly_cost=D(0),
        horizon_months=24,
        downside_loss=D(100000),
        goal_tags=("goal_promotion",),
        current_gross_salary_annual=D(1200000),
        payroll_deductions_annual=D(0),
        uplift={
            "conservative": _uplift(D(1200000)),
            "base": _uplift(D(1300000)),
            "optimistic": _uplift(D(1500000)),
        },
        tax_pack=tax,
        regime="new",
        situation=_situation(),
        rates=load_rates_pack(),
        as_of=AS_OF,
    )
    assert rep["calculator"] == "decision"
    assert _clean(rep) in (SHIP, HOLD)
    course = next(o for o in rep["result"]["options"] if o["name"] == "ml_certificate")
    row4 = course["criteria"][3]["values"]["benefit_monthly"]
    base = _th_monthly(tax, D(1300000)) - _th_monthly(tax, D(1200000))
    assert row4 == {
        "conservative": D("0.00"),
        "base": base,
        "optimistic": _th_monthly(tax, D(1500000)) - _th_monthly(tax, D(1200000)),
    }
    assert base == D("6166.67")
    assert BASELINE_NAME in [o["name"] for o in rep["result"]["options"]]


def test_education_missing_source_holds(tax) -> None:
    rep = evaluate_education(
        "ml_certificate",
        upfront_cost=D(100000),
        recurring_monthly_cost=D(0),
        horizon_months=24,
        downside_loss=D(100000),
        goal_tags=("goal_promotion",),
        current_gross_salary_annual=D(1200000),
        payroll_deductions_annual=D(0),
        uplift={
            "conservative": _uplift(D(1200000)),
            "base": _uplift(D(1300000), ref=""),
            "optimistic": _uplift(D(1500000)),
        },
        tax_pack=tax,
        regime="new",
        situation=_situation(),
        rates=load_rates_pack(),
        as_of=AS_OF,
    )
    assert rep["result"]["recommendation"]["status"] == "insufficient_information"
    course = next(o for o in rep["result"]["options"] if o["name"] == "ml_certificate")
    assert course["criteria"][3]["disposition"] == HOLD


def test_education_lower_uplift_than_current_blocks(tax) -> None:
    rep = evaluate_education(
        "x",
        upfront_cost=D(1),
        recurring_monthly_cost=D(0),
        horizon_months=12,
        downside_loss=D(1),
        goal_tags=(),
        current_gross_salary_annual=D(1200000),
        payroll_deductions_annual=D(0),
        uplift={
            "conservative": _uplift(D(1100000)),
            "base": _uplift(D(1300000)),
            "optimistic": _uplift(D(1500000)),
        },
        tax_pack=tax,
        regime="new",
        situation=_situation(),
        rates=load_rates_pack(),
        as_of=AS_OF,
    )
    course = next(o for o in rep["result"]["options"] if o["name"] == "x")
    assert course["criteria"][3]["disposition"] == BLOCK


# --- benchmarks & negotiation ---------------------------------------------------------------------


_LOW, _HIGH, _MEDIAN = D(1300000), D(1800000), D(1500000)


def _bench(
    retrieved: str = "2026-09-01", low: Any = _LOW, high: Any = _HIGH, median: Any = _MEDIAN
) -> SalaryBenchmark:
    src = SourceRecord(
        url="https://example.org/salary-survey-2026",
        title="Salary survey",
        publisher="Example Org",
        published="2026-08-15",
        retrieved_on=retrieved,
        tier="secondary",
        evidence_class="EMPIRICAL",
    )
    return SalaryBenchmark(
        source=src,
        role="data analyst",
        location="Pune",
        low=low,
        median=median,
        high=high,
        sample_notes="n=120 self-reported",
    )


def test_benchmarks_missing_or_stale_hold() -> None:
    rep = benchmark_summary([], as_of=AS_OF)
    assert _clean(rep) == HOLD
    assert "insufficient market evidence" in json.dumps(rep).casefold()
    assert _clean(benchmark_summary([_bench(retrieved="2024-01-01")], as_of=AS_OF)) == HOLD


def test_benchmarks_range_from_sources_only() -> None:
    b2 = _bench(low=D(1400000), high=D(2000000), median=None)
    rep = benchmark_summary([_bench(), b2], as_of=AS_OF)
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["cited_range"] == {"low": D(1300000), "high": D(2000000)}
    assert r["sources_used"] == 2 and len(r["sources"]) == 2
    assert r["sources"][0]["url"] == "https://example.org/salary-survey-2026"
    assert "median" not in r["cited_range"]


def test_benchmark_contradiction_blocks() -> None:
    assert _clean(benchmark_summary([_bench(low=D(2000000), high=D(1000000))], as_of=AS_OF)) == BLOCK
    with pytest.raises(TypeError):
        benchmark_summary([_bench(low=1300000.0)], as_of=AS_OF)


def test_negotiation_prep(tax) -> None:
    offer = compare_job_offer(
        _pkg("current", D(1200000)), _pkg("offer", D(1500000)), tax_pack=tax, as_of=AS_OF, regime="new"
    )
    bench = benchmark_summary([_bench()], as_of=AS_OF)
    rep = negotiation_prep(offer, bench, target_gross_annual=D(1700000))
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["batna"]["option"] == "current"
    assert r["batna"]["net_after_work_costs_monthly"] == D("100000.00")
    assert r["offer_position_in_cited_range"] == "within"
    assert r["target_position_in_cited_range"] == "within"
    assert len(r["checklist"]) >= 5
    assert all({"item", "status", "basis"} <= set(i) for i in r["checklist"])


def test_negotiation_without_market_evidence_holds(tax) -> None:
    offer = compare_job_offer(
        _pkg("current", D(1200000)), _pkg("offer", D(1500000)), tax_pack=tax, as_of=AS_OF, regime="new"
    )
    rep = negotiation_prep(offer, benchmark_summary([], as_of=AS_OF))
    assert _clean(rep) == HOLD
    assert rep["result"]["offer_position_in_cited_range"] is None
    assert "insufficient market evidence" in json.dumps(rep, default=str).casefold()


def test_negotiation_rejects_wrong_reports(tax) -> None:
    bench = benchmark_summary([_bench()], as_of=AS_OF)
    with pytest.raises(ValueError):
        negotiation_prep(bench, bench)
