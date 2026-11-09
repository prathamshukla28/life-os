"""Exercise plan generator checked against the cited WHO 2020 adult guidelines (LM07).

The weekly layout (how many strength vs aerobic days, mobility minutes, movement
lists, 10 %/week progression cap) is a Life Mentor HEURISTIC and is labelled as such
in every report; only the guideline thresholds come from the cited rule pack
``rules/activity_reference.json``. Medical limitations or listed conditions -> HOLD
("clear with your clinician or physiotherapist"); no medical clearance is implied.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.inputs import InputValue, Raw, assumption_notes, input_checks, resolve
from ..finance.report import (
    finalize,
    fixed_check,
    gate_reason_checks,
    rule_checks,
    rule_citations,
)
from ..profile import UNKNOWN, ConsentRequired, Profile, Requirement, profile_gate
from ..research import RulePack, load_rule_pack

__all__ = [
    "ACTIVITY_REFS_PATH",
    "CLINICIAN_PHYSIO_LINE",
    "EQUIPMENT",
    "EXPERIENCE_LEVELS",
    "ExerciseInputs",
    "check_activity",
    "load_activity_refs",
    "plan_exercise",
    "plan_exercise_from_profile",
    "progression",
]

ACTIVITY_REFS_PATH = Path(__file__).resolve().parent.parent / "rules" / "activity_reference.json"
EXPERIENCE_LEVELS = ("beginner", "intermediate", "advanced")
MOVEMENTS: dict[str, tuple[str, ...]] = {
    "none": ("squat", "push-up (incline if needed)", "glute bridge", "plank", "reverse lunge"),
    "resistance_bands": ("band squat", "band row", "band chest press", "band deadlift", "pallof press"),
    "dumbbells": (
        "goblet squat",
        "dumbbell row",
        "dumbbell bench or floor press",
        "romanian deadlift",
        "overhead press",
    ),
    "gym": ("leg press or squat", "lat pulldown", "chest press", "hip hinge / deadlift", "seated row"),
}
EQUIPMENT = tuple(MOVEMENTS)
MAX_MINUTES_PER_SESSION = 240
MAX_PROGRESSION_PERCENT = Decimal(10)
CLINICIAN_PHYSIO_LINE = "Clear with your clinician or physiotherapist before starting this plan."

_GUIDELINE_IDS = (
    "who_adult_aerobic_moderate_minutes_per_week",
    "who_adult_aerobic_vigorous_minutes_per_week",
    "who_adult_muscle_strengthening_days_per_week",
    "who_adult_age_band",
    "who_any_activity_better_than_none",
)
HEURISTIC_NOTE = (
    "Heuristic, not a cited guideline: mobility = 10% of each session (minimum 5 minutes); "
    "2 strength days (3 for advanced with 4+ days); remaining days aerobic at moderate "
    "intensity; movement lists are generic templates."
)


def load_activity_refs(path: str | Path = ACTIVITY_REFS_PATH) -> RulePack:
    return load_rule_pack(path)


@dataclass(frozen=True)
class ExerciseInputs:
    experience: str
    days_per_week: int | None
    minutes_per_session: int | None
    equipment: tuple[str, ...]
    medical_limitations: bool | None
    conditions: tuple[str, ...] | None
    age: Raw
    goals: tuple[str, ...] = ()


def _range_value(refs: RulePack, rid: str) -> dict[str, Decimal]:
    value = refs.get(rid).value
    if not isinstance(value, dict) or "min" not in value:
        raise ValueError(f"{rid}: expected an object with min/max")
    return value


def _guideline_rows(
    refs: RulePack, moderate: Decimal, vigorous: Decimal, strength_days: int
) -> tuple[list[dict[str, Any]], str]:
    mod = _range_value(refs, "who_adult_aerobic_moderate_minutes_per_week")
    vig = _range_value(refs, "who_adult_aerobic_vigorous_minutes_per_week")
    ratio = mod["min"] / vig["min"]
    equivalent = moderate + vigorous * ratio
    strength_min = refs.get("who_adult_muscle_strengthening_days_per_week").value
    rows = [
        {
            "criterion": "aerobic",
            "moderate_minutes": moderate,
            "vigorous_minutes": vigorous,
            "moderate_equivalent_minutes": equivalent,
            "reference_min": mod["min"],
            "reference_upper": mod["max"],
            "met": equivalent >= mod["min"],
            "moderate_equivalent_gap_minutes": max(Decimal(0), mod["min"] - equivalent),
            "beyond_upper_for_additional_benefit": equivalent > mod["max"],
        },
        {
            "criterion": "muscle_strengthening",
            "days": strength_days,
            "reference_min_days": strength_min,
            "met": strength_days >= strength_min,
            "gap_days": max(Decimal(0), strength_min - strength_days),
        },
    ]
    note = (
        f"For combined activity, 1 minute of vigorous activity is counted as {ratio} minutes "
        f"of moderate activity (the ratio of WHO's {mod['min']}- and {vig['min']}-minute "
        f"lower bounds); WHO allows 'an equivalent combination' without a fixed ratio here."
    )
    return rows, note


def _week(
    days: int, minutes: int, strength: int, equipment: Sequence[str]
) -> tuple[list[dict[str, Any]], int, int]:
    mobility = min(minutes, max(5, math.ceil(minutes / 10)))
    main = minutes - mobility
    positions = {int(k * days / strength) for k in range(strength)} if strength else set()
    moves: list[str] = []
    for eq in equipment:
        moves += [m for m in MOVEMENTS[eq] if m not in moves]
    week = [
        {
            "day": f"Day {i + 1}",
            "type": "strength" if i in positions else "aerobic",
            "main_minutes": main,
            "mobility_minutes": mobility,
            "intensity": "moderate",
            "movements": moves[:5] if i in positions else ["brisk walk, cycle or similar"],
        }
        for i in range(days)
    ]
    return week, mobility, main


def _plan_parts(
    inp: ExerciseInputs, refs: RulePack, as_of: date
) -> tuple[list[InputValue], list[str], dict[str, Any] | None, list[Check], list[dict[str, Any]]]:
    checks: list[Check] = []
    assumptions = [HEURISTIC_NOTE, "General information only; no medical clearance is implied."]
    age = resolve("age", inp.age, integer=True)
    inputs = [age]
    checks += input_checks(inputs)
    if inp.experience not in EXPERIENCE_LEVELS:
        checks.append(fixed_check(BLOCK, "experience", f"experience must be one of {EXPERIENCE_LEVELS}"))
    if not inp.equipment or any(e not in MOVEMENTS for e in inp.equipment):
        checks.append(fixed_check(BLOCK, "equipment", f"equipment must be from {EQUIPMENT}"))
    days, minutes = inp.days_per_week, inp.minutes_per_session
    for name, val, hi in (
        ("days_per_week", days, 7),
        ("minutes_per_session", minutes, MAX_MINUTES_PER_SESSION),
    ):
        if val is None:
            checks.append(fixed_check(HOLD, name, f"{name}: missing; please provide it"))
        elif isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= hi:
            checks.append(fixed_check(BLOCK, name, f"{name} must be a whole number 1..{hi}"))
    if inp.medical_limitations is None:
        checks.append(
            fixed_check(
                HOLD,
                "medical_limitations",
                "Do you have any medical limitation affecting exercise? Answer yes or no",
            )
        )
    elif inp.medical_limitations:
        checks.append(
            fixed_check(HOLD, "medical_limitations", "medical limitation reported; " + CLINICIAN_PHYSIO_LINE)
        )
    if inp.conditions is None:
        checks.append(
            fixed_check(HOLD, "conditions", "Do you have any health conditions? Give an empty list if none")
        )
    elif any(c.strip() for c in inp.conditions):
        checks.append(
            fixed_check(
                HOLD, "conditions", f"{len(inp.conditions)} condition(s) listed; " + CLINICIAN_PHYSIO_LINE
            )
        )
    ids = [i for i in _GUIDELINE_IDS if i in refs.ids()]
    checks += rule_checks(refs, _GUIDELINE_IDS, as_of)
    rules_used = rule_citations(refs, ids, as_of)
    if age.value is not None and "who_adult_age_band" in ids:
        band = _range_value(refs, "who_adult_age_band")
        if not band["min"] <= age.value <= band["max"]:
            checks.append(
                fixed_check(
                    HOLD,
                    "age_band",
                    f"WHO adult recommendations cover ages "
                    f"{band['min']}-{band['max']}; other recommendations are "
                    f"not bundled. " + CLINICIAN_PHYSIO_LINE,
                )
            )
    if "who_any_activity_better_than_none" in ids:
        assumptions.append("WHO: any amount of physical activity is better than none.")
    result: dict[str, Any] | None = None
    structural = any(c.fn()[0] == BLOCK for c in checks)
    if (
        not structural
        and isinstance(days, int)
        and isinstance(minutes, int)
        and len(ids) == len(_GUIDELINE_IDS)
    ):
        strength = min(days, 3 if inp.experience == "advanced" and days >= 4 else 2)
        week, mobility, main = _week(days, minutes, strength, inp.equipment)
        aerobic_minutes = Decimal(main * (days - strength))
        rows, note = _guideline_rows(refs, aerobic_minutes, Decimal(0), strength)
        assumptions.append(note)
        result = {
            "strength_sessions": strength,
            "aerobic_sessions": days - strength,
            "mobility_minutes_per_session": mobility,
            "weekly_aerobic_moderate_minutes": aerobic_minutes,
            "week": week,
            "guideline_check": rows,
            "goals": list(inp.goals),
            "safety": [CLINICIAN_PHYSIO_LINE]
            if inp.medical_limitations or inp.conditions
            else [
                ("Stop and seek medical advice if you feel chest pain, faintness or unusual breathlessness.")
            ],
        }
        checks.append(fixed_check(SHIP, "computed", "weekly plan built"))
    assumptions += assumption_notes(inputs)
    return inputs, assumptions, result, checks, rules_used


def plan_exercise(inputs: ExerciseInputs, *, refs: RulePack, as_of: date) -> dict[str, Any]:
    """Weekly exercise structure + per-criterion check against WHO 2020 (adults)."""
    ivs, assumptions, result, checks, rules_used = _plan_parts(inputs, refs, as_of)
    return finalize(
        "exercise_plan",
        inputs=ivs,
        assumptions=assumptions,
        result=result,
        checks=checks,
        rules_used=rules_used,
    )


def plan_exercise_from_profile(
    profile: Profile,
    *,
    refs: RulePack,
    as_of: date,
    experience: str,
    days_per_week: int | None,
    minutes_per_session: int | None,
    equipment: tuple[str, ...],
    goals: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Plan using consented facts: ``age`` (identity), ``medical_limitations`` and
    ``conditions`` (health). Missing facts become questions and HOLD."""
    reqs = [
        Requirement("age", "identity", "What is your age?"),
        Requirement(
            "medical_limitations", "health", "Do you have any medical limitation affecting exercise (yes/no)?"
        ),
        Requirement("conditions", "health", "Which health conditions do you have (empty if none)?"),
    ]
    outcome = profile_gate(profile, reqs, allow_assumptions=True)

    def value(key: str) -> Any:
        try:
            fact = profile.get(key)
        except ConsentRequired:
            return None
        return None if fact is None or fact.provenance == UNKNOWN else fact

    lim, cond = value("medical_limitations"), value("conditions")
    inp = ExerciseInputs(
        experience=experience,
        days_per_week=days_per_week,
        minutes_per_session=minutes_per_session,
        equipment=equipment,
        medical_limitations=lim.value if lim is not None and isinstance(lim.value, bool) else None,
        conditions=tuple(cond.value) if cond is not None and isinstance(cond.value, list) else None,
        age=value("age"),
        goals=goals,
    )
    ivs, assumptions, result, checks, rules_used = _plan_parts(inp, refs, as_of)
    checks += gate_reason_checks(
        {**r, "check": f"profile:{r['check']}"} for r in outcome.gate.reasons if r["disposition"] != SHIP
    )
    return finalize(
        "exercise_plan",
        inputs=ivs,
        assumptions=assumptions,
        result=result,
        checks=checks,
        rules_used=rules_used,
        extra={"questions": outcome.questions},
    )


