"""Debt payoff schedules: avalanche and snowball (LM04).

Both strategies are simulated month by month with the same constant budget:

  1. interest accrues on each balance at annual_rate_percent / 12 / 100,
     rounded to the paisa (half up);
  2. every open debt receives its minimum payment (capped at its balance);
  3. the rest of the budget goes to the target debt, cascading on payoff -
       avalanche: highest rate first (ties: smaller balance, then name);
       snowball:  smallest current balance first (ties: higher rate, then name).

Fail closed:
  - minimum payments above the budget       -> BLOCK (plan is infeasible);
  - balances not falling, or not cleared within ``max_months`` (default 600)
    -> status "never", months None, HOLD. The loop is always bounded.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..security import redact
from .inputs import InputValue, Raw, assumption_notes, input_checks, resolve
from .money import format_inr, quantize_paise
from .report import finalize

__all__ = ["DEFAULT_MAX_MONTHS", "STRATEGIES", "Debt", "debt_payoff"]

DEFAULT_MAX_MONTHS = 600
STRATEGIES = ("avalanche", "snowball")

DEBT_ASSUMPTIONS = (
    (
        "Interest accrues monthly at annual_rate_percent/12 on the outstanding balance, "
        "rounded to the paisa; payments are made at month end after interest."
    ),
    ("The monthly budget stays constant; minimums freed by a paid-off debt roll into the target debt."),
    "No fees, penalties, prepayment charges, rate changes or new borrowing are modelled.",
)


@dataclass(frozen=True)
class Debt:
    name: str
    balance: Raw
    annual_rate_percent: Raw
    minimum_payment: Raw


@dataclass(frozen=True)
class _D:
    name: str
    balance: Decimal
    rate: Decimal
    minimum: Decimal


def _fixed(disposition: str, name: str, reason: str) -> Check:
    return Check(name=name, fn=lambda: (disposition, reason))


def _val(iv: InputValue) -> Decimal:
    if iv.value is None:  # unreachable once every input check has shipped
        raise ValueError(f"{iv.name}: missing")
    return iv.value


def _order_key(strategy: str) -> Callable[[tuple[_D, Decimal]], tuple[Any, ...]]:
    if strategy == "avalanche":
        return lambda item: (-item[0].rate, item[1], item[0].name)
    return lambda item: (item[1], -item[0].rate, item[0].name)


# A balance that has grown to this multiple of the starting total is treated as never
# clearing (no realistic payoff first grows a hundredfold).
RUNAWAY_FACTOR = Decimal(100)


def _simulate(debts: list[_D], budget: Decimal, strategy: str, max_months: int) -> dict[str, Any]:
    bal = {d.name: d.balance for d in debts}
    key = _order_key(strategy)
    schedule: list[dict[str, Any]] = []
    payoff_month: dict[str, int] = {d.name: 0 for d in debts if d.balance == 0}
    total_interest = Decimal(0)
    total_paid = Decimal(0)
    month = 0
    status = "paid_off"
    runaway = RUNAWAY_FACTOR * max(sum(bal.values(), Decimal(0)), Decimal(1))
    while any(v > 0 for v in bal.values()):
        if month >= max_months:
            status = "never"
            break
        month += 1
        interest: dict[str, Decimal] = {}
        for d in debts:
            if bal[d.name] > 0:
                i = quantize_paise(bal[d.name] * d.rate / 1200)
                interest[d.name] = i
                bal[d.name] += i
                total_interest += i
        remaining = budget
        payments: dict[str, Decimal] = {}
        for d in debts:
            if bal[d.name] > 0:
                p = min(d.minimum, bal[d.name])
                payments[d.name] = p
                bal[d.name] -= p
                remaining -= p
        for d, _ in sorted(((d, bal[d.name]) for d in debts if bal[d.name] > 0), key=key):
            if remaining <= 0:
                break
            p = min(remaining, bal[d.name])
            payments[d.name] = payments.get(d.name, Decimal(0)) + p
            bal[d.name] -= p
            remaining -= p
        total_paid += sum(payments.values(), Decimal(0))
        for name, v in bal.items():
            if v == 0 and name not in payoff_month:
                payoff_month[name] = month
        schedule.append({"month": month, "interest": interest, "payments": payments, "balances": dict(bal)})
        # Do NOT stop just because the total failed to fall in one month: under avalanche
        # the total can rise for a while as the high-rate debt shrinks, then fall (this
        # used to report such plans as "never"). Only a runaway balance ends early, which
        # also keeps the numbers inside Decimal precision.
        if sum(bal.values(), Decimal(0)) > runaway:
            status = "never"
            break
    order = sorted(payoff_month, key=lambda n: (payoff_month[n], n))
    return {
        "status": status,
        "months": month if status == "paid_off" else None,
        "total_interest": total_interest,
        "total_paid": total_paid,
        "order": order,
        "payoff_month": payoff_month,
        "remaining_balance": sum(bal.values(), Decimal(0)),
        "schedule": schedule,
    }


def debt_payoff(
    debts: Sequence[Debt],
    *,
    monthly_budget: Raw,
    max_months: int = DEFAULT_MAX_MONTHS,
) -> dict[str, Any]:
    """Avalanche and snowball schedules side by side (no combined ranking)."""
    if isinstance(max_months, bool) or not isinstance(max_months, int) or max_months < 1:
        raise ValueError("max_months must be a positive int")
    budget_iv = resolve("monthly_budget", monthly_budget)
    inputs: list[InputValue] = [budget_iv]
    checks: list[Check] = []
    names: list[str] = []
    parsed: list[tuple[str, InputValue, InputValue, InputValue]] = []
    for d in debts:
        name = redact(d.name.strip()) if isinstance(d.name, str) else ""
        if not name:
            checks.append(_fixed(BLOCK, "debt_name", "every debt needs a non-empty name"))
            name = f"debt_{len(names)}"
        if name in names:
            checks.append(_fixed(BLOCK, "debt_name", f"duplicate debt name {name!r}"))
        names.append(name)
        trio = (
            resolve(f"{name}.balance", d.balance),
            resolve(f"{name}.annual_rate_percent", d.annual_rate_percent),
            resolve(f"{name}.minimum_payment", d.minimum_payment),
        )
        parsed.append((name, *trio))
        inputs.extend(trio)
    checks = [*input_checks(inputs), *checks]
    if not debts:
        checks.append(_fixed(HOLD, "debts", "no debts supplied"))
    assumptions = [*DEBT_ASSUMPTIONS, *assumption_notes(inputs)]

    blocked = any(c.fn()[0] != SHIP for c in checks)
    result: dict[str, Any] | None = None
    if not blocked:
        assert budget_iv.value is not None
        ds = [_D(n, _val(b), _val(r), _val(m)) for n, b, r, m in parsed]
        minimums = sum((d.minimum for d in ds if d.balance > 0), Decimal(0))
        if minimums > budget_iv.value:
            checks.append(
                _fixed(
                    BLOCK,
                    "minimums_exceed_budget",
                    f"minimum payments {format_inr(minimums)} exceed the monthly "
                    f"budget {format_inr(budget_iv.value)}",
                )
            )
        else:
            warnings = [
                f"{d.name}: minimum payment does not cover its first month's interest"
                for d in ds
                if d.balance > 0 and d.minimum <= quantize_paise(d.balance * d.rate / 1200)
            ]
            runs = {s: _simulate(ds, budget_iv.value, s, max_months) for s in STRATEGIES}
            for s, run in runs.items():
                if run["status"] == "never":
                    checks.append(
                        _fixed(
                            HOLD,
                            f"{s}_never",
                            f"{s}: debts are not cleared within {max_months} months at this budget "
                            f"(payments do not outpace interest)",
                        )
                    )
                else:
                    checks.append(_fixed(SHIP, f"{s}_paid_off", f"{s}: cleared in {run['months']} months"))
            av, sb = runs["avalanche"], runs["snowball"]
            comparison: dict[str, Any] = {
                "interest_saved_by_avalanche": sb["total_interest"] - av["total_interest"],
                "months_avalanche": av["months"],
                "months_snowball": sb["months"],
            }
            result = {**runs, "comparison": comparison, "warnings": warnings}
    return finalize("debt_payoff", inputs=inputs, assumptions=assumptions, result=result, checks=checks)
