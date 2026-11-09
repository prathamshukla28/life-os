"""Calculator inputs with provenance (LM04, LM-PROV).

Every calculator argument may be:

  - ``Decimal`` / ``int``  -> recorded as USER_ESTIMATE ("supplied directly");
  - a profile ``Fact``     -> its own provenance (UNKNOWN means missing);
  - a ``RuleValue``        -> provenance RULE, cited in ``rules_used``;
  - ``None``               -> missing.

``float`` raises ``TypeError`` immediately (money is never float). A Fact or
RuleValue holding a non-number is a contradiction and becomes a BLOCK check.
Gate semantics (fail closed): missing/UNKNOWN -> HOLD, invalid/negative ->
BLOCK, ASSUMPTION/USER_ESTIMATE -> SHIP but listed in the report assumptions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..profile import ASSUMPTION, UNKNOWN, USER_ESTIMATE, Fact
from ..research import RuleValue
from ..security import redact
from .money import to_money

__all__ = [
    "RULE",
    "InputValue",
    "Raw",
    "assumption_notes",
    "input_checks",
    "resolve",
]

RULE = "RULE"
Raw = Decimal | int | Fact | RuleValue | None


@dataclass(frozen=True)
class InputValue:
    """One resolved calculator input."""

    name: str
    value: Decimal | None
    provenance: str
    basis: str
    rule: RuleValue | None = None
    error: str | None = None
    allow_negative: bool = False
    integer: bool = False

    def __post_init__(self) -> None:
        if self.value is not None and not isinstance(self.value, Decimal):
            raise TypeError(
                f"{self.name}: InputValue.value must be Decimal or None, got {type(self.value).__name__}"
            )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "value": self.value,
            "provenance": self.provenance,
            "basis": redact(self.basis),
        }
        if self.rule is not None:
            out["rule_id"] = self.rule.id
        return out


def resolve(
    name: str,
    raw: Raw,
    *,
    allow_negative: bool = False,
    integer: bool = False,
    provenance_override: str | None = None,
) -> InputValue:
    """Resolve *raw* into an ``InputValue`` (TypeError on float/unsupported)."""
    name = redact(name)
    common: dict[str, Any] = {"allow_negative": allow_negative, "integer": integer}
    if raw is None:
        return InputValue(name, None, UNKNOWN, "not supplied", **common)
    if isinstance(raw, Fact):
        if raw.provenance == UNKNOWN:
            return InputValue(name, None, UNKNOWN, raw.basis, **common)
        try:
            value = to_money(name, raw.value)
        except (TypeError, ValueError) as exc:
            return InputValue(
                name, None, raw.provenance, raw.basis, error=f"profile fact is not a number ({exc})", **common
            )
        return _checked(InputValue(name, value, raw.provenance, raw.basis, **common))
    if isinstance(raw, RuleValue):
        try:
            value = to_money(name, raw.value)
        except (TypeError, ValueError) as exc:
            return InputValue(
                name,
                None,
                RULE,
                f"rule {raw.id}",
                rule=raw,
                error=f"rule value is not a number ({exc})",
                **common,
            )
        return _checked(InputValue(name, value, RULE, f"rule {raw.id}", rule=raw, **common))
    prov = provenance_override or USER_ESTIMATE
    basis = "supplied directly to the calculator"
    if isinstance(raw, Decimal) and not raw.is_finite():
        # NaN/Infinity become a BLOCKing input error (they used to raise ValueError
        # out of every calculator). Floats still raise TypeError: that is a bug in
        # the caller, not bad data.
        return InputValue(name, None, prov, basis, error="not a finite number", **common)
    value = to_money(name, raw)  # raises TypeError on float
    return _checked(InputValue(name, value, prov, basis, **common))


def _checked(iv: InputValue) -> InputValue:
    if iv.integer and iv.value is not None and iv.value != iv.value.to_integral_value():
        return InputValue(
            iv.name,
            None,
            iv.provenance,
            iv.basis,
            iv.rule,
            error="must be a whole number",
            allow_negative=iv.allow_negative,
            integer=True,
        )
    return iv


def input_checks(inputs: Iterable[InputValue]) -> list[Check]:
    """One gate check per input."""
    return [_check(iv) for iv in inputs]


def _check(iv: InputValue) -> Check:
    def fn() -> tuple[str, str]:
        if iv.error is not None:
            return BLOCK, f"{iv.name}: {iv.error}"
        if iv.value is None:
            return HOLD, f"{iv.name}: missing or UNKNOWN; please provide it"
        if iv.value < 0 and not iv.allow_negative:
            return BLOCK, f"{iv.name}: negative value is contradictory"
        return SHIP, f"{iv.name}: present ({iv.provenance})"

    return Check(name=f"input:{iv.name}", fn=fn)


def assumption_notes(inputs: Iterable[InputValue]) -> list[str]:
    """Human-readable notes for every input that is not VERIFIED."""
    notes: list[str] = []
    for iv in inputs:
        if iv.value is None:
            continue
        basis = redact(iv.basis)
        if iv.provenance == ASSUMPTION:
            notes.append(f"{iv.name} is an ASSUMPTION ({basis}); confirm before relying on it.")
        elif iv.provenance == USER_ESTIMATE:
            notes.append(f"{iv.name} is a user estimate ({basis}), not a verified figure.")
    return notes
