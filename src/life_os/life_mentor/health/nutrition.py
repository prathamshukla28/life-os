"""Nutrition analysis (LM06): food lookup, meal totals, protein targets, review thresholds,
uncorrected amino-acid ratios, and glycemic load from user-cited GI values.

Every report follows the finance report contract (``calculator, inputs, rules_used,
assumptions, result, gate``) and passes the LM02 scanner. Food values come only from
the bundled USDA subset (``data_source`` block); targets come only from the dated
``nutrition_reference`` rule pack. Missing data or targets -> HOLD with a question;
negative or contradictory inputs -> BLOCK. This is general information, no
diagnosis; anything clinical is for your clinician.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from life_os.core.evidence import EVIDENCE_CLASSES
from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.inputs import InputValue, Raw, assumption_notes, input_checks, resolve
from ..finance.report import finalize, gate_reason_checks, rule_checks, rule_citations
from ..profile import (
    USER_ESTIMATE,
    ConsentRequired,
    Fact,
    Profile,
    Requirement,
    profile_gate,
)
from ..research import RulePack, load_rule_pack
from ..security import redact
from .foods import FoodTable
from .notes import NOTE_BY_ID

__all__ = [
    "AMINO_ACIDS",
    "ENERGY_REVIEW_FLOOR_KCAL",
    "GI_QUALITIES",
    "NUTRITION_REFS_PATH",
    "PATTERNS",
    "PROTEIN_REVIEW_CEILING_G_PER_KG",
    "WEIGHT_KEY",
    "Digestibility",
    "GiInput",
    "MealItem",
    "amino_acid_profile",
    "analyze_meal",
    "check_targets",
    "glycemic_load",
    "load_nutrition_refs",
    "lookup_food",
    "protein_target",
    "protein_target_from_profile",
]

NUTRITION_REFS_PATH = Path(__file__).resolve().parent.parent / "rules" / "nutrition_reference.json"

# Human-specified REVIEW thresholds (Life Mentor policy from the LM06 brief), not
# nutrition targets: beyond them a plan HOLDs unless a clinician note is recorded.
ENERGY_REVIEW_FLOOR_KCAL = Decimal(1200)
PROTEIN_REVIEW_CEILING_G_PER_KG = Decimal("2.2")
MAX_PLAUSIBLE_WEIGHT_KG = Decimal(350)

WEIGHT_KEY = "body_weight_kg"
PATTERNS = ("infant_0_6m", "child_6m_3y", "older_child_adolescent_adult")
GI_QUALITIES = ("published_table", "primary_study", "user_estimate")

PROTEIN, ENERGY, CARB, FIBER, FAT = "1003", "1008", "1005", "1079", "1004"
_ITEM_NUTRIENTS = (ENERGY, PROTEIN, FAT, CARB, FIBER)
# Indispensable amino acids -> USDA nutrient ids (SAA = Met + Cys; AAA = Phe + Tyr).
AMINO_ACIDS: dict[str, tuple[str, ...]] = {
    "his": ("1221",),
    "ile": ("1212",),
    "leu": ("1213",),
    "lys": ("1214",),
    "saa": ("1215", "1216"),
    "aaa": ("1217", "1218"),
    "thr": ("1211",),
    "trp": ("1210",),
    "val": ("1219",),
}
_Q2 = Decimal("0.01")
_Q1 = Decimal("0.1")

FOOD_ASSUMPTIONS = (
    (
        "Food values are USDA FoodData Central SR Legacy 2018-04 averages per 100 g edible "
        "portion, scaled by the grams entered; varieties, recipes, cooking and brands differ."
    ),
    "Choose the food key matching how the food was weighed (raw vs cooked).",
    (
        "General information only, no diagnosis; for medical conditions, pregnancy or children "
        "discuss targets with your clinician."
    ),
)


def load_nutrition_refs(path: str | Path = NUTRITION_REFS_PATH) -> RulePack:
    return load_rule_pack(path)


def _fixed(disposition: str, name: str, reason: str) -> Check:
    def fn() -> tuple[str, str]:
        return disposition, reason

    return Check(name=name, fn=fn)


def _ok(iv: InputValue) -> bool:
    return iv.error is None and iv.value is not None and (iv.allow_negative or iv.value >= 0)


def _blocked(checks: list[Check]) -> bool:
    return any(c.fn()[0] != SHIP for c in checks)


def _source(table: FoodTable) -> dict[str, Any]:
    return dict(table.dataset)


@dataclass(frozen=True)
class MealItem:
    food: str
    grams: Raw


@dataclass(frozen=True)
class Digestibility:
    """A cited digestibility coefficient (0 < value <= 1)."""

    value: Raw
    source_ref: str
    evidence_class: str


@dataclass(frozen=True)
class GiInput:
    """A user-supplied, cited glycemic index (glucose = 100). Never bundled."""

    gi_value: Raw
    gi_source_ref: str
    gi_quality: str


@dataclass
class _Parts:
    inputs: list[InputValue] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    rules_used: list[dict[str, Any]] = field(default_factory=list)


# -- lookup -------------------------------------------------------------------------


def _table_checks(table: FoodTable) -> list[Check]:
    if table.empty:
        return [
            _fixed(
                HOLD,
                "food_data",
                "no bundled food data; import a USDA FoodData Central "
                "CSV export with life_os.life_mentor.health.usda.build_subset",
            )
        ]
    return [_fixed(SHIP, "food_data", f"{table.dataset.get('name')} {table.dataset.get('release')}")]


def lookup_food(table: FoodTable, key: str) -> dict[str, Any]:
    """Per-100 g values for one bundled food, with fdc_id and dataset provenance."""
    checks = _table_checks(table)
    result: dict[str, Any] | None = None
    if not table.empty:
        if key not in table.foods:
            checks.append(
                _fixed(
                    HOLD,
                    "food",
                    f"food {key!r} is not in the bundled subset; choose one of the listed keys or import it",
                )
            )
        else:
            food = table.get(key)
            result = {
                "food": key,
                "fdc_id": food.fdc_id,
                "description": food.description,
                "data_type": food.data_type,
                "per_100g": {
                    nid: {**table.nutrient_meta(nid), "amount": amt}
                    for nid, amt in sorted(food.nutrients.items())
                },
                "portions": list(food.portions),
            }
    return finalize(
        "food_lookup",
        inputs=[],
        assumptions=list(FOOD_ASSUMPTIONS),
        result=result,
        checks=checks,
        extra={"data_source": _source(table), "available_foods": sorted(table.foods)},
    )


# -- meals --------------------------------------------------------------------------


def _resolve_items(items: Sequence[MealItem], table: FoodTable, parts: _Parts) -> list[tuple[str, Decimal]]:
    """Validate items; returns [(food_key, grams)] only when every item is usable."""
    parts.checks += _table_checks(table)
    if not items:
        parts.checks.append(_fixed(HOLD, "items", "no foods listed; add foods with grams"))
    good: list[tuple[str, Decimal]] = []
    for i, item in enumerate(items):
        iv = resolve(f"item{i}:{redact(item.food)}:grams", item.grams)
        parts.inputs.append(iv)
        if not table.empty and item.food not in table.foods:
            parts.checks.append(
                _fixed(
                    HOLD,
                    f"item{i}",
                    f"food {redact(item.food)!r} is not in the bundled subset; pick a listed key",
                )
            )
        elif _ok(iv) and iv.value is not None:
            good.append((item.food, iv.value))
    parts.checks += input_checks(parts.inputs)
    return good if len(good) == len(items) and not _blocked(parts.checks) else []


def _meal_totals(table: FoodTable, good: list[tuple[str, Decimal]]) -> dict[str, Any]:
    totals: dict[str, Any] = {}
    for nid in sorted(table.nutrients):
        amount, fdc_ids = Decimal(0), []
        for key, grams in good:
            food = table.get(key)
            if nid in food.nutrients:
                amount += food.nutrients[nid] * grams / 100
                fdc_ids.append(food.fdc_id)
        totals[nid] = {
            **table.nutrient_meta(nid),
            "amount": amount.quantize(_Q2, ROUND_HALF_UP),
            "foods_reporting": len(fdc_ids),
            "foods_total": len(good),
            "complete": len(fdc_ids) == len(good),
            "fdc_ids": sorted(set(fdc_ids)),
        }
    return totals


def analyze_meal(items: Sequence[MealItem], *, table: FoodTable, name: str = "meal") -> dict[str, Any]:
    """Sum energy, macros, fibre, minerals, vitamins and amino acids for a meal or day."""
    parts = _Parts(assumptions=list(FOOD_ASSUMPTIONS))
    good = _resolve_items(items, table, parts)
    result: dict[str, Any] | None = None
    if good:
        totals = _meal_totals(table, good)
        result = {
            "name": redact(name),
            "total_grams": sum((g for _, g in good), Decimal(0)),
            "totals": totals,
            "items": [
                {
                    "food": key,
                    "fdc_id": table.get(key).fdc_id,
                    "description": table.get(key).description,
                    "grams": grams,
                    "nutrients": {
                        nid: {
                            **table.nutrient_meta(nid),
                            "amount": (table.get(key).nutrients[nid] * grams / 100).quantize(
                                _Q2, ROUND_HALF_UP
                            ),
                        }
                        for nid in _ITEM_NUTRIENTS
                        if nid in table.get(key).nutrients
                    },
                }
                for key, grams in good
            ],
        }
        if any(not t["complete"] for t in totals.values()):
            parts.assumptions.append(
                "Totals marked complete=false are a lower bound: at least one food has no USDA "
                "value for that nutrient."
            )
        parts.checks.append(_fixed(SHIP, "computed", f"{len(good)} foods summed"))
    parts.assumptions += assumption_notes(parts.inputs)
    return finalize(
        "meal_analysis",
        inputs=parts.inputs,
        assumptions=parts.assumptions,
        result=result,
        checks=parts.checks,
        extra={"data_source": _source(table)},
    )


# -- protein target & review thresholds ------------------------------------------------


_PROTEIN_IDS = ("protein_ear_adult", "protein_rda_adult", "protein_cereal_based_low_quality_diet")


def _protein_parts(
    weight: InputValue, refs: RulePack, as_of: date, planned: Raw, clinician_note: str | None
) -> tuple[_Parts, dict[str, Any] | None]:
    parts = _Parts(
        assumptions=[
            (
                "Reference values are for healthy adults (ICMR-NIN 2020); they do not cover pregnancy, "
                "lactation, children, illness or kidney disease - discuss those with your clinician."
            ),
            (
                "EAR is the average requirement; RDA is the safe intake covering nearly all healthy "
                "adults. Values assume good-quality protein."
            ),
        ]
    )
    parts.inputs.append(weight)
    parts.checks += input_checks([weight])
    if (
        weight.value is not None
        and weight.error is None
        and (weight.value == 0 or weight.value > MAX_PLAUSIBLE_WEIGHT_KG)
    ):
        parts.checks.append(_fixed(BLOCK, "body_weight_range", "body weight is implausible"))
    ids = [i for i in _PROTEIN_IDS if i in refs.ids()]
    for missing in sorted(set(_PROTEIN_IDS[:2]) - set(ids)):
        parts.checks.append(
            _fixed(
                HOLD,
                f"rule:{missing}",
                f"reference {missing!r} missing from pack {refs.pack_id!r}; which cited target should apply?",
            )
        )
    parts.checks += rule_checks(refs, ids, as_of)
    parts.rules_used = rule_citations(refs, ids, as_of)
    planned_iv: InputValue | None = None
    if planned is not None:
        planned_iv = resolve("planned_g_per_kg", planned)
        parts.inputs.append(planned_iv)
        parts.checks += input_checks([planned_iv])
        parts.checks += _review(
            planned_iv,
            above=PROTEIN_REVIEW_CEILING_G_PER_KG,
            clinician_note=clinician_note,
            parts=parts,
            what="planned protein (g/kg/day)",
        )
    parts.assumptions += assumption_notes(parts.inputs)
    # Compute whenever inputs are usable; HOLDs (stale rules, review thresholds) still
    # explain the numbers but cap the disposition. BLOCK or missing weight -> no result.
    if any(c.fn()[0] == BLOCK for c in parts.checks) or weight.value is None:
        return parts, None
    w = weight.value
    result: dict[str, Any] = {
        "body_weight_kg": w,
        "population": "healthy adults",
        "notes": [NOTE_BY_ID["protein_reference"]],
    }
    for rid, label in (
        ("protein_ear_adult", "ear"),
        ("protein_rda_adult", "rda"),
        ("protein_cereal_based_low_quality_diet", "cereal_based_diet"),
    ):
        if rid in ids:
            per_kg = refs.get(rid).value
            result[f"{label}_g_per_kg"] = per_kg
            result[f"{label}_g_per_day"] = (per_kg * w).quantize(_Q2, ROUND_HALF_UP)
    if planned_iv is not None and planned_iv.value is not None:
        result["planned_g_per_kg"] = planned_iv.value
        result["planned_g_per_day"] = (planned_iv.value * w).quantize(_Q2, ROUND_HALF_UP)
    return parts, result


def _review(
    iv: InputValue,
    *,
    clinician_note: str | None,
    parts: _Parts,
    what: str,
    above: Decimal | None = None,
    below: Decimal | None = None,
) -> list[Check]:
    if iv.value is None or iv.error is not None or iv.value < 0:
        return []
    extreme = (above is not None and iv.value > above) or (below is not None and iv.value < below)
    if not extreme:
        return [_fixed(SHIP, f"review:{iv.name}", f"{what} within review thresholds")]
    bound = f"above {above}" if above is not None and iv.value > above else f"below {below}"
    if clinician_note and clinician_note.strip():
        parts.assumptions.append(
            f"{what} is {bound} (a Life Mentor review threshold); a "
            f"clinician note is recorded: {redact(clinician_note)}"
        )
        return [_fixed(SHIP, f"review:{iv.name}", f"{what} {bound}; clinician note recorded")]
    return [
        _fixed(
            HOLD,
            f"review:{iv.name}",
            f"{what} is {bound}, beyond a Life Mentor review threshold; discuss with your "
            f"clinician and record their note before using it",
        )
    ]


def protein_target(
    *,
    body_weight_kg: Raw,
    refs: RulePack,
    as_of: date,
    planned_g_per_kg: Raw = None,
    clinician_note: str | None = None,
) -> dict[str, Any]:
    """Daily protein EAR/RDA from body weight using the cited ICMR-NIN 2020 values."""
    parts, result = _protein_parts(
        resolve("body_weight_kg", body_weight_kg), refs, as_of, planned_g_per_kg, clinician_note
    )
    return finalize(
        "protein_target",
        inputs=parts.inputs,
        assumptions=parts.assumptions,
        result=result,
        checks=parts.checks,
        rules_used=parts.rules_used,
    )


def _fetch(profile: Profile, key: str) -> Fact | None:
    try:
        return profile.get(key)
    except ConsentRequired:
        return None


def protein_target_from_profile(
    profile: Profile,
    refs: RulePack,
    *,
    as_of: date,
    planned_g_per_kg: Raw = None,
    clinician_note: str | None = None,
) -> dict[str, Any]:
    """Protein target from the consented ``body_weight_kg`` health fact."""
    req = Requirement(WEIGHT_KEY, "health", "What is your body weight in kg, and how measured?")
    outcome = profile_gate(profile, [req], allow_assumptions=True)
    parts, result = _protein_parts(
        resolve("body_weight_kg", _fetch(profile, WEIGHT_KEY)), refs, as_of, planned_g_per_kg, clinician_note
    )
    checks = [
        *parts.checks,
        *gate_reason_checks(
            {**r, "check": f"profile:{r['check']}"} for r in outcome.gate.reasons if r["disposition"] != SHIP
        ),
    ]
    return finalize(
        "protein_target",
        inputs=parts.inputs,
        assumptions=parts.assumptions,
        result=result,
        checks=checks,
        rules_used=parts.rules_used,
        extra={"questions": outcome.questions},
    )


def check_targets(
    *, energy_kcal: Raw = None, protein_g_per_kg: Raw = None, clinician_note: str | None = None
) -> dict[str, Any]:
    """HOLD extreme planned targets (energy below 1200 kcal/day, protein above 2.2 g/kg/day)
    unless a clinician note is recorded. Thresholds are review triggers, not targets."""
    parts = _Parts(
        assumptions=[
            (
                f"Review thresholds ({ENERGY_REVIEW_FLOOR_KCAL} kcal/day minimum, "
                f"{PROTEIN_REVIEW_CEILING_G_PER_KG} g protein/kg/day maximum) are Life Mentor policy set "
                f"by its owner, not clinical targets (LM-G24)."
            ),
        ]
    )
    result: dict[str, Any] = {}
    if energy_kcal is None and protein_g_per_kg is None:
        parts.checks.append(_fixed(HOLD, "targets", "no planned target given to check"))
    for name, raw, kw in (
        ("energy_kcal", energy_kcal, {"below": ENERGY_REVIEW_FLOOR_KCAL}),
        ("protein_g_per_kg", protein_g_per_kg, {"above": PROTEIN_REVIEW_CEILING_G_PER_KG}),
    ):
        if raw is None:
            continue
        iv = resolve(name, raw)
        parts.inputs.append(iv)
        parts.checks += input_checks([iv])
        parts.checks += _review(
            iv, clinician_note=clinician_note, parts=parts, what=name.replace("_", " "), **kw
        )
        result[name] = iv.value
    parts.assumptions += assumption_notes(parts.inputs)
    disp_ok = not any(c.fn()[0] == BLOCK for c in parts.checks)
    return finalize(
        "target_check",
        inputs=parts.inputs,
        assumptions=parts.assumptions,
        result=result if (result and disp_ok) else None,
        checks=parts.checks,
    )


# -- amino acids -----------------------------------------------------------------------------


def amino_acid_profile(
    items: Sequence[MealItem],
    *,
    table: FoodTable,
    refs: RulePack,
    as_of: date,
    pattern: str = "older_child_adolescent_adult",
    digestibility: Digestibility | None = None,
) -> dict[str, Any]:
    """Uncorrected amino-acid ratios of a meal's protein vs the FAO (2013) scoring pattern.

    Labelled "uncorrected (no digestibility correction)". A cited single digestibility
    coefficient may be applied as a rough adjustment; that is NOT a DIAAS, which needs
    per-amino-acid true ileal digestibility.
    """
    parts = _Parts(
        assumptions=[
            *FOOD_ASSUMPTIONS,
            (
                "Ratios are uncorrected (no digestibility correction): mg of each indispensable amino "
                "acid per g of meal protein divided by the FAO (2013) reference pattern."
            ),
        ]
    )
    rid = f"iaa_scoring_pattern_{pattern}"
    if pattern not in PATTERNS:
        parts.checks.append(_fixed(BLOCK, "pattern", f"unknown pattern {pattern!r}; use one of {PATTERNS}"))
    elif rid not in refs.ids():
        parts.checks.append(_fixed(HOLD, "pattern", f"pattern {rid!r} missing from the pack"))
    else:
        parts.checks += rule_checks(refs, [rid], as_of)
        parts.rules_used = rule_citations(refs, [rid], as_of)
    dig_iv: InputValue | None = None
    if digestibility is not None:
        dig_iv = resolve("digestibility", digestibility.value)
        parts.inputs.append(
            InputValue(
                "digestibility",
                dig_iv.value,
                USER_ESTIMATE,
                redact(digestibility.source_ref or "no source given"),
            )
            if dig_iv.error is None
            else dig_iv
        )
        if dig_iv.value is not None and not (0 < dig_iv.value <= 1):
            parts.checks.append(_fixed(BLOCK, "digestibility", "coefficient must be in (0, 1]"))
        if not digestibility.source_ref.strip():
            parts.checks.append(
                _fixed(HOLD, "digestibility_source", "digestibility needs a citation (source_ref)")
            )
        if digestibility.evidence_class not in EVIDENCE_CLASSES:
            parts.checks.append(
                _fixed(BLOCK, "digestibility_class", f"evidence_class must be one of {EVIDENCE_CLASSES}")
            )
    good = _resolve_items(items, table, parts)
    result: dict[str, Any] | None = None
    if good and not _blocked(parts.checks):
        result = _amino_result(table, good, refs.get(rid).value, parts, digestibility, dig_iv)
    parts.assumptions += assumption_notes(parts.inputs)
    return finalize(
        "amino_acid_profile",
        inputs=parts.inputs,
        assumptions=parts.assumptions,
        result=result,
        checks=parts.checks,
        rules_used=parts.rules_used,
        extra={"data_source": _source(table)},
    )


def _amino_result(
    table: FoodTable,
    good: list[tuple[str, Decimal]],
    ref: dict[str, Decimal],
    parts: _Parts,
    digestibility: Digestibility | None,
    dig_iv: InputValue | None,
) -> dict[str, Any] | None:
    protein = Decimal(0)
    aa_g = {aa: Decimal(0) for aa in AMINO_ACIDS}
    for key, grams in good:
        food = table.get(key)
        p = food.nutrients.get(PROTEIN)
        if p is None or p == 0:
            continue
        lacking = [aa for aa, nids in AMINO_ACIDS.items() if any(n not in food.nutrients for n in nids)]
        if lacking:
            parts.checks.append(
                _fixed(
                    HOLD,
                    f"amino:{key}",
                    f"{key}: USDA reports no data for "
                    f"{', '.join(lacking)}; the meal profile would be "
                    f"incomplete",
                )
            )
            return None
        protein += p * grams / 100
        for aa, nids in AMINO_ACIDS.items():
            aa_g[aa] += sum((food.nutrients[n] for n in nids), Decimal(0)) * grams / 100
    if protein == 0:
        parts.checks.append(_fixed(HOLD, "protein", "these foods contain no protein to assess"))
        return None
    mg_per_g = {aa: aa_g[aa] * 1000 / protein for aa in AMINO_ACIDS}
    raw_ratios = {aa: mg_per_g[aa] / ref[aa] for aa in AMINO_ACIDS}
    ratios = {aa: r.quantize(_Q2) for aa, r in raw_ratios.items()}
    limiting = min(raw_ratios, key=lambda a: (raw_ratios[a], a))
    percent = (raw_ratios[limiting] * 100).quantize(_Q1)  # rounded once, from the raw ratio
    result: dict[str, Any] = {
        "label": "uncorrected (no digestibility correction)",
        "protein_g": protein.quantize(_Q2, ROUND_HALF_UP),
        "mg_per_g_protein": {aa: v.quantize(_Q1, ROUND_HALF_UP) for aa, v in mg_per_g.items()},
        "reference_mg_per_g_protein": dict(ref),
        "ratios": ratios,
        "limiting_amino_acid": limiting,
        "uncorrected_percent": percent,
        "digestibility": {"value": None, "source_ref": None, "evidence_class": None},
        "notes": [NOTE_BY_ID["diaas_definition"]],
    }
    if digestibility is not None and dig_iv is not None and dig_iv.value is not None:
        result["digestibility"] = {
            "value": dig_iv.value,
            "source_ref": redact(digestibility.source_ref),
            "evidence_class": digestibility.evidence_class,
        }
        result["digestibility_adjusted_percent"] = (percent * dig_iv.value).quantize(_Q1)
        result["digestibility_adjusted_label"] = (
            "single-coefficient adjustment of the uncorrected value; not a DIAAS"
        )
    parts.checks.append(_fixed(SHIP, "computed", f"limiting amino acid {limiting}"))
    return result


# -- glycemic load ---------------------------------------------------------------------------------


def glycemic_load(
    items: Sequence[MealItem], *, table: FoodTable, gi: Mapping[str, GiInput]
) -> dict[str, Any]:
    """GL = GI x available carbohydrate (g) / 100, ONLY for foods with a user-cited GI."""
    parts = _Parts(
        assumptions=[
            *FOOD_ASSUMPTIONS,
            (
                "Glycemic load uses only GI values you supplied with a citation; Life Mentor bundles "
                "no GI table. Available carbohydrate = carbohydrate by difference minus total fibre."
            ),
        ]
    )
    if not gi:
        parts.checks.append(
            _fixed(
                HOLD,
                "gi",
                "no cited GI supplied; give gi_value, gi_source_ref and gi_quality for the foods to assess",
            )
        )
    gi_vals: dict[str, Decimal] = {}
    for food_key, g in gi.items():
        iv = resolve(f"gi:{redact(food_key)}", g.gi_value)
        parts.checks += input_checks([iv])
        if iv.error is None and iv.value is not None:
            parts.inputs.append(
                InputValue(iv.name, iv.value, USER_ESTIMATE, g.gi_source_ref or "no source given")
            )
            if iv.value >= 0:
                gi_vals[food_key] = iv.value
        if not g.gi_source_ref.strip():
            parts.checks.append(
                _fixed(HOLD, f"gi_source:{food_key}", f"GI for {food_key} has no citation (gi_source_ref)")
            )
        if g.gi_quality not in GI_QUALITIES:
            parts.checks.append(
                _fixed(BLOCK, f"gi_quality:{food_key}", f"gi_quality must be one of {GI_QUALITIES}")
            )
    good = _resolve_items(items, table, parts)
    result: dict[str, Any] | None = None
    if good and not _blocked(parts.checks):
        grams_by: dict[str, Decimal] = {}
        for key, grams in good:
            grams_by[key] = grams_by.get(key, Decimal(0)) + grams
        per_item: dict[str, Any] = {}
        total = Decimal(0)
        for key, grams in grams_by.items():
            if key not in gi_vals:
                per_item[key] = {"glycemic_load": None, "reason": "no cited GI supplied"}
                continue
            n = table.get(key).nutrients
            if FIBER not in n:
                parts.assumptions.append(f"{key}: USDA reports no fibre value; fibre taken as 0.")
            avail = (n.get(CARB, Decimal(0)) - n.get(FIBER, Decimal(0))) * grams / 100
            gl = (gi_vals[key] * avail / 100).quantize(_Q1)
            total += gl
            per_item[key] = {
                "glycemic_load": gl,
                "gi_value": gi_vals[key],
                "available_carbohydrate_g": avail.quantize(_Q2, ROUND_HALF_UP),
                "gi_quality": gi[key].gi_quality,
                "fdc_id": table.get(key).fdc_id,
            }
        result = {
            "items": per_item,
            "total_glycemic_load_cited_foods": total,
            "complete": all(v["glycemic_load"] is not None for v in per_item.values()),
        }
        parts.checks.append(_fixed(SHIP, "computed", "glycemic load for cited foods"))
    parts.assumptions += assumption_notes(parts.inputs)
    return finalize(
        "glycemic_load",
        inputs=parts.inputs,
        assumptions=parts.assumptions,
        result=result,
        checks=parts.checks,
        extra={"data_source": _source(table)},
    )
