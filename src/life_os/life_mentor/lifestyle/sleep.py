"""Sleep log analysis against the cited AASM/SRS 2015 adult consensus (LM07).

Duration and consistency are computed from the user's own log; the only reference
value is ">= 7 hours per night on a regular basis" for adults 18-60
(``rules/sleep_reference.json``). Persistent short sleep (more than half of the
logged nights under 6 h - a Life Mentor policy threshold) or any reported symptoms
-> HOLD with a suggestion to talk to a clinician. No diagnosis; symptom text is never
echoed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.inputs import Raw, assumption_notes, input_checks, resolve
from ..finance.report import finalize, fixed_check, rule_checks, rule_citations
from ..research import RulePack, load_rule_pack

__all__ = [
    "MIN_NIGHTS",
    "RECENCY_DAYS",
    "SHORT_SLEEP_CLINICIAN_HOURS",
    "SLEEP_REFS_PATH",
    "SleepEntry",
    "analyze_sleep",
    "load_sleep_refs",
]

SLEEP_REFS_PATH = Path(__file__).resolve().parent.parent / "rules" / "sleep_reference.json"
MIN_NIGHTS = 7
RECENCY_DAYS = 14  # Life Mentor policy (LM-G59): only nights within 14 days of as_of count
SHORT_SLEEP_CLINICIAN_HOURS = Decimal(6)
MAX_PLAUSIBLE_MINUTES = 16 * 60
_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_IDS = ("adult_sleep_minimum_hours", "adult_sleep_age_band")


def load_sleep_refs(path: str | Path = SLEEP_REFS_PATH) -> RulePack:
    return load_rule_pack(path)


@dataclass(frozen=True)
class SleepEntry:
    night: date
    bedtime: str
    wake_time: str


def _minutes(text: str) -> int | None:
    m = _HHMM.fullmatch(text) if isinstance(text, str) else None
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _pstdev(values: list[Decimal]) -> Decimal:
    mean = sum(values, Decimal(0)) / len(values)
    return (sum(((v - mean) ** 2 for v in values), Decimal(0)) / len(values)).sqrt()


def _circular_sd(minutes: list[int]) -> Decimal:
    """SD of clock times on a 24 h circle: each time is taken as its signed distance
    (within +-12 h) from an anchor, and the anchor among the observed times that gives the
    smallest spread is used - so 23:50 and 00:10 are 20 minutes apart, not 1420."""
    best: Decimal | None = None
    for anchor in sorted(set(minutes)):
        deltas = [Decimal((m - anchor + 720) % 1440 - 720) for m in minutes]
        sd = _pstdev(deltas)
        best = sd if best is None or sd < best else best
    return best if best is not None else Decimal(0)


def _parse(
    entries: Sequence[SleepEntry], as_of: date, checks: list[Check]
) -> list[tuple[date, int, int, int]]:
    """[(night, duration_min, bedtime_min, wake_min)] for the valid entries."""
    out: list[tuple[date, int, int, int]] = []
    seen: set[date] = set()
    for e in entries:
        bed, wake = _minutes(e.bedtime), _minutes(e.wake_time)
        label = e.night.isoformat()
        if bed is None or wake is None:
            checks.append(fixed_check(BLOCK, f"night:{label}", "times must be HH:MM (00:00-23:59)"))
            continue
        duration = (wake - bed) % 1440
        if duration == 0 or duration > MAX_PLAUSIBLE_MINUTES:
            checks.append(fixed_check(BLOCK, f"night:{label}", "implausible sleep duration"))
            continue
        if e.night in seen:
            checks.append(fixed_check(BLOCK, f"night:{label}", "two entries for the same night"))
            continue
        if e.night > as_of:
            checks.append(fixed_check(BLOCK, f"night:{label}", "night is after as_of"))
            continue
        seen.add(e.night)
        out.append((e.night, duration, bed, wake))
    return out


def analyze_sleep(
    entries: Sequence[SleepEntry], *, refs: RulePack, as_of: date, age: Raw, symptoms: str | None = None
) -> dict[str, Any]:
    """Average duration, bedtime/wake consistency (SD, minutes) and flags vs >= 7 h."""
    checks: list[Check] = []
    age_iv = resolve("age", age, integer=True)
    inputs = [age_iv]
    checks += input_checks(inputs)
    checks += rule_checks(refs, _IDS, as_of)
    have = all(i in refs.ids() for i in _IDS)
    if have and age_iv.value is not None:
        band = refs.get("adult_sleep_age_band").value
        if not band["min"] <= age_iv.value <= band["max"]:
            checks.append(
                fixed_check(
                    HOLD,
                    "age_band",
                    f"the cited reference covers adults "
                    f"{band['min']}-{band['max']}; discuss sleep needs at your "
                    f"age with a clinician",
                )
            )
    parsed = _parse(entries, as_of, checks)
    # The last RECENCY_DAYS nights, as_of included (`<=` used to keep 15 nights for 14).
    nights = [n for n in parsed if (as_of - n[0]).days < RECENCY_DAYS]
    if parsed and len(nights) < len(parsed):
        newest = max(n[0] for n in parsed)
        if (as_of - newest).days >= RECENCY_DAYS:
            checks.append(
                fixed_check(
                    HOLD,
                    "recency",
                    f"the newest logged night ({newest}) is "
                    f"more than {RECENCY_DAYS} days before {as_of}; log "
                    f"recent nights",
                )
            )
    if len(nights) < MIN_NIGHTS:
        checks.append(
            fixed_check(
                HOLD,
                "nights",
                f"insufficient data: {len(nights)} nights "
                f"in the last {RECENCY_DAYS} days, at least {MIN_NIGHTS} "
                f"needed",
            )
        )
    if symptoms is not None and symptoms.strip():
        checks.append(
            fixed_check(
                HOLD, "symptoms", "you described sleep symptoms; please discuss them with a clinician"
            )
        )
    assumptions = [
        (
            "Consistency = population standard deviation (minutes) of bedtime and of wake time on "
            "a 24-hour circle, so times either side of midnight or noon compare correctly. A Life "
            "Mentor measure, not a cited threshold."
        ),
        (
            f"Persistent short sleep = more than half of logged nights under "
            f"{SHORT_SLEEP_CLINICIAN_HOURS} h (Life Mentor policy, not a cited threshold)."
        ),
        "No diagnosis. Durations are time in bed as you logged it.",
        f"Only nights within {RECENCY_DAYS} days of the as-of date are analysed (policy).",
        *assumption_notes(inputs),
    ]
    result: dict[str, Any] | None = None
    if have and nights and len(parsed) == len(entries) and not any(c.fn()[0] == BLOCK for c in checks):
        reference = refs.get("adult_sleep_minimum_hours").value
        durations = [Decimal(d) for _, d, _, _ in nights]
        avg_hours = (sum(durations, Decimal(0)) / len(durations) / 60).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )
        below = sum(1 for d in durations if d < reference * 60)
        short = sum(1 for d in durations if d < SHORT_SLEEP_CLINICIAN_HOURS * 60)
        flags: list[str] = []
        if avg_hours < reference:
            flags.append(f"average below the {reference} h adult reference")
        if short * 2 > len(durations):
            flags.append("persistently short sleep")
            checks.append(
                fixed_check(
                    HOLD,
                    "short_sleep",
                    "most logged nights are under "
                    f"{SHORT_SLEEP_CLINICIAN_HOURS} h; consider talking to a "
                    f"clinician",
                )
            )
        result = {
            "nights": len(nights),
            "average_hours": avg_hours,
            "bedtime_sd_minutes": _circular_sd([b for _, _, b, _ in nights]).quantize(Decimal("0.1")),
            "wake_sd_minutes": _circular_sd([w for _, _, _, w in nights]).quantize(Decimal("0.1")),
            "nights_below_reference": below,
            "nights_under_short_threshold": short,
            "reference_hours": reference,
            "meets_reference_on_average": avg_hours >= reference,
            "flags": flags,
        }
        checks.append(fixed_check(SHIP, "computed", f"{len(nights)} nights analysed"))
    return finalize(
        "sleep_analysis",
        inputs=inputs,
        assumptions=assumptions,
        result=result,
        checks=checks,
        rules_used=rule_citations(refs, _IDS, as_of),
    )
