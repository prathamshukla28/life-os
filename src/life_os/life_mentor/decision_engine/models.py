"""Decision-engine inputs: options, benefit scenarios, and the user's situation (LM05).

An ``Option`` is one proposed spend / investment / course / subscription /
business / lifestyle upgrade (plan LM05 "Inputs"). A ``Situation`` is the
profile cash-flow context: take-home, essential spend, emergency-fund balance
(+ target months) and the user's stated goal ids. ``situation_from_profile``
builds it from consented profile facts (LM01) and, optionally, an LM04
``take_home`` report; missing facts become questions, never defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from life_os.core.gate import SHIP

from ..finance.inputs import InputValue, Raw
from ..finance.money import to_money
from ..profile import UNKNOWN, ConsentRequired, Fact, Profile, Requirement, profile_gate

__all__ = [
    "DERIVED",
    "EF_BALANCE_KEY",
    "EF_MONTHS_KEY",
    "ESSENTIAL_KEY",
    "OPTION_KINDS",
    "TAKE_HOME_KEY",
    "BenefitEstimate",
    "Benefits",
    "Option",
    "Situation",
    "benefits",
    "situation_from_profile",
]

OPTION_KINDS = ("spend", "investment", "course", "subscription", "business", "lifestyle_upgrade")
DERIVED = "DERIVED"

TAKE_HOME_KEY = "take_home_monthly"
ESSENTIAL_KEY = "essential_spend_monthly"
EF_BALANCE_KEY = "emergency_fund_balance"
EF_MONTHS_KEY = "emergency_fund_months"


@dataclass(frozen=True)
class BenefitEstimate:
    """One scenario's monthly benefit, with where the estimate comes from."""

    monthly: Raw
    basis: str
    evidence_class: str


@dataclass(frozen=True)
class Benefits:
    conservative: BenefitEstimate | None
    base: BenefitEstimate | None
    optimistic: BenefitEstimate | None


def benefits(conservative: Raw, base: Raw, optimistic: Raw, *, basis: str, evidence_class: str) -> Benefits:
    """Three scenarios sharing one basis and evidence class (convenience builder)."""
    return Benefits(*(BenefitEstimate(v, basis, evidence_class) for v in (conservative, base, optimistic)))


@dataclass(frozen=True)
class Option:
    name: str
    kind: str
    upfront_cost: Raw
    recurring_monthly_cost: Raw
    horizon_months: int | None
    benefit_monthly: Benefits | None
    downside_loss: Raw
    goal_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("option name must be a non-empty string")
        if self.kind not in OPTION_KINDS:
            raise ValueError(f"unknown option kind {self.kind!r}; expected one of {OPTION_KINDS}")
        if not isinstance(self.goal_tags, tuple) or not all(
            isinstance(t, str) and t.strip() for t in self.goal_tags
        ):
            raise ValueError("goal_tags must be a tuple of non-empty goal ids")
        if self.horizon_months is not None and (
            isinstance(self.horizon_months, bool) or not isinstance(self.horizon_months, int)
        ):
            raise TypeError("horizon_months must be an int")


@dataclass(frozen=True)
class Situation:
    """Cash-flow context for a decision.

    ``emergency_fund_months`` may be an int, a whole-number Decimal, or a profile
    Fact; a non-whole value BLOCKs step 3. ``profile_reasons`` carries the
    non-SHIP reasons of the profile gate (a BLOCK there BLOCKs the decision).
    ``take_home_as_of`` is the as_of of the LM04 report the take-home came from;
    it must equal the decision's as_of or take-home is treated as UNKNOWN.
    """

    take_home_monthly: Raw | InputValue
    essential_monthly: Raw | InputValue
    emergency_fund_balance: Raw | InputValue
    goals: tuple[str, ...] | None = None
    emergency_fund_months: Raw = None
    questions: tuple[dict[str, str], ...] = ()
    extra_rules_used: tuple[dict[str, Any], ...] = field(default=())
    profile_reasons: tuple[dict[str, Any], ...] = ()
    take_home_as_of: str | None = None


