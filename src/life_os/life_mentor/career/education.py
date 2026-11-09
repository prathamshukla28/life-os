"""Education / course evaluation through the LM05 decision engine (LM08).

The user supplies the salary they expect after the course for each scenario
(conservative / base / optimistic), each with a source reference and evidence class.
The monthly benefit is the LM04 take-home difference between that salary and the
current one (same regime, same payroll deductions) - nothing is invented. A missing
source reference leaves the benefit basis empty, so the decision HOLDs; a scenario
below the current salary gives a negative benefit and BLOCKs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from life_os.core.gate import SHIP

from ..decision_engine import (
    BenefitEstimate,
    Benefits,
    Option,
    Situation,
    analyze_decision,
)
from ..finance.inputs import Raw
from ..finance.money import format_inr
from ..finance.tax import take_home
from ..research import RulePack
from ..security import PrivateStore

__all__ = ["UpliftScenario", "evaluate_education"]

_SCENARIOS = ("conservative", "base", "optimistic")


@dataclass(frozen=True)
class UpliftScenario:
    gross_salary_annual: Raw
    source_ref: str
    evidence_class: str


def _take_home(
    pack: RulePack, gross: Raw, payroll: Raw, as_of: date, regime: str, tax_year: str | None, age: Raw
) -> dict[str, Any]:
    return take_home(
        pack,
        as_of=as_of,
        regime=regime,
        gross_salary=gross,
        payroll_deductions=payroll,
        tax_year=tax_year,
        age=age,
    )


def evaluate_education(
    name: str,
    *,
    upfront_cost: Raw,
    recurring_monthly_cost: Raw,
    horizon_months: int | None,
    downside_loss: Raw,
    goal_tags: tuple[str, ...],
    current_gross_salary_annual: Raw,
    payroll_deductions_annual: Raw,
    uplift: Mapping[str, UpliftScenario],
    tax_pack: RulePack,
    regime: str,
    situation: Situation,
    rates: RulePack,
    as_of: date,
    tax_year: str | None = None,
    age: Raw = None,
    kind: str = "course",
    alternatives: Sequence[Option] = (),
    store: PrivateStore | None = None,
) -> dict[str, Any]:
    """Run the 8-step decision analysis with take-home uplift benefits (LM05 report)."""
    current = _take_home(
        tax_pack, current_gross_salary_annual, payroll_deductions_annual, as_of, regime, tax_year, age
    )
    cur_ok = current["gate"]["disposition"] == SHIP and current["result"] is not None
    estimates: dict[str, BenefitEstimate | None] = {}
    rules = list(current["rules_used"])
    for s in _SCENARIOS:
        sc = uplift.get(s)
        if sc is None:
            estimates[s] = None
            continue
        after = _take_home(
            tax_pack, sc.gross_salary_annual, payroll_deductions_annual, as_of, regime, tax_year, age
        )
        monthly = None
        if cur_ok and after["gate"]["disposition"] == SHIP and after["result"] is not None:
            monthly = after["result"]["take_home_monthly"] - current["result"]["take_home_monthly"]
        basis = ""
        if sc.source_ref.strip():
            gross = after["result"]["gross_annual"] if after["result"] else None
            basis = (
                f"{sc.source_ref} | take-home uplift via LM04 ({regime} regime) from gross "
                f"{format_inr(gross) if gross is not None else 'unknown'}"
            )
        estimates[s] = BenefitEstimate(monthly, basis, sc.evidence_class)
    option = Option(
        name=name,
        kind=kind,
        upfront_cost=upfront_cost,
        recurring_monthly_cost=recurring_monthly_cost,
        horizon_months=horizon_months,
        benefit_monthly=Benefits(estimates["conservative"], estimates["base"], estimates["optimistic"]),
        downside_loss=downside_loss,
        goal_tags=goal_tags,
    )
    seen = {r["id"] for r in situation.extra_rules_used}
    extra = tuple(situation.extra_rules_used) + tuple(r for r in rules if r["id"] not in seen)
    return analyze_decision(
        option,
        situation=replace(situation, extra_rules_used=extra),
        rates=rates,
        as_of=as_of,
        alternatives=alternatives,
        store=store,
    )