def check_activity(
    *, moderate_minutes: Raw, vigorous_minutes: Raw, strength_days: int, refs: RulePack, as_of: date
) -> dict[str, Any]:
    """Check an actual week of activity against the WHO 2020 adult thresholds."""
    mod, vig = resolve("moderate_minutes", moderate_minutes), resolve("vigorous_minutes", vigorous_minutes)
    inputs = [mod, vig]
    checks = input_checks(inputs)
    if isinstance(strength_days, bool) or not isinstance(strength_days, int) or not (0 <= strength_days <= 7):
        checks.append(fixed_check(BLOCK, "strength_days", "strength_days must be 0..7"))
    ids = list(_GUIDELINE_IDS[:3])
    checks += rule_checks(refs, ids, as_of)
    assumptions: list[str] = []
    result: dict[str, Any] | None = None
    have_rules = all(i in refs.ids() for i in ids)  # missing rule: HOLD from rule_checks
    if (
        have_rules
        and not any(c.fn()[0] != SHIP for c in checks if not c.name.startswith("rule:"))
        and (mod.value is not None and vig.value is not None)
    ):
        rows, note = _guideline_rows(refs, mod.value, vig.value, strength_days)
        assumptions.append(note)
        result = {"guideline_check": rows}
    assumptions += assumption_notes(inputs)
    return finalize(
        "activity_check",
        inputs=inputs,
        assumptions=assumptions,
        result=result,
        checks=checks,
        rules_used=rule_citations(refs, ids, as_of),
    )


