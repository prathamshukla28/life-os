"""Append-only metric log and periodic reviews (LM07).

The log lives in the private store as a hash-chained journal
(``tracking/metrics.jsonl``, with its head anchor). Unlike the consent/profile audit
journals it is a DATA log, so it holds the values; each Decimal is stored as an
integer coefficient + exponent (exact, and never mistaken for an account number by
the redactor). Nothing is overwritten: a second value for the same metric and day
must go through ``correct``; both events stay in the history.

Reviews compare the latest window with the previous one and with an optional
target. They describe what changed, never why: correlation is not causation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from life_os.core.clock import utc_now_iso
from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.inputs import InputValue, Raw, input_checks, resolve
from ..finance.money import to_money
from ..finance.report import finalize, fixed_check
from ..profile import (
    ASSUMPTION,
    USER_ESTIMATE,
    VERIFIED,
    ConsentLedger,
    ConsentRequired,
)
from ..security import Journal, PrivateStore

__all__ = [
    "CAUSATION_NOTE",
    "METRICS",
    "METRICS_JOURNAL",
    "MIN_POINTS",
    "PERIODS",
    "MetricLog",
    "review",
]

METRICS_JOURNAL = "tracking/metrics.jsonl"
# metric -> (consent category, unit, negative values allowed)
METRICS: dict[str, tuple[str, str, bool]] = {
    "weight_kg": ("health", "kg", False),
    "sleep_hours": ("health", "hours", False),
    "exercise_minutes": ("lifestyle", "minutes", False),
    "income": ("finance", "INR", False),
    "expenses": ("finance", "INR", False),
    "savings": ("finance", "INR", False),
    "net_worth": ("finance", "INR", True),
    "debt": ("finance", "INR", False),
    "habit_completion_percent": ("lifestyle", "percent", False),
}
# Physical upper bounds for one day's value (values above these are typos, not data).
MAX_VALUE: dict[str, Decimal] = {
    "sleep_hours": Decimal(24),
    "exercise_minutes": Decimal(1440),
    "habit_completion_percent": Decimal(100),
}
PERIODS = {"weekly": 7, "monthly": 30}
MIN_POINTS = 3
DIRECTIONS = ("at_least", "at_most")
_RECORDABLE = (VERIFIED, USER_ESTIMATE, ASSUMPTION)
_Q = Decimal("0.01")
CAUSATION_NOTE = (
    "Correlation is not causation: these numbers show what changed, not why. "
    "No change is attributed to any action or programme."
)


def _encode(value: Decimal) -> dict[str, int]:
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise TypeError("non-finite value")
    coef = int("".join(map(str, digits)) or "0")
    return {"coef": -coef if sign else coef, "exp": exponent}


def _decode(data: dict[str, int]) -> Decimal:
    return Decimal(data["coef"]).scaleb(data["exp"])


class MetricLog:
    def __init__(self, store: PrivateStore, *, clock: Callable[[], str] = utc_now_iso) -> None:
        self.consent = ConsentLedger(store, clock=clock)
        self.journal: Journal = store.journal(METRICS_JOURNAL, clock=clock)

    def _validate(self, metric: str, value: Any, provenance: str) -> Decimal:
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric!r}; expected one of {sorted(METRICS)}")
        amount = to_money(metric, value)
        if amount < 0 and not METRICS[metric][2]:
            raise ValueError(f"{metric} cannot be negative")
        if metric in MAX_VALUE and amount > MAX_VALUE[metric]:
            raise ValueError(f"{metric} cannot exceed {MAX_VALUE[metric]} per day")
        if provenance not in _RECORDABLE:
            raise ValueError(f"provenance must be one of {_RECORDABLE}")
        self.consent.require(METRICS[metric][0])
        return amount

    def _points(self, metric: str) -> dict[date, dict[str, Any]]:
        latest: dict[date, dict[str, Any]] = {}
        for entry in self.journal.entries():
            ev = entry["event"]
            if ev.get("metric") == metric and ev.get("event") in ("metric_record", "metric_correct"):
                latest[date.fromisoformat(ev["day"])] = ev
        return latest

    def record(self, metric: str, value: Decimal, day: date, *, provenance: str, basis: str) -> None:
        amount = self._validate(metric, value, provenance)
        if day in self._points(metric):
            raise ValueError(
                f"{metric} already has a value for {day}; use correct() so the change is kept in history"
            )
        self.journal.append(
            {
                "event": "metric_record",
                "metric": metric,
                "day": day.isoformat(),
                "value": _encode(amount),
                "provenance": provenance,
                "basis": basis,
            }
        )

    def correct(self, metric: str, value: Decimal, day: date, *, provenance: str, basis: str) -> None:
        amount = self._validate(metric, value, provenance)
        if day not in self._points(metric):
            raise ValueError(f"no {metric} value recorded for {day}")
        self.journal.append(
            {
                "event": "metric_correct",
                "metric": metric,
                "day": day.isoformat(),
                "value": _encode(amount),
                "provenance": provenance,
                "basis": basis,
            }
        )

    def series(self, metric: str) -> list[tuple[date, Decimal]]:
        """Current value per day (latest correction wins), oldest first. Needs consent."""
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric!r}")
        self.consent.require(METRICS[metric][0])
        return [(d, _decode(ev["value"])) for d, ev in sorted(self._points(metric).items())]

    def provenance_counts(self, metric: str, start: date, end: date) -> dict[str, int]:
        self.consent.require(METRICS[metric][0])
        counts: dict[str, int] = {}
        for d, ev in self._points(metric).items():
            if start <= d <= end:
                counts[ev["provenance"]] = counts.get(ev["provenance"], 0) + 1
        return counts


def _avg(values: list[Decimal]) -> Decimal:
    return (sum(values, Decimal(0)) / len(values)).quantize(_Q, ROUND_HALF_UP)


def review(
    log: MetricLog,
    metric: str,
    *,
    as_of: date,
    period: str = "weekly",
    target: Raw = None,
    target_direction: str | None = None,
    min_points: int = MIN_POINTS,
) -> dict[str, Any]:
    """Compare the latest window with the previous one and an optional target."""
    checks: list[Check] = []
    inputs: list[InputValue] = []
    if metric not in METRICS:
        checks.append(fixed_check(BLOCK, "metric", f"unknown metric {metric!r}"))
    if period not in PERIODS:
        checks.append(fixed_check(BLOCK, "period", f"period must be one of {sorted(PERIODS)}"))
    if isinstance(min_points, bool) or not isinstance(min_points, int) or min_points < 1:
        # 0 used to reach _avg([]) and raise decimal.InvalidOperation.
        checks.append(fixed_check(BLOCK, "min_points", "min_points must be a whole number of at least 1"))
        min_points = MIN_POINTS
    tgt: InputValue | None = None
    if target is not None:
        tgt = resolve("target", target, allow_negative=True)
        inputs.append(tgt)
        checks += input_checks([tgt])
        if target_direction not in DIRECTIONS:
            checks.append(
                fixed_check(BLOCK, "target_direction", f"target_direction must be one of {DIRECTIONS}")
            )
    assumptions = [
        CAUSATION_NOTE,
        (
            f"Windows: the last {PERIODS.get(period, '?')} days ending {as_of.isoformat()} vs the "
            f"{PERIODS.get(period, '?')} days before; trend = difference of window averages and "
            f"last minus first value in the current window. At least {min_points} points needed."
        ),
    ]
    result: dict[str, Any] | None = None
    series: list[tuple[date, Decimal]] = []
    if not any(c.fn()[0] == BLOCK for c in checks):
        try:
            series = log.series(metric)
        except ConsentRequired as exc:
            checks.append(
                fixed_check(HOLD, "consent", f"consent for {exc.category!r} is needed to read {metric}")
            )
    if not any(c.fn()[0] != SHIP for c in checks if c.name != "target"):
        days = PERIODS[period]
        cur_start = as_of - timedelta(days=days - 1)
        prev_start = cur_start - timedelta(days=days)
        cur = [v for d, v in series if cur_start <= d <= as_of]
        prev = [v for d, v in series if prev_start <= d < cur_start]
        if len(cur) < min_points:
            checks.append(
                fixed_check(
                    HOLD,
                    "points",
                    f"insufficient data: {len(cur)} points in "
                    f"the current window, at least {min_points} needed",
                )
            )
        else:
            avg = _avg(cur)
            result = {
                "metric": metric,
                "unit": METRICS[metric][1],
                "period": period,
                "current": {
                    "start": cur_start.isoformat(),
                    "end": as_of.isoformat(),
                    "points": len(cur),
                    "average": avg,
                },
                "previous": {"points": len(prev), "average": _avg(prev) if len(prev) >= min_points else None},
                "change_in_average": (avg - _avg(prev)) if len(prev) >= min_points else None,
                "first_to_last_in_current": (cur[-1] - cur[0]).quantize(_Q, ROUND_HALF_UP),
                "provenance_counts": log.provenance_counts(metric, cur_start, as_of),
                "note": CAUSATION_NOTE,
            }
            if len(prev) < min_points:
                assumptions.append("The previous window has too few points; no comparison made.")
            if tgt is not None and tgt.value is not None:
                met = avg >= tgt.value if target_direction == "at_least" else avg <= tgt.value
                result["target"] = {
                    "value": tgt.value,
                    "direction": target_direction,
                    "met": met,
                    "difference": (avg - tgt.value).quantize(_Q, ROUND_HALF_UP),
                }
            checks.append(fixed_check(SHIP, "computed", f"{len(cur)} points reviewed"))
    return finalize("metric_review", inputs=inputs, assumptions=assumptions, result=result, checks=checks)
