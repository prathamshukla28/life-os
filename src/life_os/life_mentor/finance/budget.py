"""Monthly cash flow, emergency-fund target, and net worth (LM04).

No rule pack is involved; each report still carries inputs with provenance,
explicit assumptions, and a fail-closed gate. A deficit or an unreachable
emergency-fund target is a real finding reported with HOLD, not an error.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..profile import ASSUMPTION
from .inputs import InputValue, Raw, assumption_notes, input_checks, resolve
from .money import format_inr
from .report import finalize

__all__ = ["DEFAULT_EMERGENCY_MONTHS", "cash_flow", "emergency_fund", "net_worth"]

# A planning convention, not a rule value: recorded as an ASSUMPTION in output.
DEFAULT_EMERGENCY_MONTHS = 6
_PCT = Decimal("0.01")


def _fixed(disposition: str, name: str, reason: str) -> Check:
    return Check(name=name, fn=lambda: (disposition, reason))


def _ok(ivs: list[InputValue]) -> bool:
    return all(
        iv.error is None and iv.value is not None and (iv.allow_negative or iv.value >= 0) for iv in ivs
    )


def _resolve_map(prefix: str, items: Mapping[str, Raw] | None) -> list[InputValue]:
    return [resolve(f"{prefix}:{name}", raw) for name, raw in (items or {}).items()]


def _sum(ivs: list[InputValue]) -> Decimal:
    return sum((iv.value for iv in ivs if iv.value is not None), Decimal(0))


def cash_flow(
    *,
    take_home_monthly: Raw,
    essential_expenses: Mapping[str, Raw],
    discretionary_expenses: Mapping[str, Raw] | None = None,
) -> dict[str, Any]:
    """Monthly surplus = take-home - essential - discretionary spending."""
    th = resolve("take_home_monthly", take_home_monthly)
    ess = _resolve_map("essential", essential_expenses)
    disc = _resolve_map("discretionary", discretionary_expenses)
    inputs = [th, *ess, *disc]
    checks = input_checks(inputs)
    if not ess:
        checks.append(
            _fixed(
                HOLD,
                "essential_expenses",
                "no essential expenses listed; please list rent, food, utilities, EMIs",
            )
        )
    assumptions = [
        "All amounts are monthly and in INR; expenses not listed are treated as absent.",
        *assumption_notes(inputs),
    ]
    result: dict[str, Any] | None = None
    if _ok(inputs) and ess:
        assert th.value is not None
        total_ess, total_disc = _sum(ess), _sum(disc)
        surplus = th.value - total_ess - total_disc
        result = {
            "take_home_monthly": th.value,
            "total_essential": total_ess,
            "total_discretionary": total_disc,
            "surplus": surplus,
            "savings_rate_percent": (surplus / th.value * 100).quantize(_PCT, ROUND_HALF_UP)
            if th.value
            else None,
            "essential_share_percent": (total_ess / th.value * 100).quantize(_PCT, ROUND_HALF_UP)
            if th.value
            else None,
        }
        if surplus < 0:
            checks.append(
                _fixed(HOLD, "deficit", f"spending exceeds take-home by {format_inr(-surplus)} a month")
            )
        else:
            checks.append(_fixed(SHIP, "surplus", f"monthly surplus {format_inr(surplus)}"))
    return finalize("cash_flow", inputs=inputs, assumptions=assumptions, result=result, checks=checks)


def emergency_fund(
    *,
    essential_monthly: Raw,
    current_balance: Raw,
    monthly_surplus: Raw,
    months: int | None = None,
) -> dict[str, Any]:
    """Target = months x essential monthly expenses; gap and months to reach it."""
    if months is None:
        months_iv = InputValue(
            "months",
            Decimal(DEFAULT_EMERGENCY_MONTHS),
            ASSUMPTION,
            "default planning convention",
            integer=True,
        )
        months_note = (
            f"Target uses {DEFAULT_EMERGENCY_MONTHS} months of essential expenses "
            "(a default ASSUMPTION; choose your own months)."
        )
    else:
        if isinstance(months, bool) or not isinstance(months, int):
            raise TypeError("months must be an int")
        months_iv = resolve("months", months, integer=True)
        months_note = f"Target uses {months} months of essential expenses (user-chosen)."
    ess = resolve("essential_monthly", essential_monthly)
    bal = resolve("current_balance", current_balance)
    sur = resolve("monthly_surplus", monthly_surplus, allow_negative=True)
    inputs = [months_iv, ess, bal, sur]
    checks = input_checks(inputs)
    if months_iv.value is not None and months_iv.value <= 0:
        checks.append(_fixed(BLOCK, "months_positive", "months must be at least 1"))
    if ess.value is not None and ess.value == 0:
        # A zero target would SHIP as "fund complete" on no information.
        checks.append(
            _fixed(HOLD, "essential_positive", "essential monthly expenses of zero; enter the real amount")
        )
    assumptions = [
        months_note,
        ("The emergency fund is assumed to be held in liquid, low-risk savings; growth on it is ignored."),
        "The monthly surplus is assumed to be fully set aside until the target is met.",
        *assumption_notes(inputs),
    ]
    result: dict[str, Any] | None = None
    if _ok(inputs) and months_iv.value is not None and months_iv.value > 0:
        assert ess.value is not None and bal.value is not None and sur.value is not None
        target = months_iv.value * ess.value
        gap = max(Decimal(0), target - bal.value)
        to_target: int | str
        if gap == 0:
            to_target = 0
        elif sur.value <= 0:
            to_target = "never"
            checks.append(
                _fixed(HOLD, "unreachable", "no monthly surplus: the target is never reached at this rate")
            )
        else:
            to_target = int((gap / sur.value).to_integral_value(rounding=ROUND_CEILING))
        result = {
            "months": int(months_iv.value),
            "target": target,
            "current_balance": bal.value,
            "gap": gap,
            "months_to_target": to_target,
            "months_of_cover": (bal.value / ess.value).quantize(Decimal("0.1"), ROUND_HALF_UP)
            if ess.value
            else None,
        }
    return finalize("emergency_fund", inputs=inputs, assumptions=assumptions, result=result, checks=checks)


def net_worth(*, assets: Mapping[str, Raw], liabilities: Mapping[str, Raw]) -> dict[str, Any]:
    """Net worth = total assets - total liabilities (a negative result is reported)."""
    a = _resolve_map("asset", assets)
    li = _resolve_map("liability", liabilities)
    inputs = [*a, *li]
    checks = input_checks(inputs)
    if not inputs:
        checks.append(_fixed(HOLD, "nothing_listed", "no assets or liabilities listed"))
    assumptions = [
        "Assets are at the user-supplied current value; no market revaluation is done.",
        "Items not listed are treated as absent.",
        *assumption_notes(inputs),
    ]
    result: dict[str, Any] | None = None
    if inputs and _ok(inputs):
        ta, tl = _sum(a), _sum(li)
        result = {"total_assets": ta, "total_liabilities": tl, "net_worth": ta - tl}
    return finalize("net_worth", inputs=inputs, assumptions=assumptions, result=result, checks=checks)
