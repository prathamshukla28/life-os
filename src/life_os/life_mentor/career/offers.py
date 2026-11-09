"""Job-offer comparison: current vs offer, take-home via LM04, per-criterion table (LM08).

Both packages are taxed with the same regime and tax year using ``finance.take_home``;
work costs (commute, other) are user inputs. The table has no score: it states the
monthly difference and the risks, and names staying put as the BATNA.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.inputs import InputValue, Raw, assumption_notes, input_checks, resolve
from ..finance.report import finalize, fixed_check, gate_reason_checks
from ..finance.tax import take_home
from ..profile import ConsentRequired, Fact, Profile, Requirement
from ..research import RulePack
from ..security import redact

__all__ = [
    "CRITERIA",
    "JOB_PACKAGE_REQUIREMENTS",
    "JobPackage",
    "compare_job_offer",
    "job_package_from_profile",
    "job_package_questions",
]

CRITERIA = (
    "take_home_monthly",
    "income_tax_annual",
    "work_costs_monthly",
    "net_after_work_costs_monthly",
    "difference_vs_current",
    "probation_and_risk",
)


@dataclass(frozen=True)
class JobPackage:
    name: str
    gross_salary_annual: Raw
    payroll_deductions_annual: Raw
    commute_monthly: Raw
    other_monthly_costs: Raw
    probation_months: int | None = 0
    notes: tuple[str, ...] = ()


JOB_PACKAGE_REQUIREMENTS = (
    Requirement("gross_salary_annual", "finance", "What is your current gross annual salary (fixed pay)?"),
    Requirement(
        "payroll_deductions_annual",
        "finance",
        "What are your yearly payroll deductions (professional tax plus employee EPF)?",
    ),
    Requirement(
        "commute_monthly", "finance", "What does commuting to your current job cost per month? (0 if nothing)"
    ),
    Requirement(
        "other_monthly_work_costs",
        "finance",
        "What other monthly costs does your current job cause? (0 if none)",
    ),
)


def job_package_questions(profile: Profile) -> list[dict[str, str]]:
    """Questions for the current-job facts a comparison needs (same keys as the inputs)."""
    return profile.missing_facts(JOB_PACKAGE_REQUIREMENTS, allow_assumptions=True)


def job_package_from_profile(profile: Profile, name: str = "current") -> JobPackage:
    """Current job from consented finance facts (missing facts stay None -> HOLD later)."""

    def fact(key: str) -> Fact | None:
        try:
            return profile.get(key)
        except ConsentRequired:
            return None

    return JobPackage(
        name=name,
        gross_salary_annual=fact("gross_salary_annual"),
        payroll_deductions_annual=fact("payroll_deductions_annual"),
        commute_monthly=fact("commute_monthly"),
        other_monthly_costs=fact("other_monthly_work_costs"),
        probation_months=0,
    )


def _package(
    pkg: JobPackage, pack: RulePack, as_of: date, regime: str, tax_year: str | None, age: Raw = None
) -> tuple[list[InputValue], list[Check], dict[str, Any] | None, list[dict[str, Any]]]:
    n = redact(pkg.name)
    gross = resolve(f"{n}.gross_salary_annual", pkg.gross_salary_annual)
    payroll = resolve(f"{n}.payroll_deductions_annual", pkg.payroll_deductions_annual)
    commute = resolve(f"{n}.commute_monthly", pkg.commute_monthly)
    other = resolve(f"{n}.other_monthly_work_costs", pkg.other_monthly_costs)
    inputs = [gross, payroll, commute, other]
    checks = input_checks(inputs)
    probation = pkg.probation_months
    if probation is None:
        checks.append(fixed_check(HOLD, f"{n}.probation", f"{n}: probation terms unknown"))
    elif isinstance(probation, bool) or not isinstance(probation, int) or probation < 0:
        checks.append(
            fixed_check(BLOCK, f"{n}.probation", f"{n}: probation_months must be a whole number >= 0")
        )
    report = take_home(
        pack,
        as_of=as_of,
        regime=regime,
        gross_salary=pkg.gross_salary_annual,
        payroll_deductions=pkg.payroll_deductions_annual,
        tax_year=tax_year,
        age=age,
    )
    checks += gate_reason_checks(
        {**r, "check": f"{n}:{r['check']}"} for r in report["gate"]["reasons"] if r["disposition"] != SHIP
    )
    r = report["result"]
    data: dict[str, Any] | None = None
    if (
        r is not None
        and commute.value is not None
        and other.value is not None
        and commute.error is None
        and other.error is None
    ):
        costs = commute.value + other.value
        risk = (
            [f"{probation}-month probation" if probation else "no probation stated"]
            if isinstance(probation, int)
            else ["probation terms unknown"]
        )
        data = {
            "gross_salary_annual": r["gross_annual"],
            "take_home_monthly": r["take_home_monthly"],
            "income_tax_annual": r["income_tax"],
            "work_costs_monthly": costs,
            "net_after_work_costs_monthly": r["take_home_monthly"] - costs,
            "risk_notes": risk + [redact(x) for x in pkg.notes],
        }
    return inputs, checks, data, report["rules_used"]


def compare_job_offer(
    current: JobPackage,
    offer: JobPackage,
    *,
    tax_pack: RulePack,
    as_of: date,
    regime: str,
    tax_year: str | None = None,
    age: Raw = None,
) -> dict[str, Any]:
    """Current vs offer: monthly take-home (LM04), work costs, net, difference, risks."""
    inputs: list[InputValue] = []
    checks: list[Check] = []
    packages: dict[str, dict[str, Any] | None] = {}
    rules_used: list[dict[str, Any]] = []
    names = (redact(current.name), redact(offer.name))
    if names[0] == names[1]:
        checks.append(fixed_check(BLOCK, "names", "the two packages need different names"))
    for pkg in (current, offer):
        ivs, cks, data, rules = _package(pkg, tax_pack, as_of, regime, tax_year, age)
        inputs += ivs
        checks += cks
        packages[redact(pkg.name)] = data
        rules_used += [x for x in rules if x["id"] not in {y["id"] for y in rules_used}]
    assumptions = [
        f"Both packages use the {regime} regime and the same tax rules (LM04 take_home).",
        (
            "Gross salary is fixed annual pay; variable pay, bonuses, ESOPs and employer "
            "contributions are not included unless you put them in gross."
        ),
        "Commute and other work costs are your monthly estimates.",
        (
            "No combined score: compare the rows; staying in the current job is the walk-away "
            "alternative (BATNA)."
        ),
        *assumption_notes(inputs),
    ]
    cur, off = packages.get(names[0]), packages.get(names[1])
    result: dict[str, Any] | None = None
    if cur is not None and off is not None and not any(c.fn()[0] == BLOCK for c in checks):
        diff = {
            "take_home": off["take_home_monthly"] - cur["take_home_monthly"],
            "net_after_work_costs": off["net_after_work_costs_monthly"] - cur["net_after_work_costs_monthly"],
        }
        rows = [
            {"criterion": c, "values": {n: p[c] for n, p in ((names[0], cur), (names[1], off))}}
            for c in CRITERIA[:4]
        ]
        rows.append({"criterion": "difference_vs_current", "values": {names[1]: diff}})
        rows.append(
            {
                "criterion": "probation_and_risk",
                "values": {names[0]: "; ".join(cur["risk_notes"]), names[1]: "; ".join(off["risk_notes"])},
            }
        )
        result = {
            "packages": {names[0]: cur, names[1]: off},
            "criteria": rows,
            "difference_monthly": diff,
            "batna": {
                "option": names[0],
                "net_after_work_costs_monthly": cur["net_after_work_costs_monthly"],
                "basis": "staying in the current job if the offer is not improved",
            },
        }
        sign = "higher" if diff["net_after_work_costs"] >= 0 else "lower"
        checks.append(fixed_check(SHIP, "computed", f"offer net monthly is {sign} than current"))
    return finalize(
        "job_offer_comparison",
        inputs=inputs,
        assumptions=assumptions,
        result=result,
        checks=checks,
        rules_used=rules_used,
        extra={
            "as_of": as_of.isoformat(),
            "regime": regime,
            "names": {"current": names[0], "offer": names[1]},
        },
    )
