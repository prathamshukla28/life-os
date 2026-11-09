"""Goal SIP: required monthly contribution for a target corpus (LM04).

For each scenario (conservative / base / optimistic) with annual return R%:

    i = R / 12 / 100                      (nominal rate, monthly compounding)
    g = (1 + i) ** n
    factor = n                            if i == 0
           = ((g - 1) / i) * (1 + i)      otherwise (contribution at month start)
    need = target - current_savings * g
    P = ceil_to_rupee(need / factor)      (0 if need <= 0)

Return rates are ASSUMPTIONS (user-supplied, or a cited ``RuleValue`` such as
a published small-savings rate) - never promises. Scenario rates must be
ordered conservative <= base <= optimistic, otherwise the inputs contradict
each other (BLOCK).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import ROUND_CEILING, Decimal, DecimalException, localcontext
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..profile import ASSUMPTION
from ..research import RulePack, RuleValue
from .inputs import InputValue, Raw, assumption_notes, input_checks, resolve
from .money import quantize_paise
from .report import finalize, rule_checks, rule_citations

__all__ = ["SCENARIOS", "goal_sip"]

SCENARIOS = ("conservative", "base", "optimistic")

SIP_ASSUMPTIONS = (
    (
        "Annual return rates are assumptions, not promises; actual returns vary and can be "
        "negative. Compare the three scenarios rather than relying on one."
    ),
    (
        "Returns compound monthly at the nominal annual rate / 12; contributions are made at "
        "the start of each month and stay constant."
    ),
    ("Taxes, fees, expense ratios and inflation are not deducted; the target is in nominal rupees."),
)


def _fixed(disposition: str, name: str, reason: str) -> Check:
    return Check(name=name, fn=lambda: (disposition, reason))


def _ok(iv: InputValue) -> bool:
    return iv.error is None and iv.value is not None and (iv.allow_negative or iv.value >= 0)


# Plausibility bounds: beyond these the compounding maths leaves Decimal's working
# precision (and the answer is meaningless anyway), so the gate BLOCKs instead.
MAX_MONTHS = 1200  # 100 years
MAX_ANNUAL_RETURN_PERCENT = Decimal(100)


def _scenario(target: Decimal, months: int, savings: Decimal, rate_pct: Decimal) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = 50
        i = rate_pct / 1200
        if i == 0:
            growth, factor = Decimal(1), Decimal(months)
        else:
            growth = (1 + i) ** months
            factor = (growth - 1) / i * (1 + i)
        need = target - savings * growth
        monthly = (need / factor).to_integral_value(rounding=ROUND_CEILING) if need > 0 else (Decimal(0))
        projected = monthly * factor + savings * growth
        # Quantize inside the high-precision context (it used to happen outside, at the
        # default 28 digits, and raised InvalidOperation for large horizons/rates).
        contributed = monthly * months
        return {
            "annual_return_percent": rate_pct,
            "monthly_contribution": monthly,
            "total_contributed": contributed,
            "projected_corpus": quantize_paise(projected),
            "growth_portion": quantize_paise(projected - contributed - savings),
        }


def goal_sip(
    *,
    target_corpus: Raw,
    months: int | None,
    annual_return_percent: Mapping[str, Raw],
    current_savings: Raw = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Required monthly SIP per return scenario. ``as_of`` is needed for rule rates."""
    target = resolve("target_corpus", target_corpus)
    if months is not None and (isinstance(months, bool) or not isinstance(months, int)):
        raise TypeError("months must be an int")
    months_iv = resolve("months", months, integer=True)
    if current_savings is None:
        savings = InputValue(
            "current_savings", Decimal(0), ASSUMPTION, "not supplied; treated as nothing saved yet"
        )
    else:
        savings = resolve("current_savings", current_savings)
    rates: dict[str, InputValue] = {}
    for s in SCENARIOS:
        raw = annual_return_percent.get(s)
        iv = resolve(f"annual_return_percent:{s}", raw, allow_negative=True, provenance_override=ASSUMPTION)
        rates[s] = iv
    inputs = [target, months_iv, savings, *rates.values()]
    checks = input_checks(inputs)
    extra_keys = sorted(set(annual_return_percent) - set(SCENARIOS))
    if extra_keys:
        checks.append(_fixed(BLOCK, "scenarios", f"unknown scenario names {extra_keys}"))
    if target.value is not None and target.value == 0:
        checks.append(_fixed(BLOCK, "target_positive", "target_corpus must be above zero"))
    if months_iv.value is not None and months_iv.value <= 0:
        checks.append(_fixed(BLOCK, "months_positive", "months must be at least 1"))
    if months_iv.value is not None and months_iv.value > MAX_MONTHS:
        checks.append(_fixed(BLOCK, "months_bound", f"months must be at most {MAX_MONTHS} (100 years)"))
    for s, iv in rates.items():
        if iv.value is not None and iv.value <= -100:
            checks.append(_fixed(BLOCK, f"rate:{s}", f"{s}: a return of -100% or less is invalid"))
        if iv.value is not None and iv.value > MAX_ANNUAL_RETURN_PERCENT:
            checks.append(
                _fixed(
                    BLOCK,
                    f"rate:{s}",
                    f"{s}: an annual return above {MAX_ANNUAL_RETURN_PERCENT}% is implausible",
                )
            )
    known = [v for v in (rates[s].value for s in SCENARIOS) if v is not None]
    if len(known) == len(SCENARIOS) and known != sorted(known):
        checks.append(
            _fixed(BLOCK, "scenario_order", "returns must satisfy conservative <= base <= optimistic")
        )

    rules_used: list[dict[str, Any]] = []
    rule_vals: list[RuleValue] = [iv.rule for iv in rates.values() if iv.rule is not None]
    if rule_vals:
        pack = RulePack(
            pack_id="sip_inline",
            title="rates supplied to goal_sip",
            values=tuple({rv.id: rv for rv in rule_vals}.values()),
        )
        if as_of is None:
            checks.append(_fixed(HOLD, "as_of", "as_of is required to check rule-supplied rates"))
        else:
            checks.extend(rule_checks(pack, pack.ids(), as_of))
            rules_used = rule_citations(pack, pack.ids(), as_of)

    assumptions = [*SIP_ASSUMPTIONS, *assumption_notes(inputs)]
    result: dict[str, Any] | None = None
    blocked = any(c.fn()[0] == BLOCK for c in checks)
    rate_vals: dict[str, Decimal] = {}
    for s in SCENARIOS:
        rv_value = rates[s].value
        if rv_value is not None:
            rate_vals[s] = rv_value
    tv, mv, sv = target.value, months_iv.value, savings.value
    ready = all(_ok(iv) for iv in (target, months_iv, savings)) and len(rate_vals) == len(SCENARIOS)
    if not blocked and ready and tv is not None and mv is not None and sv is not None:
        n = int(mv)
        try:
            scenarios = {s: _scenario(tv, n, sv, r) for s, r in rate_vals.items()}
        except DecimalException:
            checks.append(
                _fixed(BLOCK, "computed", "inputs are outside the range this calculator can compute")
            )
        else:
            result = {"target_corpus": tv, "months": n, "current_savings": sv, "scenarios": scenarios}
            checks.append(_fixed(SHIP, "computed", "monthly contribution computed per scenario"))
    return finalize(
        "goal_sip",
        inputs=inputs,
        assumptions=assumptions,
        result=result,
        checks=checks,
        rules_used=rules_used,
    )
