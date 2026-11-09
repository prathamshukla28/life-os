"""Money helpers (LM04, LM-MONEY): Decimal only, never float.

``to_money`` accepts ``Decimal`` or ``int`` (not ``bool``) and raises
``TypeError`` for ``float`` or anything else - consistent with
``life_os.life_mentor.profile.facts``. ``round_288`` implements the Indian
income-tax rounding convention; ``format_inr`` renders Indian digit grouping
for explanation strings (commas also keep long amounts from looking like
account numbers to the PII redactor).
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

__all__ = ["PAISA", "format_inr", "quantize_paise", "round_288", "to_money"]

PAISA = Decimal("0.01")


def to_money(name: str, raw: Any) -> Decimal:
    """Coerce *raw* to Decimal; float/bool/str are rejected with TypeError."""
    if isinstance(raw, bool) or not isinstance(raw, (Decimal, int)):
        if isinstance(raw, float):
            raise TypeError(f"{name}: float is not allowed for money; use Decimal")
        raise TypeError(f"{name}: expected Decimal or int, got {type(raw).__name__}")
    value = raw if isinstance(raw, Decimal) else Decimal(raw)
    if not value.is_finite():
        raise ValueError(f"{name}: non-finite amount")
    return value


def quantize_paise(amount: Decimal) -> Decimal:
    """Round to the paisa, half up."""
    return amount.quantize(PAISA, rounding=ROUND_HALF_UP)


def round_288(amount: Decimal, multiple: Decimal) -> Decimal:
    """Round to the nearest *multiple* of rupees, Indian income-tax style.

    Convention of s.288A (total income) and s.288B (tax) of the Income-tax Act,
    1961: any part of a rupee (paise) is first ignored; then, if the amount is
    not a multiple of ten, a last figure of five or more is increased to the
    next higher multiple and a last figure below five is reduced to the next
    lower multiple. *multiple* comes from the rule pack (not hard-coded).
    Generalised as: remainder * 2 >= multiple rounds up. Negative amounts are
    not meaningful here and are rejected.
    """
    if amount < 0:
        raise ValueError("round_288 expects a non-negative amount")
    if multiple <= 0 or multiple != multiple.to_integral_value():
        raise ValueError("rounding multiple must be a positive whole number")
    whole = amount.to_integral_value(rounding=ROUND_FLOOR)
    remainder = whole % multiple
    down = whole - remainder
    return down + multiple if remainder * 2 >= multiple else down


def format_inr(amount: Decimal) -> str:
    """Indian grouping, e.g. Decimal('1250000.5') -> '12,50,000.50'."""
    # Round to paise FIRST, so a carry (12.999 -> 13.00) reaches the rupees and a
    # value that rounds to zero is not printed as "-Rs 0.00".
    q = quantize_paise(abs(amount))
    sign = "-" if amount < 0 and q != 0 else ""
    whole = int(q)
    frac = q - whole
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    if frac:
        digits += "." + f"{frac:.2f}".split(".")[1]
    return f"{sign}Rs {digits}"
