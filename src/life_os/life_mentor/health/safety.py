"""Medication / condition safety flags (LM06).

A short, cited list (``rules/health_interactions.json``: NIH MedlinePlus drug pages and
NIDDK) of food-medicine and food-condition cautions. Any listed medicine or condition
makes the report HOLD for clinician review, whether or not a flag matches - Life Mentor
knows only this short list. Output always says to discuss with your clinician; it never
suggests a dose or medicine change and gives no diagnosis. User-entered medicine and
condition text is matched but never echoed in the report.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.report import finalize, gate_reason_checks, rule_checks, rule_citations
from ..profile import UNKNOWN, ConsentRequired, Profile, Requirement, profile_gate
from ..research import RulePack, load_rule_pack
from .foods import FoodTable
from .notes import NOTE_BY_ID
from .nutrition import MealItem

__all__ = [
    "CLINICIAN_LINE",
    "CONDITIONS_KEY",
    "INTERACTIONS_PATH",
    "MEDICATIONS_KEY",
    "health_safety",
    "health_safety_from_profile",
    "load_interactions",
]

INTERACTIONS_PATH = Path(__file__).resolve().parent.parent / "rules" / "health_interactions.json"
MEDICATIONS_KEY = "medications"
CONDITIONS_KEY = "conditions"
CLINICIAN_LINE = "Discuss with your clinician before changing your diet or your medicines."

SAFETY_ASSUMPTIONS = (
    ("Only a short cited list of interactions is checked; absence of a flag is not evidence of safety."),
    (
        "Information only, no diagnosis. Life Mentor never suggests starting, pausing or changing "
        "a medicine or its dose."
    ),
)


def load_interactions(path: str | Path = INTERACTIONS_PATH) -> RulePack:
    return load_rule_pack(path)


def _fixed(disposition: str, name: str, reason: str) -> Check:
    def fn() -> tuple[str, str]:
        return disposition, reason

    return Check(name=name, fn=fn)


def _clean_list(name: str, raw: Any, checks: list[Check]) -> list[str] | None:
    if raw is None:
        checks.append(
            _fixed(
                HOLD,
                name,
                f"{name}: not stated; do you take any medicines or "
                f"have any conditions? Say 'none' (an empty list) if so",
            )
        )
        return None
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) for x in raw):
        checks.append(_fixed(BLOCK, name, f"{name} must be a list of strings"))
        return None
    return [_norm(x) for x in raw if _norm(x)]


def _norm(text: str) -> str:
    """Casefold and collapse punctuation/whitespace: 'ACE-inhibitor' == 'ace  inhibitor'."""
    return " ".join(re.sub(r"[\W_]+", " ", text.casefold()).split())


def _term_in(term: str, texts: list[str]) -> bool:
    # Substring (not whole-word) on normalised text, so plurals such as "ACE inhibitors"
    # still match: a spurious caution is cheap, a missed one is not.
    t = _norm(term)
    return any(t in x for x in texts)


def _meal_relevance(value: dict[str, Any], items: Sequence[MealItem], table: FoodTable | None) -> bool:
    keys = {i.food for i in items}
    if keys & set(value.get("food_keys", [])):
        return True
    if table is None:
        return False
    for key in keys:
        food = table.foods.get(key)
        if food and any(food.nutrients.get(n, 0) > 0 for n in value.get("nutrient_ids", [])):
            return True
    return False


def _safety_parts(
    medications: Any,
    conditions: Any,
    items: Sequence[MealItem],
    table: FoodTable | None,
    interactions: RulePack,
    as_of: date,
) -> tuple[list[Check], dict[str, Any] | None, list[dict[str, Any]]]:
    checks: list[Check] = []
    meds = _clean_list("medications", medications, checks)
    conds = _clean_list("conditions", conditions, checks)
    checks += rule_checks(interactions, interactions.ids(), as_of)
    rules_used = rule_citations(interactions, interactions.ids(), as_of)
    if meds is None or conds is None:
        return checks, None, rules_used
    flags: list[dict[str, Any]] = []
    for rv in interactions.values:
        value = rv.value
        # Normalised matching (it used to compare raw casefolded text, so
        # "ACE-inhibitor" or a doubled space silently missed a cited caution).
        hit = next((t for t in value.get("medication_terms", []) if _term_in(t, meds)), None) or next(
            (t for t in value.get("condition_terms", []) if _term_in(t, conds)), None
        )
        if hit is None:
            continue
        flags.append(
            {
                "id": rv.id,
                "matched_term": hit,
                "message": value["message"],
                "source_url": rv.source_url,
                "source_tier": rv.source_tier,
                "relevant_to_meal": _meal_relevance(value, items, table),
                "action": CLINICIAN_LINE,
                "notes": [NOTE_BY_ID[rv.id]] if rv.id in NOTE_BY_ID else [],
            }
        )
    constraints = [CLINICIAN_LINE]
    if meds or conds:
        constraints += [
            (
                "Diet suggestions are limited to general information until a clinician has "
                "reviewed them for your medicines and conditions."
            ),
            (
                "Life Mentor checks only a short cited list; ask your pharmacist about other "
                "interactions with your medicines."
            ),
        ]
        checks.append(
            _fixed(
                HOLD,
                "clinician_review",
                f"{len(flags)} cited flag(s); medicines or conditions are listed, "
                f"so clinician review comes first",
            )
        )
    else:
        checks.append(_fixed(SHIP, "clinician_review", "no medicines or conditions listed"))
    result = {
        "flags": flags,
        "constraints": constraints,
        "medications_listed": len(meds),
        "conditions_listed": len(conds),
    }
    return checks, result, rules_used


def health_safety(
    *,
    medications: Sequence[str] | None,
    conditions: Sequence[str] | None,
    interactions: RulePack,
    as_of: date,
    items: Sequence[MealItem] = (),
    table: FoodTable | None = None,
) -> dict[str, Any]:
    """Flag cited food-medicine / food-condition cautions; always defer to a clinician."""
    checks, result, rules_used = _safety_parts(medications, conditions, items, table, interactions, as_of)
    return finalize(
        "health_safety",
        inputs=[],
        assumptions=list(SAFETY_ASSUMPTIONS),
        result=result,
        checks=checks,
        rules_used=rules_used,
    )


def health_safety_from_profile(
    profile: Profile,
    interactions: RulePack,
    *,
    as_of: date,
    items: Sequence[MealItem] = (),
    table: FoodTable | None = None,
) -> dict[str, Any]:
    """Safety flags from the consented ``medications`` / ``conditions`` health facts."""
    reqs = [
        Requirement(MEDICATIONS_KEY, "health", "Which medicines do you take? (empty if none)"),
        Requirement(CONDITIONS_KEY, "health", "Which health conditions do you have? (empty if none)"),
    ]
    outcome = profile_gate(profile, reqs, allow_assumptions=True)

    def value(key: str) -> Any:
        try:
            fact = profile.get(key)
        except ConsentRequired:
            return None
        return None if fact is None or fact.provenance == UNKNOWN else fact.value

    checks, result, rules_used = _safety_parts(
        value(MEDICATIONS_KEY), value(CONDITIONS_KEY), items, table, interactions, as_of
    )
    checks += gate_reason_checks(
        {**r, "check": f"profile:{r['check']}"} for r in outcome.gate.reasons if r["disposition"] != SHIP
    )
    return finalize(
        "health_safety",
        inputs=[],
        assumptions=list(SAFETY_ASSUMPTIONS),
        result=result,
        checks=checks,
        rules_used=rules_used,
        extra={"questions": outcome.questions},
    )
