"""Human confirmation of a decision (LM05, LM-HUMAN).

Life Mentor never executes anything. ``record_confirmation`` only writes the
human's choice to the hash-chained decision journal (ids and dispositions, no
money values) so the history shows who chose what, and whether the choice
overrode a HOLD. The report must hash to its own ``decision_id`` (recomputed
exactly as ``analyze_decision`` made it), so an edited report - e.g. a BLOCK
flipped to SHIP - is refused. A BLOCKed option cannot be confirmed: BLOCK means the
analysis found something wrong (e.g. essentials would not be covered).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from life_os.core.canonical import canonical_sha256
from life_os.core.gate import BLOCK, HOLD
from life_os.core.identity import Identity, get_identity

from ..profile.facts import encode_value
from ..security import PrivateStore
from .engine import DECISION_JOURNAL

__all__ = ["record_confirmation"]


def record_confirmation(
    store: PrivateStore,
    report: dict[str, Any],
    option_name: str,
    *,
    identity: Identity | None = None,
    clock: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Journal that a human chose *option_name* from *report*. Never executes it."""
    result: dict[str, Any] | None = report.get("result")
    if not result:
        raise ValueError("report has no result; nothing can be confirmed")
    body = {k: v for k, v in report.items() if k not in ("decision_id", "journal")}
    try:
        recomputed = canonical_sha256(encode_value(body))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"report cannot be re-hashed ({exc}); refusing") from exc
    if recomputed != report.get("decision_id"):
        raise ValueError(
            "report does not match its decision_id (edited after analysis); "
            "re-run analyze_decision and confirm the fresh report"
        )
    options = {o["name"]: o for o in result.get("options", [])}
    if option_name not in options:
        raise ValueError(f"option {option_name!r} is not in this decision report")
    disposition = options[option_name]["gate"]["disposition"]
    if disposition == BLOCK:
        raise ValueError(
            f"option {option_name!r} is BLOCKed; it cannot be confirmed - "
            f"resolve its blocking checks and re-run the analysis"
        )
    # The report-level gate carries structural problems (duplicate option names,
    # profile category mismatch) and profile HOLDs that no single option sees.
    overall = report.get("gate", {}).get("disposition")
    if overall == BLOCK:
        raise ValueError(
            "the decision report as a whole is BLOCKed; resolve its blocking checks and re-run the analysis"
        )
    who = identity or get_identity()
    # Record the confirmation under the caller's clock so a run pinned to a past
    # date (the CLI's --generated-on) stays internally consistent with the report
    # it feeds; defaults to the real wall clock when no clock is given.
    return store.journal(DECISION_JOURNAL, clock=clock).append(
        {
            "event": "decision_confirmed_by_human",
            "decision_id": report["decision_id"],
            "option": option_name,
            "option_disposition": disposition,
            "report_disposition": overall,
            "override_of_hold": HOLD in (disposition, overall),
            "confirmed_by": who.principal(),
            "executed": False,
        }
    )
