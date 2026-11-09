"""Dated RulePacks: cited reference values with a staleness gate (LM03, LM-RULE).

A RulePack is a JSON file of rule values (tax slabs, rates, thresholds):

    {"pack_id": "...", "title": "...", "values": [
        {"id": "...", "value": <number | str | list | dict>, "unit": "...",
         "effective_from": "YYYY-MM-DD", "effective_to": "YYYY-MM-DD" | null,
         "source_url": "https://...", "source_tier": "primary" | "secondary",
         "retrieved_on": "YYYY-MM-DD", "verified_by_human": true | false,
         "refresh_cadence_days": <int > 0>, ...extra keys kept verbatim}]}

Loading (``load_rule_pack``) fails closed with ``RulePackBlocked`` (-> BLOCK)
on ANY invalid value, most importantly a missing/empty ``source_url``. All
numbers inside ``value`` become ``Decimal`` - never float.

Using (``rule_status`` / ``rules_gate``) is pure and takes an explicit
``as_of`` date (no hidden clock):
  - stale        as_of - retrieved_on > refresh_cadence_days      -> HOLD
  - not_in_effect as_of outside [effective_from, effective_to]     -> HOLD
  - future_retrieval retrieved_on after as_of (contradiction)      -> BLOCK
  - unverified   source_tier == secondary or not verified_by_human -> flagged
                 in the reasons (SHIP), or HOLD with require_verified=True
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check, GateResult, run_gate

from .sources import SOURCE_TIERS, parse_iso_date, valid_source_url

__all__ = [
    "RulePack",
    "RulePackBlocked",
    "RuleStatus",
    "RuleValue",
    "check_rule_pack_file",
    "load_rule_pack",
    "rule_status",
    "rules_gate",
]

_REQUIRED = (
    "id",
    "value",
    "unit",
    "effective_from",
    "source_url",
    "source_tier",
    "retrieved_on",
    "verified_by_human",
    "refresh_cadence_days",
)
_KNOWN = (*_REQUIRED, "effective_to")


class RulePackBlocked(ValueError):
    """A RulePack failed validation. Carries every reason (maps to BLOCK)."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("rule pack blocked: " + "; ".join(self.reasons))


@dataclass(frozen=True)
class RuleValue:
    id: str
    value: Any
    unit: str
    effective_from: date
    effective_to: date | None
    source_url: str
    source_tier: str
    retrieved_on: date
    verified_by_human: bool
    refresh_cadence_days: int
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def unverified(self) -> bool:
        return self.source_tier != "primary" or not self.verified_by_human


@dataclass(frozen=True)
class RulePack:
    pack_id: str
    title: str
    values: tuple[RuleValue, ...]
    extra: dict[str, Any] = field(default_factory=dict)

    def ids(self) -> list[str]:
        return [v.id for v in self.values]

    def get(self, rule_id: str) -> RuleValue:
        for v in self.values:
            if v.id == rule_id:
                return v
        raise KeyError(rule_id)


@dataclass(frozen=True)
class RuleStatus:
    id: str
    stale: bool
    unverified: bool
    in_effect: bool
    future_retrieval: bool
    age_days: int
    flags: tuple[str, ...]


# -- loading ----------------------------------------------------------------


_MAX_VALUE_DEPTH = 32


def _decimalize(obj: Any, depth: int = 0) -> Any:
    """Every number becomes Decimal (bool stays bool). Floats go via str().

    Nesting deeper than ``_MAX_VALUE_DEPTH`` is rejected (ValueError -> BLOCK).
    """
    if depth > _MAX_VALUE_DEPTH:
        raise ValueError(f"nested deeper than {_MAX_VALUE_DEPTH} levels")
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float, Decimal)):
        dec = obj if isinstance(obj, Decimal) else Decimal(str(obj))
        if not dec.is_finite():
            raise ValueError("non-finite number")
        return dec
    if isinstance(obj, list):
        return [_decimalize(v, depth + 1) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _decimalize(v, depth + 1) for k, v in obj.items()}
    raise ValueError(f"unsupported value type {type(obj).__name__}")


