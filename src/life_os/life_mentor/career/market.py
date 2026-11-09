"""Career snapshot, salary benchmarks from user-cited sources, negotiation checklist (LM08).

No salary data is bundled. A benchmark is a user-supplied ``SourceRecord`` plus the
low/median/high figures that source states; without at least one fresh benchmark the
result is HOLD "insufficient market evidence". The cited range is the min-low to
max-high across sources - never an average or an invented "market rate".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.inputs import InputValue, Raw, assumption_notes, input_checks, resolve
from ..finance.report import finalize, fixed_check, gate_reason_checks
from ..profile import UNKNOWN, ConsentRequired, Profile, Requirement, profile_gate
from ..research import SourceRecord
from ..security import redact

__all__ = [
    "BENCHMARK_MAX_AGE_DAYS",
    "CAREER_KEYS",
    "INSUFFICIENT_EVIDENCE",
    "SalaryBenchmark",
    "benchmark_summary",
    "career_snapshot",
    "negotiation_prep",
]

CAREER_KEYS = ("role", "years_experience", "skills", "industry", "location", "ambitions")
BENCHMARK_MAX_AGE_DAYS = 365
INSUFFICIENT_EVIDENCE = "insufficient market evidence"


def career_snapshot(profile: Profile) -> dict[str, Any]:
    """Consented career facts + current salary facts, with questions for what is missing."""
    reqs = [Requirement(k, "career") for k in CAREER_KEYS]
    reqs.append(
        Requirement(
            "gross_salary_annual", "finance", "What is your current gross annual salary (CTC fixed pay)?"
        )
    )
    outcome = profile_gate(profile, reqs, allow_assumptions=True)
    facts: dict[str, Any] = {}
    for key in (*CAREER_KEYS, "gross_salary_annual", "take_home_monthly"):
        try:
            fact = profile.get(key)
        except ConsentRequired:
            continue
        if fact is not None and fact.provenance != UNKNOWN:
            value = redact(fact.value) if isinstance(fact.value, str) else fact.value
            if isinstance(value, list):
                value = [redact(v) if isinstance(v, str) else v for v in value]
            facts[key] = {"value": value, "provenance": fact.provenance, "basis": fact.basis}
    checks = gate_reason_checks(outcome.gate.reasons)
    return finalize(
        "career_snapshot",
        inputs=[],
        assumptions=["Facts are as you recorded them; Life Mentor adds no market data."],
        result={"facts": facts},
        checks=checks,
        extra={"questions": outcome.questions},
    )


@dataclass(frozen=True)
class SalaryBenchmark:
    source: SourceRecord
    role: str
    location: str
    low: Raw
    median: Raw
    high: Raw
    sample_notes: str
    currency: str = "INR"
    period: str = "annual"


def benchmark_summary(
    benchmarks: Sequence[SalaryBenchmark], *, as_of: date, max_age_days: int = BENCHMARK_MAX_AGE_DAYS
) -> dict[str, Any]:
    checks: list[Check] = []
    inputs: list[InputValue] = []
    rows: list[dict[str, Any]] = []
    for i, b in enumerate(benchmarks):
        low, high = resolve(f"b{i}.low", b.low), resolve(f"b{i}.high", b.high)
        median = resolve(f"b{i}.median", b.median) if b.median is not None else None
        ivs = [low, high] + ([median] if median is not None else [])
        inputs += ivs
        checks += input_checks(ivs)
        if b.currency != "INR" or b.period != "annual":
            checks.append(fixed_check(BLOCK, f"b{i}.unit", "benchmarks must be INR per year"))
        retrieved = b.source.retrieved_on
        if not isinstance(retrieved, date):
            checks.append(fixed_check(BLOCK, f"b{i}.retrieved", "retrieved_on must be a date"))
            continue
        age = (as_of - retrieved).days
        if age < 0:
            checks.append(fixed_check(BLOCK, f"b{i}.retrieved", "retrieved after as_of"))
            continue
        if low.value is None or high.value is None:
            continue
        if low.value > high.value or (
            median is not None and median.value is not None and not low.value <= median.value <= high.value
        ):
            checks.append(fixed_check(BLOCK, f"b{i}.range", "low <= median <= high is violated"))
            continue
        rows.append(
            {
                **{k: v for k, v in b.source.to_dict().items()},
                "role": redact(b.role),
                "location": redact(b.location),
                "low": low.value,
                "median": median.value if median else None,
                "high": high.value,
                "sample_notes": redact(b.sample_notes),
                "age_days": age,
                "fresh": age <= max_age_days,
            }
        )
    fresh = [r for r in rows if r["fresh"]]
    if not fresh and not any(c.fn()[0] == BLOCK for c in checks):
        checks.append(
            fixed_check(
                HOLD,
                "evidence",
                f"{INSUFFICIENT_EVIDENCE}: no cited salary "
                f"benchmark retrieved within {max_age_days} days; add a dated "
                f"source (url, retrieved_on, figures)",
            )
        )
    result: dict[str, Any] | None = None
    if fresh and not any(c.fn()[0] == BLOCK for c in checks):
        result = {
            "sources": rows,
            "sources_used": len(fresh),
            "cited_range": {"low": min(r["low"] for r in fresh), "high": max(r["high"] for r in fresh)},
            "evidence_note": "single source" if len(fresh) == 1 else f"{len(fresh)} independent sources",
        }
        checks.append(fixed_check(SHIP, "evidence", f"{len(fresh)} fresh cited benchmark(s)"))
    return finalize(
        "salary_benchmarks",
        inputs=inputs,
        assumptions=[
            (
                "Benchmarks are the figures your cited sources state; Life Mentor bundles no salary "
                "data and does not average sources into a market rate."
            ),
            f"Sources retrieved more than {max_age_days} days before as_of are listed but not used.",
            *assumption_notes(inputs),
        ],
        result=result,
        checks=checks,
    )


def _position(value: Decimal | None, rng: dict[str, Decimal] | None) -> str | None:
    if value is None or rng is None:
        return None
    if value < rng["low"]:
        return "below"
    return "above" if value > rng["high"] else "within"


def negotiation_prep(
    offer_report: dict[str, Any], benchmark_report: dict[str, Any], *, target_gross_annual: Raw = None
) -> dict[str, Any]:
    """Checklist from a ``job_offer_comparison`` and a ``salary_benchmarks`` report."""
    if offer_report.get("calculator") != "job_offer_comparison":
        raise ValueError("offer_report must come from compare_job_offer")
    if benchmark_report.get("calculator") != "salary_benchmarks":
        raise ValueError("benchmark_report must come from benchmark_summary")
    checks: list[Check] = []
    od, bd = offer_report["gate"]["disposition"], benchmark_report["gate"]["disposition"]
    checks.append(fixed_check(od, "offer_comparison", f"offer comparison is {od}"))
    checks.append(
        fixed_check(
            bd,
            "market_evidence",
            "cited benchmarks available" if bd == SHIP else f"{INSUFFICIENT_EVIDENCE} (benchmarks are {bd})",
        )
    )
    inputs: list[InputValue] = []
    target: InputValue | None = None
    if target_gross_annual is not None:
        target = resolve("target_gross_annual", target_gross_annual)
        inputs.append(target)
        checks += input_checks([target])
    offer = offer_report.get("result")
    bench = benchmark_report.get("result")
    rng = bench["cited_range"] if bench else None
    offer_name = offer_report.get("names", {}).get("offer")
    offer_gross = offer["packages"][offer_name]["gross_salary_annual"] if offer else None
    batna = offer["batna"] if offer else None
    offer_pos = _position(offer_gross, rng)
    target_pos = _position(target.value if target else None, rng)
    checklist = [
        {
            "item": "Cited market range for this role",
            "status": f"{rng['low']} to {rng['high']} INR/year" if rng else INSUFFICIENT_EVIDENCE,
            "basis": f"{bench['sources_used']} user-cited source(s)" if bench else "none",
        },
        {
            "item": "Walk-away alternative (BATNA): stay in the current job",
            "status": f"net {batna['net_after_work_costs_monthly']} INR/month"
            if batna
            else "unknown - complete the offer comparison",
            "basis": "LM04 take-home via compare_job_offer",
        },
        {
            "item": "Offer position against the cited range",
            "status": offer_pos or "unknown",
            "basis": "offer gross vs cited range",
        },
        {
            "item": "Your target ask",
            "status": target_pos or ("not set" if target is None else "unknown"),
            "basis": "your target vs cited range",
        },
        {
            "item": "Confirm fixed vs variable pay, joining bonus and any clawback in writing",
            "status": "to do",
            "basis": "Life Mentor checklist (no market data)",
        },
        {
            "item": "Confirm probation length, notice period and joining date",
            "status": "to do",
            "basis": "Life Mentor checklist (no market data)",
        },
        {
            "item": "Get the revised offer in writing before resigning",
            "status": "to do",
            "basis": "Life Mentor checklist (no market data)",
        },
    ]
    if target_pos == "above":
        checks.append(
            fixed_check(SHIP, "target_note", "target is above every cited source; be ready to justify it")
        )
    return finalize(
        "negotiation_prep",
        inputs=inputs,
        assumptions=[
            (
                "The checklist uses only your cited benchmarks and your own offer comparison; no market "
                "figures are added."
            ),
            *assumption_notes(inputs),
        ],
        result={
            "checklist": checklist,
            "batna": batna,
            "offer_position_in_cited_range": offer_pos,
            "target_position_in_cited_range": target_pos,
        },
        checks=checks,
    )