def progression(
    *,
    current_weekly_minutes: Raw,
    target_weekly_minutes: Raw,
    weeks: int,
    max_increase_percent: Raw = MAX_PROGRESSION_PERCENT,
) -> dict[str, Any]:
    """Weekly minutes rising by at most ``max_increase_percent`` (<= 10) per week, floored
    to whole minutes and capped at the target. A heuristic, not a cited guideline."""
    cur = resolve("current_weekly_minutes", current_weekly_minutes)
    tgt = resolve("target_weekly_minutes", target_weekly_minutes)
    pct = resolve("max_increase_percent", max_increase_percent)
    inputs = [cur, tgt, pct]
    checks = input_checks(inputs)
    if isinstance(weeks, bool) or not isinstance(weeks, int) or not 1 <= weeks <= 52:
        checks.append(fixed_check(BLOCK, "weeks", "weeks must be 1..52"))
    if pct.value is not None and not 0 < pct.value <= MAX_PROGRESSION_PERCENT:
        checks.append(
            fixed_check(
                BLOCK,
                "max_increase_percent",
                f"the heuristic caps weekly increases at {MAX_PROGRESSION_PERCENT}%",
            )
        )
    if cur.value is not None and cur.value == 0:
        checks.append(
            fixed_check(
                HOLD,
                "current_weekly_minutes",
                "a percentage rule cannot start from zero; choose a small starting amount yourself",
            )
        )
    ok_inputs = not any(c.fn()[0] != SHIP for c in checks)
    if ok_inputs and cur.value and tgt.value is not None and pct.value is not None:
        first = (cur.value * (1 + pct.value / 100)).to_integral_value(ROUND_FLOOR)
        if cur.value >= tgt.value:
            checks.append(
                fixed_check(
                    HOLD,
                    "target",
                    "already at or above the target; no progression is needed - confirm the target",
                )
            )
        elif first <= cur.value.to_integral_value(ROUND_FLOOR):
            # `first == cur` missed non-integer currents: 9.99 * 1.05 floors to 9, a
            # permanent stall BELOW the current level that used to SHIP.
            checks.append(
                fixed_check(
                    HOLD,
                    "progress",
                    f"a {pct.value}% weekly increase of "
                    f"{cur.value} minutes rounds down to no change; choose a "
                    f"minimum weekly step (a fixed number of minutes) "
                    f"yourself",
                )
            )
    result: dict[str, Any] | None = None
    if (
        not any(c.fn()[0] != SHIP for c in checks)
        and cur.value
        and tgt.value is not None
        and pct.value is not None
    ):
        plan: list[Decimal] = []
        prev = cur.value
        stalled: int | None = None
        for week in range(1, weeks + 1):
            new = min(tgt.value, (prev * (1 + pct.value / 100)).to_integral_value(ROUND_FLOOR))
            if stalled is None and new < tgt.value and new <= prev:
                # Rounding down can freeze the plan (10 * 1.05 -> 10) - checked every
                # week, not only the first, so a stalled plan never SHIPs.
                stalled = week
            plan.append(new)
            prev = new
        reached = next((i + 1 for i, m in enumerate(plan) if m >= tgt.value), None)
        result = {
            "label": "heuristic, not a cited guideline",
            "weekly_minutes": plan,
            "reaches_target_in_week": reached,
        }
        if stalled is not None:
            checks.append(
                fixed_check(
                    HOLD,
                    "progress",
                    f"the {pct.value}% weekly increase rounds down to no change in week {stalled}; "
                    f"choose a minimum weekly step (a fixed number of minutes) yourself",
                )
            )
        else:
            checks.append(fixed_check(SHIP, "computed", "progression built"))
    return finalize(
        "exercise_progression",
        inputs=inputs,
        assumptions=[
            (
                f"Progression is a conservative heuristic (at most "
                f"{pct.value}% more weekly minutes than the week before, "
                f"rounded down); it is not a cited guideline."
            ),
            *assumption_notes(inputs),
        ],
        result=result,
        checks=checks,
    )
