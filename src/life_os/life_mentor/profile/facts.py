"""Typed personal facts with provenance (LM01, LM-PROV).

A ``Fact`` is one thing the user told us (or explicitly could not tell us):

    key          stable snake-ish identifier, e.g. "take_home_monthly"
    value        str | int | bool | Decimal | list/dict of those | None
    category     identity | finance | health | career | lifestyle | goals
    provenance   VERIFIED | USER_ESTIMATE | ASSUMPTION | UNKNOWN
    basis        where the value came from ("payslip Sep 2026", "guess")
    recorded_at  ISO-8601 UTC timestamp

Invariants enforced at construction (fail closed):
  - unknown category/provenance -> ValueError;
  - ``float`` anywhere in the value -> TypeError (money is Decimal/paise);
  - UNKNOWN must carry ``value=None``; every other provenance must carry a value.

``basis`` is PII-redacted at construction, so a card number or email typed
into it never reaches ``facts.json``. ``repr()`` shows key, category,
provenance and timestamp only - never the value or basis.

Decimals serialise as ``{"$decimal": "85000.50"}`` so canonical JSON (which
forbids Decimal) round-trips exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..security.redact import redact

__all__ = [
    "ASSUMPTION",
    "CATEGORIES",
    "PROVENANCES",
    "UNKNOWN",
    "USER_ESTIMATE",
    "VERIFIED",
    "Fact",
    "decode_value",
    "encode_value",
]

CATEGORIES = ("identity", "finance", "health", "career", "lifestyle", "goals")

VERIFIED = "VERIFIED"
USER_ESTIMATE = "USER_ESTIMATE"
ASSUMPTION = "ASSUMPTION"
UNKNOWN = "UNKNOWN"
PROVENANCES = (VERIFIED, USER_ESTIMATE, ASSUMPTION, UNKNOWN)

_DECIMAL_TAG = "$decimal"


def encode_value(value: Any) -> Any:
    """Convert a fact value to canonical-JSON-safe data (Decimal tagged)."""
    if isinstance(value, float):
        raise TypeError("float is not allowed in facts; use Decimal (money is never float)")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal is not allowed")
        return {_DECIMAL_TAG: str(value)}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [encode_value(v) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError("fact dict keys must be str")
            if k == _DECIMAL_TAG:
                raise ValueError(f"key {_DECIMAL_TAG!r} is reserved")
            out[k] = encode_value(v)
        return out
    raise TypeError(f"unsupported fact value type: {type(value).__name__}")


def decode_value(data: Any) -> Any:
    """Inverse of ``encode_value``."""
    if isinstance(data, dict):
        if set(data) == {_DECIMAL_TAG}:
            return Decimal(data[_DECIMAL_TAG])
        return {k: decode_value(v) for k, v in data.items()}
    if isinstance(data, list):
        return [decode_value(v) for v in data]
    if isinstance(data, float):
        raise TypeError("float found in stored fact; refusing to load")
    return data


@dataclass(frozen=True, repr=False)
class Fact:
    """One user fact with provenance. Immutable; corrections create a new Fact."""

    key: str
    value: Any
    category: str
    provenance: str
    basis: str
    recorded_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("fact key must be a non-empty string")
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category!r}; expected one of {CATEGORIES}")
        if self.provenance not in PROVENANCES:
            raise ValueError(f"unknown provenance {self.provenance!r}; expected one of {PROVENANCES}")
        if not isinstance(self.basis, str):
            raise TypeError("basis must be a string")
        object.__setattr__(self, "basis", redact(self.basis))
        encode_value(self.value)  # type/float validation
        if self.provenance == UNKNOWN and self.value is not None:
            raise ValueError("an UNKNOWN fact must have value=None")
        if self.provenance != UNKNOWN and self.value is None:
            raise ValueError(f"a {self.provenance} fact needs a value (use UNKNOWN for none)")

    def __repr__(self) -> str:
        return (
            f"Fact(key={self.key!r}, category={self.category!r}, "
            f"provenance={self.provenance!r}, recorded_at={self.recorded_at!r})"
        )

    @property
    def is_known(self) -> bool:
        return self.provenance != UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": encode_value(self.value),
            "category": self.category,
            "provenance": self.provenance,
            "basis": self.basis,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fact:
        return cls(
            key=data["key"],
            value=decode_value(data["value"]),
            category=data["category"],
            provenance=data["provenance"],
            basis=data["basis"],
            recorded_at=data["recorded_at"],
        )
