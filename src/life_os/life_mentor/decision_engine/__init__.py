"""Salary-based affordability decision engine (LM05): 8 per-criterion rows per option,
a do-nothing baseline, side-by-side alternatives, gate disposition - no combined score."""

from __future__ import annotations

from .confirm import record_confirmation
from .engine import (
    BASELINE_NAME,
    CRITERIA,
    DECISION_JOURNAL,
    DEFAULT_RATE_ID,
    RATES_PACK_PATH,
    SCENARIOS,
    analyze_decision,
    load_rates_pack,
)
from .models import (
    DERIVED,
    OPTION_KINDS,
    BenefitEstimate,
    Benefits,
    Option,
    Situation,
    benefits,
    situation_from_profile,
)

__all__ = [
    "BASELINE_NAME",
    "CRITERIA",
    "DECISION_JOURNAL",
    "DEFAULT_RATE_ID",
    "DERIVED",
    "OPTION_KINDS",
    "RATES_PACK_PATH",
    "SCENARIOS",
    "BenefitEstimate",
    "Benefits",
    "Option",
    "Situation",
    "analyze_decision",
    "benefits",
    "load_rates_pack",
    "record_confirmation",
    "situation_from_profile",
]