def _parse_value(raw: Any, pos: int, reasons: list[str]) -> RuleValue | None:
    if not isinstance(raw, dict):
        reasons.append(f"values[{pos}]: not an object")
        return None
    rid = raw.get("id")
    label = f"values[{pos}] ({rid!r})"
    before = len(reasons)
    for key in _REQUIRED:
        if key not in raw:
            reasons.append(f"{label}: missing {key}")
    if not isinstance(rid, str) or not rid.strip():
        reasons.append(f"{label}: id must be a non-empty string")
    if "source_url" in raw and not valid_source_url(raw.get("source_url")):
        reasons.append(f"{label}: source_url missing or not an http(s) URL")
    if "source_tier" in raw and raw.get("source_tier") not in SOURCE_TIERS:
        reasons.append(f"{label}: source_tier must be one of {SOURCE_TIERS}")
    if "unit" in raw and not isinstance(raw.get("unit"), str):
        reasons.append(f"{label}: unit must be a string")
    if "verified_by_human" in raw and not isinstance(raw.get("verified_by_human"), bool):
        reasons.append(f"{label}: verified_by_human must be true/false")
    cadence = raw.get("refresh_cadence_days")
    if "refresh_cadence_days" in raw and (
        isinstance(cadence, bool) or not isinstance(cadence, int) or cadence <= 0
    ):
        reasons.append(f"{label}: refresh_cadence_days must be a positive integer")
    dates: dict[str, date | None] = {}
    for key in ("effective_from", "retrieved_on", "effective_to"):
        if key not in raw or (key == "effective_to" and raw[key] is None):
            dates[key] = None
            continue
        try:
            dates[key] = parse_iso_date(raw[key], key)
        except ValueError as exc:
            reasons.append(f"{label}: {exc}")
    eff_from, eff_to = dates.get("effective_from"), dates.get("effective_to")
    if eff_from is not None and eff_to is not None and eff_to < eff_from:
        reasons.append(f"{label}: effective_to precedes effective_from")
    try:
        value = _decimalize(raw.get("value"))
    except ValueError as exc:
        reasons.append(f"{label}: value {exc}")
        value = None
    if "value" in raw and raw.get("value") is None:
        reasons.append(f"{label}: value is null")
    retrieved = dates.get("retrieved_on")
    if len(reasons) != before or eff_from is None or retrieved is None:
        return None
    if not isinstance(rid, str) or not isinstance(cadence, int):
        return None
    return RuleValue(
        id=rid,
        value=value,
        unit=raw["unit"],
        effective_from=eff_from,
        effective_to=eff_to,
        source_url=raw["source_url"],
        source_tier=raw["source_tier"],
        retrieved_on=retrieved,
        verified_by_human=raw["verified_by_human"],
        refresh_cadence_days=cadence,
        extra={k: v for k, v in raw.items() if k not in _KNOWN},
    )


