# Vendored from the Asur monorepo (asur/core/gate.py).
# Copyright (c) 2026 prathamshukla28. Licensed under the MIT License (see LICENSE).
"""The fail-closed disposition gate.

A gate answers one question - "may this advance?" - with a *disposition* in a
closed vocabulary:

    SHIP   - advance; every check passed.
    HOLD   - do not advance yet; something is missing but not wrong
             (e.g. an input was not supplied or a value is stale).
    BLOCK  - refuse; something is wrong (contradictory input, a failed
             integrity check, a check that raised).

Fail-closed means the default outcome of an unknown or missing condition is
never SHIP: absence caps at HOLD, contradiction caps at BLOCK, and a check that
raises or returns an unknown disposition is BLOCK.

(The Asur original also carried an ``approval_gate`` for producer != approver
review; Life OS does not use it, so it was not vendored. ``GateResult`` keeps
its ``self_approved`` field for serialisation compatibility.)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BLOCK",
    "HOLD",
    "SHIP",
    "Check",
    "GateResult",
    "run_gate",
]

SHIP = "SHIP"
HOLD = "HOLD"
BLOCK = "BLOCK"

# Severity ordering so we can take the "worst" outcome across many checks.
_RANK = {SHIP: 0, HOLD: 1, BLOCK: 2}


@dataclass
class Check:
    """One named condition evaluated by a gate.

    ``fn`` returns a tuple (disposition, reason). A check that merely passes
    returns (SHIP, "..."); a check that wants changes returns (HOLD, ...);
    a check that detects something wrong returns (BLOCK, ...).
    """

    name: str
    fn: Callable[[], tuple[str, str]]


@dataclass
class GateResult:
    """The outcome of running a gate: a disposition plus an explainable trail."""

    disposition: str
    substate: str | None
    reasons: list[dict[str, str]] = field(default_factory=list)
    self_approved: bool = False

    @property
    def passed(self) -> bool:
        return self.disposition == SHIP

    def as_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "substate": self.substate,
            "reasons": list(self.reasons),
            "self_approved": self.self_approved,
        }


def _worst(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def run_gate(name: str, checks: list[Check]) -> GateResult:
    """Evaluate *checks* and return the worst (most restrictive) disposition.

    Fail-closed: if ``checks`` is empty we HOLD (nothing was actually
    verified, so we cannot SHIP). Any check that raises is treated as BLOCK -
    an error in a safety check must never silently pass.
    """
    if not checks:
        return GateResult(
            disposition=HOLD,
            substate="NO_CHECKS",
            # A dict like every other reason, so consumers that read r["check"] /
            # r["reason"] (reports, renderers) never crash on the empty case.
            reasons=[
                {
                    "check": "no_checks",
                    "disposition": HOLD,
                    "reason": f"gate {name!r}: no checks supplied; cannot advance",
                }
            ],
        )

    disposition = SHIP
    reasons: list[dict[str, str]] = []
    for check in checks:
        try:
            result, reason = check.fn()
        except Exception as exc:  # fail closed on a broken check
            result, reason = BLOCK, f"check {check.name!r} raised: {exc!r}"
        if result not in _RANK:  # unknown disposition -> fail closed
            result, reason = BLOCK, f"check {check.name!r} returned unknown: {result!r}"
        reasons.append({"check": check.name, "disposition": result, "reason": reason})
        disposition = _worst(disposition, result)

    return GateResult(disposition=disposition, substate=None, reasons=reasons)
