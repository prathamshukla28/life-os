"""Finance calculators (LM04): India income tax FY2026-27, take-home, cash flow,
emergency fund, debt payoff, goal SIP, and net worth.

Every calculator returns a report dict (``calculator, inputs, rules_used,
assumptions, result, gate``) - per-criterion facts plus a ``life_os.core.gate``
disposition, never a combined score. Money is Decimal; floats raise TypeError.
"""

from __future__ import annotations

from .budget import DEFAULT_EMERGENCY_MONTHS, cash_flow, emergency_fund, net_worth
from .debt import DEFAULT_MAX_MONTHS, STRATEGIES, Debt, debt_payoff
from .inputs import RULE, InputValue, resolve
from .money import format_inr, round_288, to_money
from .sip import SCENARIOS, goal_sip
from .tax import (
    AGE_KEY,
    GROSS_SALARY_KEY,
    OTHER_DEDUCTIONS_KEY,
    REGIMES,
    TAX_PACK_PATH,
    AgeExemption,
    RegimeRules,
    TaxBreakdown,
    compute_tax,
    income_tax,
    income_tax_from_profile,
    load_tax_pack,
    regime_rules,
    take_home,
)

__all__ = [
    "AGE_KEY",
    "DEFAULT_EMERGENCY_MONTHS",
    "DEFAULT_MAX_MONTHS",
    "GROSS_SALARY_KEY",
    "OTHER_DEDUCTIONS_KEY",
    "REGIMES",
    "RULE",
    "SCENARIOS",
    "STRATEGIES",
    "TAX_PACK_PATH",
    "AgeExemption",
    "Debt",
    "InputValue",
    "RegimeRules",
    "TaxBreakdown",
    "cash_flow",
    "compute_tax",
    "debt_payoff",
    "emergency_fund",
    "format_inr",
    "goal_sip",
    "income_tax",
    "income_tax_from_profile",
    "load_tax_pack",
    "net_worth",
    "regime_rules",
    "resolve",
    "round_288",
    "take_home",
    "to_money",
]