def _read(source: str | os.PathLike[str] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    path = Path(source)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulePackBlocked([f"cannot read rule pack {path}: {exc.strerror}"]) from exc
    try:
        data = json.loads(text, parse_float=Decimal)
    except ValueError as exc:
        raise RulePackBlocked([f"rule pack {path} is not valid JSON: {exc}"]) from exc
    if not isinstance(data, dict):
        raise RulePackBlocked([f"rule pack {path} must be a JSON object"])
    return data


def load_rule_pack(source: str | os.PathLike[str] | dict[str, Any]) -> RulePack:
    """Load and validate a RulePack from a path or dict. Raises ``RulePackBlocked``."""
    data = _read(source)
    reasons: list[str] = []
    pack_id, title, raw_values = data.get("pack_id"), data.get("title"), data.get("values")
    if not isinstance(pack_id, str) or not pack_id.strip():
        reasons.append("pack_id must be a non-empty string")
    if not isinstance(title, str):
        reasons.append("title must be a string")
    if not isinstance(raw_values, list) or not raw_values:
        reasons.append("values must be a non-empty list")
        raw_values = []
    parsed: list[RuleValue] = []
    seen: set[str] = set()
    for pos, raw in enumerate(raw_values):
        rv = _parse_value(raw, pos, reasons)
        if rv is None:
            continue
        if rv.id in seen:
            reasons.append(f"duplicate rule id {rv.id!r}")
        seen.add(rv.id)
        parsed.append(rv)
    if reasons:
        raise RulePackBlocked(reasons)
    return RulePack(
        pack_id=str(pack_id),
        title=str(title),
        values=tuple(parsed),
        extra={k: v for k, v in data.items() if k not in ("pack_id", "title", "values")},
    )


# -- staleness / verification ------------------------------------------------


def _as_date(as_of: Any) -> date:
    if isinstance(as_of, datetime):
        return as_of.date()
    if not isinstance(as_of, date):
        raise TypeError("as_of must be a datetime.date")
    return as_of


def _status_of(rv: RuleValue, as_of: date, effective_on: date | None = None) -> RuleStatus:
    """Staleness is judged at *as_of*; effectiveness at *effective_on* (default as_of)."""
    on = as_of if effective_on is None else effective_on
    age = (as_of - rv.retrieved_on).days
    stale = age > rv.refresh_cadence_days
    future = age < 0
    in_effect = rv.effective_from <= on and (rv.effective_to is None or on <= rv.effective_to)
    flags = [
        name
        for name, on in (
            ("stale", stale),
            ("not_in_effect", not in_effect),
            ("future_retrieval", future),
            ("unverified", rv.unverified),
        )
        if on
    ]
    return RuleStatus(
        id=rv.id,
        stale=stale,
        unverified=rv.unverified,
        in_effect=in_effect,
        future_retrieval=future,
        age_days=age,
        flags=tuple(flags),
    )


def rule_status(
    pack: RulePack,
    *,
    as_of: date,
    ids: Iterable[str] | None = None,
    effective_on: date | None = None,
) -> list[RuleStatus]:
    """Per-value status (pure). Freshness at *as_of*; in-effect at *effective_on*
    (defaults to as_of) - e.g. the start of the tax year being computed (LM-G21)."""
    day = _as_date(as_of)
    on = None if effective_on is None else _as_date(effective_on)
    wanted = None if ids is None else set(ids)
    return [_status_of(v, day, on) for v in pack.values if wanted is None or v.id in wanted]


def rules_gate(
    pack: RulePack,
    *,
    as_of: date,
    ids: Iterable[str] | None = None,
    require_verified: bool = False,
    name: str = "life_os.life_mentor.rules",
    effective_on: date | None = None,
) -> GateResult:
    """Fail-closed gate over the rule values an analysis will use.

    Staleness uses *as_of*; "in effect" uses *effective_on* (default as_of).
    """
    day = _as_date(as_of)
    on = None if effective_on is None else _as_date(effective_on)
    wanted = list(ids) if ids is not None else pack.ids()
    checks: list[Check] = []
    for rid in wanted:
        checks.append(_rule_check(pack, rid, day, require_verified, on))
    return run_gate(name, checks)


def _rule_check(
    pack: RulePack, rid: str, day: date, require_verified: bool, effective_on: date | None = None
) -> Check:
    def fn() -> tuple[str, str]:
        try:
            rv = pack.get(rid)
        except KeyError:
            return HOLD, f"rule {rid!r} is not in pack {pack.pack_id!r}"
        st = _status_of(rv, day, effective_on)
        on = day if effective_on is None else effective_on
        cite = f"(source {rv.source_url}, tier {rv.source_tier}, retrieved {rv.retrieved_on})"
        if st.future_retrieval:
            return BLOCK, f"{rid}: retrieved_on {rv.retrieved_on} is after as_of {day} {cite}"
        problems: list[str] = []
        if st.stale:
            problems.append(
                f"stale: {st.age_days} days since retrieval > refresh cadence {rv.refresh_cadence_days}"
            )
        if not st.in_effect:
            problems.append(f"not_in_effect: {on} outside {rv.effective_from} to {rv.effective_to or 'open'}")
        if st.unverified and require_verified:
            problems.append("unverified: secondary source or not human-verified")
        if problems:
            return HOLD, f"{rid}: " + "; ".join(problems) + f" {cite}"
        note = " UNVERIFIED (secondary or not human-verified)" if st.unverified else ""
        return SHIP, f"{rid}: fresh and in effect{note} {cite}"

    return Check(name=f"rule:{rid}", fn=fn)


def check_rule_pack_file(
    path: str | os.PathLike[str],
    *,
    as_of: date,
    ids: Iterable[str] | None = None,
    require_verified: bool = False,
) -> tuple[GateResult, RulePack | None]:
    """Load + gate in one step: an invalid file is BLOCK (pack None)."""
    try:
        pack = load_rule_pack(path)
    except RulePackBlocked as exc:
        reasons = [{"check": "load", "disposition": BLOCK, "reason": r} for r in exc.reasons]
        return GateResult(BLOCK, "RULEPACK_INVALID", reasons), None
    return rules_gate(pack, as_of=as_of, ids=ids, require_verified=require_verified), pack
