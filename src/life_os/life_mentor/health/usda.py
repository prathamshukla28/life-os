"""USDA FoodData Central CSV importer (LM06).

Parses a USER-SUPPLIED, already-extracted USDA FoodData Central CSV directory
(stdlib ``csv``; no network) and builds a small JSON subset of selected foods:

    {"dataset": {name, release, download_url, sha256, license, retrieved_on, basis,
                 importer},
     "nutrients": {nutrient_id: {"name", "unit"}},
     "foods": {key: {"fdc_id", "description", "data_type", "publication_date",
                     "nutrients": {nutrient_id: "<amount as string>"},
                     "portions": [{"description", "gram_weight"}]}}}

Amounts are kept as the CSV's own decimal strings (never parsed through float), on
USDA's per-100 g edible-portion basis. Headers are validated; an unknown fdc_id,
a missing file, or a non-numeric amount raises ``UsdaImportError`` (fail closed).

USDA FoodData Central data are public domain (CC0 1.0). Restricted tables (IFCT,
INDB, GI tables) must never be imported into the bundle (SPEC-GAP LM-G2).
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_NUTRIENT_IDS",
    "REQUIRED_HEADERS",
    "SR_LEGACY_SELECTIONS",
    "UsdaImportError",
    "build_subset",
    "write_bundle",
]

REQUIRED_HEADERS: dict[str, tuple[str, ...]] = {
    "food.csv": ("fdc_id", "data_type", "description", "food_category_id", "publication_date"),
    "nutrient.csv": ("id", "name", "unit_name", "nutrient_nbr", "rank"),
    "food_nutrient.csv": (
        "id",
        "fdc_id",
        "nutrient_id",
        "amount",
        "data_points",
        "derivation_id",
        "min",
        "max",
        "median",
        "footnote",
        "min_year_acquired",
    ),
    "food_portion.csv": (
        "id",
        "fdc_id",
        "seq_num",
        "amount",
        "measure_unit_id",
        "portion_description",
        "modifier",
        "gram_weight",
        "data_points",
        "footnote",
        "min_year_acquired",
    ),
}
_OPTIONAL = ("food_portion.csv",)

# USDA nutrient ids (from the dataset's own nutrient.csv): energy, macros, fibre,
# selected minerals/vitamins, and the indispensable amino acids (+ cys, tyr for SAA/AAA).
DEFAULT_NUTRIENT_IDS: tuple[str, ...] = (
    "1008",
    "1051",
    "1003",
    "1004",
    "1258",
    "1253",
    "1005",
    "1079",
    "2000",
    "1087",
    "1089",
    "1090",
    "1092",
    "1093",
    "1095",
    "1106",
    "1114",
    "1162",
    "1175",
    "1178",
    "1185",
    "1190",
    "1210",
    "1211",
    "1212",
    "1213",
    "1214",
    "1215",
    "1216",
    "1217",
    "1218",
    "1219",
    "1221",
)

# Foods common in Indian diets, chosen by fdc_id from SR Legacy 2018-04 food.csv
# (descriptions are copied from the dataset by the importer, not typed here).
SR_LEGACY_SELECTIONS: dict[str, str] = {
    "rice_white_raw": "169756",
    "rice_white_cooked": "169757",
    "rice_brown_raw": "169703",
    "wheat_flour_whole_grain": "168893",
    "millet_raw": "169702",
    "oats": "173904",
    "lentils_raw": "172420",
    "lentils_cooked": "172421",
    "chickpeas_raw": "173756",
    "chickpeas_cooked": "173757",
    "kidney_beans_raw": "173744",
    "kidney_beans_cooked": "175194",
    "pigeon_peas_raw": "172436",
    "mung_beans_raw": "174256",
    "milk_whole": "172217",
    "yogurt_plain_whole_milk": "171284",
    "cheese_ricotta_whole_milk": "170851",
    "egg_whole_raw": "171287",
    "egg_whole_boiled": "173424",
    "chicken_breast_roasted": "171477",
    "fish_carp_raw": "171952",
    "spinach_raw": "168462",
    "potato_raw": "170026",
    "onion_raw": "170000",
    "tomato_raw": "170457",
    "cauliflower_raw": "169986",
    "okra_raw": "169260",
    "banana_raw": "173944",
    "apple_raw": "171688",
    "guava_raw": "173044",
    "mango_raw": "169910",
    "grapefruit_raw": "173033",
    "peanuts_raw": "172430",
    "almonds": "170567",
    "ghee_butter_oil": "173412",
    "butter_unsalted": "173430",
    "mustard_oil": "172337",
    "groundnut_oil": "171410",
    "sugar_granulated": "169655",
}


class UsdaImportError(ValueError):
    """The CSV directory is not a valid USDA FoodData Central export."""


def _rows(directory: Path, name: str) -> Iterable[dict[str, str]]:
    path = directory / name
    if not path.is_file():
        raise UsdaImportError(f"missing {name} in {directory}")
    with path.open(newline="", encoding="utf-8-sig") as fh:  # tolerate an Excel BOM
        reader = csv.DictReader(fh)
        header = tuple(reader.fieldnames or ())
        missing = [h for h in REQUIRED_HEADERS[name] if h not in header]
        if missing:
            raise UsdaImportError(f"{name}: missing columns {missing}")
        for row in reader:
            # DictReader fills missing trailing fields with None (and puts extras under
            # the None key); either is a malformed row, not an AttributeError later.
            if None in row or any(v is None for v in row.values()):
                raise UsdaImportError(f"{name} line {reader.line_num}: wrong number of fields")
            yield row


def _amount(raw: str, where: str) -> str:
    text = raw.strip()
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise UsdaImportError(f"{where}: amount {raw!r} is not a number") from exc
    if not value.is_finite() or value < 0:
        raise UsdaImportError(f"{where}: amount {raw!r} is not a finite non-negative number")
    return text


def _portion_text(row: dict[str, str]) -> str:
    amount = row["amount"].strip()
    desc = (row["portion_description"] or row["modifier"]).strip()
    return f"{amount} {desc}".strip() if amount else desc


def build_subset(
    csv_dir: str | os.PathLike[str],
    selections: Mapping[str, str],
    *,
    release: str,
    download_url: str,
    sha256: str,
    retrieved_on: str,
    dataset_name: str,
    nutrient_ids: Iterable[str] = DEFAULT_NUTRIENT_IDS,
) -> dict[str, Any]:
    """Build the subset bundle for *selections* ({key: fdc_id})."""
    directory = Path(csv_dir)
    if not selections:
        raise UsdaImportError("no foods selected")
    wanted_ids = {str(v) for v in selections.values()}
    nutrient_set = {str(n) for n in nutrient_ids}

    nutrients: dict[str, dict[str, str]] = {}
    for row in _rows(directory, "nutrient.csv"):
        if row["id"] in nutrient_set:
            nutrients[row["id"]] = {"name": row["name"].strip(), "unit": row["unit_name"].strip()}

    foods_meta: dict[str, dict[str, str]] = {}
    for row in _rows(directory, "food.csv"):
        if row["fdc_id"] in wanted_ids:
            foods_meta[row["fdc_id"]] = row
    unknown = sorted(wanted_ids - set(foods_meta))
    if unknown:
        raise UsdaImportError(f"fdc_id not found in food.csv: {unknown}")

    amounts: dict[str, dict[str, str]] = {fid: {} for fid in wanted_ids}
    for row in _rows(directory, "food_nutrient.csv"):
        fid, nid = row["fdc_id"], row["nutrient_id"]
        if fid in wanted_ids and nid in nutrient_set:
            amounts[fid][nid] = _amount(row["amount"], f"food_nutrient id {row['id']}")

    portions: dict[str, list[dict[str, str]]] = {fid: [] for fid in wanted_ids}
    if (directory / "food_portion.csv").is_file():
        for row in _rows(directory, "food_portion.csv"):
            if row["fdc_id"] in wanted_ids and row["gram_weight"].strip():
                portions[row["fdc_id"]].append(
                    {
                        "description": _portion_text(row),
                        "gram_weight": _amount(row["gram_weight"], f"food_portion id {row['id']}"),
                    }
                )

    foods: dict[str, Any] = {}
    for key, fid in sorted(selections.items()):
        meta = foods_meta[str(fid)]
        foods[key] = {
            "fdc_id": str(fid),
            "description": meta["description"].strip(),
            "data_type": meta["data_type"].strip(),
            "publication_date": meta["publication_date"].strip(),
            "nutrients": dict(sorted(amounts[str(fid)].items())),
            "portions": portions[str(fid)],
        }
    used = {nid for f in foods.values() for nid in f["nutrients"]}
    return {
        "dataset": {
            "name": dataset_name,
            "release": release,
            "download_url": download_url,
            "sha256": sha256,
            "license": "CC0 1.0 Universal (public domain) - USDA FoodData Central",
            "retrieved_on": retrieved_on,
            "basis": "per 100 g edible portion",
            "importer": "life_os.life_mentor.health.usda.build_subset",
        },
        "nutrients": {nid: nutrients[nid] for nid in sorted(used) if nid in nutrients},
        "foods": foods,
    }


def write_bundle(bundle: Mapping[str, Any], path: str | os.PathLike[str]) -> Path:
    """Write the bundle as sorted, indented JSON (amounts stay strings)."""
    out = Path(path)
    out.write_text(json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return out
