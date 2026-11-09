"""Life Mentor health & nutrition (LM06): bundled USDA FoodData Central subset (CC0),
cited reference targets, meal analysis, uncorrected amino-acid ratios, glycemic load from
user-cited GI only, and clinician-first medication/condition safety flags. No diagnosis."""

from __future__ import annotations

from .foods import BUNDLE_PATH, Food, FoodTable, load_food_table
from .notes import MECHANISM_NOTES
from .nutrition import (
    AMINO_ACIDS,
    ENERGY_REVIEW_FLOOR_KCAL,
    GI_QUALITIES,
    NUTRITION_REFS_PATH,
    PATTERNS,
    PROTEIN_REVIEW_CEILING_G_PER_KG,
    Digestibility,
    GiInput,
    MealItem,
    amino_acid_profile,
    analyze_meal,
    check_targets,
    glycemic_load,
    load_nutrition_refs,
    lookup_food,
    protein_target,
    protein_target_from_profile,
)
from .safety import (
    CLINICIAN_LINE,
    INTERACTIONS_PATH,
    health_safety,
    health_safety_from_profile,
    load_interactions,
)

__all__ = [
    "AMINO_ACIDS",
    "BUNDLE_PATH",
    "CLINICIAN_LINE",
    "ENERGY_REVIEW_FLOOR_KCAL",
    "GI_QUALITIES",
    "INTERACTIONS_PATH",
    "MECHANISM_NOTES",
    "NUTRITION_REFS_PATH",
    "PATTERNS",
    "PROTEIN_REVIEW_CEILING_G_PER_KG",
    "Digestibility",
    "Food",
    "FoodTable",
    "GiInput",
    "MealItem",
    "amino_acid_profile",
    "analyze_meal",
    "check_targets",
    "glycemic_load",
    "health_safety",
    "health_safety_from_profile",
    "load_food_table",
    "load_interactions",
    "load_nutrition_refs",
    "lookup_food",
    "protein_target",
    "protein_target_from_profile",
]
