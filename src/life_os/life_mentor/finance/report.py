"""Shared report contract for every LM04 calculator.

Every calculator returns a dict with exactly these top-level keys:

    calculator   name of the calculator
    inputs       {name: {value, provenance, basis[, rule_id]}}
    rules_used   [{id, source_url, source_tier, retrieved_on, verified_by_human,
                   unverified, flags, act_section_1961, act_section_2025}]
    assumptions  [str] - scope limits and non-verified inputs, always explicit
    result       calculator-specific dict, or None when inputs are missing/invalid
    gate         life_os.core.gate GateResult.as_dict() - SHIP / HOLD / BLOCK

No combined score (LM-HONEST). Every report is passed through the LM02
forbidden-field scanner before it is returned (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from life_os.core.gate import Check, run_gate

from ..research import RulePack, RuleValue, rule_status, rules_gate
from ..security import assert_report_clean
from .inputs import InputValue

__all__ = ["finalize", "fixed_check", "gate_reason_checks", "rule_checks", "rule_citations"]


def rule_citations(
    pack: RulePack, ids: Iterable[str], as_of: date, *, effective_on: date | None = None
) -> list[dict[str, Any]]:
    wanted = list(dict.fromkeys(ids))
    statuses = {s.id: s for s in rule_status(pack, as_of=as_of, ids=wanted, effective_on=effective_on)}
    out: list[dict[str, Any]] = []
    for rid in wanted:
        try:
            rv = pack.get(rid)
        except KeyError:
            continue
        st = statuses[rid]
        out.append(_citation(rv, list(st.flags)))
    return out


def _citation(rv: RuleValue, flags: list[str]) -> dict[str, Any]:
    return {
        "id": rv.id,
        "source_url": rv.source_url,
        "source_tier": rv.source_tier,
        "retrieved_on": rv.retrieved_on.isoformat(),
        "verified_by_human": rv.verified_by_human,
        "unverified": rv.unverified,
        "flags": flags,
        "act_section_1961": rv.extra.get("act_section_1961"),
        "act_section_2025": rv.extra.get("act_section_2025"),
    }


def gate_reason_checks(reasons: Iterable[dict[str, Any]]) -> list[Check]:
    """Re-express already-computed gate reasons as checks (keeps granularity)."""
    return [_fixed_check(str(r["check"]), str(r["disposition"]), str(r["reason"])) for r in reasons]


def fixed_check(disposition: str, name: str, reason: str) -> Check:
    """A check with a precomputed (disposition, reason)."""
    return _fixed_check(name, disposition, reason)


def _fixed_check(name: str, disposition: str, reason: str) -> Check:
    def fn() -> tuple[str, str]:
        return disposition, reason

    return Check(name=name, fn=fn)


def rule_checks(
    pack: RulePack,
    ids: Sequence[str],
    as_of: date,
    *,
    require_verified: bool = False,
    effective_on: date | None = None,
) -> list[Check]:
    gate = rules_gate(
        pack, as_of=as_of, ids=ids, require_verified=require_verified, effective_on=effective_on
    )
    return gate_reason_checks(gate.reasons)


def finalize(
    calculator: str,
    *,
    inputs: Iterable[InputValue],
    assumptions: Iterable[str],
    result: dict[str, Any] | None,
    checks: list[Check],
    rules_used: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gate = run_gate(f"life_os.life_mentor.finance.{calculator}", checks)
    report: dict[str, Any] = {
        "calculator": calculator,
        "inputs": {iv.name: iv.as_dict() for iv in inputs},
        "rules_used": rules_used or [],
        "assumptions": list(dict.fromkeys(assumptions)),
        "result": result,
        "gate": gate.as_dict(),
    }
    if extra:
        report.update(extra)
    assert_report_clean(report)
    return report