def _take_home_from_report(
    report: dict[str, Any],
) -> tuple[InputValue, dict[str, str] | None, str | None]:
    """Validate an LM04 take_home report. Wrong calculator -> ValueError; a
    non-Decimal amount (float/str) -> TypeError via ``to_money``."""
    if report.get("calculator") != "take_home":
        raise ValueError(f"expected an LM04 take_home report, got {report.get('calculator')!r}")
    disp = report.get("gate", {}).get("disposition")
    result = report.get("result")
    as_of = report.get("as_of")
    if disp == SHIP and isinstance(result, dict) and "take_home_monthly" in result and isinstance(as_of, str):
        value = to_money(TAKE_HOME_KEY, result["take_home_monthly"])
        regime = result.get("regime", "?")
        return (
            InputValue(TAKE_HOME_KEY, value, DERIVED, f"LM04 take_home ({regime} regime, as of {as_of})"),
            None,
            as_of,
        )
    iv = InputValue(TAKE_HOME_KEY, None, UNKNOWN, f"LM04 take_home not usable ({disp})")
    question = {
        "key": TAKE_HOME_KEY,
        "category": "finance",
        "reason": f"take_home_report_{str(disp).lower()}",
        "question": "The take-home calculation is not usable yet; resolve its open items "
        "(see its gate reasons) or state your monthly take-home pay.",
    }
    return iv, question, None


def situation_from_profile(
    profile: Profile,
    *,
    take_home_report: dict[str, Any] | None = None,
    emergency_fund_months: int | None = None,
) -> Situation:
    """Build a Situation from consented facts; unmet requirements become questions."""
    reqs: list[Requirement] = []
    if take_home_report is None:
        reqs.append(
            Requirement(
                TAKE_HOME_KEY,
                "finance",
                "What is your monthly take-home pay after tax and payroll "
                "deductions, and how do you know it?",
            )
        )
    reqs += [
        Requirement(
            ESSENTIAL_KEY,
            "finance",
            "What do you spend each month on essentials (rent, food, utilities, EMIs)?",
        ),
        Requirement(
            EF_BALANCE_KEY, "finance", "How much do you hold as an emergency fund in liquid savings?"
        ),
    ]
    outcome = profile_gate(profile, reqs, allow_assumptions=True)
    questions = list(outcome.questions)
    profile_reasons = tuple(
        {**r, "check": f"profile:{r['check']}"} for r in outcome.gate.reasons if r["disposition"] != SHIP
    )

    def fetch(key: str) -> Fact | None:
        try:
            return profile.get(key)
        except ConsentRequired:
            return None

    extra_rules: tuple[dict[str, Any], ...] = ()
    take_home: Raw | InputValue
    take_home_as_of: str | None = None
    if take_home_report is not None:
        take_home, q, take_home_as_of = _take_home_from_report(take_home_report)
        if q is not None:
            questions.append(q)
        extra_rules = tuple(take_home_report.get("rules_used", []))
    else:
        take_home = fetch(TAKE_HOME_KEY)

    months: Raw = emergency_fund_months
    if months is None:
        months = fetch(EF_MONTHS_KEY)  # whole-number check happens in the engine

    goals: tuple[str, ...] | None = None
    if profile.consent.has("goals"):
        goals = tuple(k for k, f in profile.facts_in_category("goals").items() if f.provenance != UNKNOWN)
    else:
        questions.append(
            {
                "key": "goals",
                "category": "goals",
                "reason": "no_consent",
                "question": "May Life Mentor use your stated goals? Goal fit (step 8) needs them.",
            }
        )
    return Situation(
        take_home_monthly=take_home,
        essential_monthly=fetch(ESSENTIAL_KEY),
        emergency_fund_balance=fetch(EF_BALANCE_KEY),
        goals=goals,
        emergency_fund_months=months,
        questions=tuple(questions),
        extra_rules_used=extra_rules,
        profile_reasons=profile_reasons,
        take_home_as_of=take_home_as_of,
    )
