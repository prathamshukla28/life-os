"""LM06 health/nutrition: USDA subset + importer, cited reference targets, analysis, safety.

Nutrient numbers are NEVER typed here: bundled-data tests read values from the bundle
and check arithmetic; importer tests use clearly synthetic fixture CSVs.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.health import (
    AMINO_ACIDS,
    BUNDLE_PATH,
    ENERGY_REVIEW_FLOOR_KCAL,
    INTERACTIONS_PATH,
    MECHANISM_NOTES,
    NUTRITION_REFS_PATH,
    PROTEIN_REVIEW_CEILING_G_PER_KG,
    Digestibility,
    FoodTable,
    GiInput,
    MealItem,
    amino_acid_profile,
    analyze_meal,
    check_targets,
    glycemic_load,
    health_safety,
    health_safety_from_profile,
    load_food_table,
    load_interactions,
    load_nutrition_refs,
    lookup_food,
    protein_target,
    protein_target_from_profile,
)
from life_os.life_mentor.health.usda import (
    REQUIRED_HEADERS,
    SR_LEGACY_SELECTIONS,
    UsdaImportError,
    build_subset,
    write_bundle,
)
from life_os.life_mentor.profile import Profile
from life_os.life_mentor.security import PrivateStore, find_forbidden

D = Decimal
AS_OF = date(2026, 10, 11)
NOW = "2026-10-11T00:00:00Z"
ZIP_SHA = "b80817294b8850530aaedf2e515c02593b1824f763a0ff356e5c2081643e6fd0"
ZIP_URL = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip"


@pytest.fixture(scope="module")
def table() -> FoodTable:
    return load_food_table()


@pytest.fixture(scope="module")
def refs():
    return load_nutrition_refs()


@pytest.fixture(scope="module")
def interactions():
    return load_interactions()


def _clean(rep: dict) -> str:
    assert find_forbidden(rep) == [], find_forbidden(rep)
    assert set(rep) >= {"calculator", "inputs", "rules_used", "assumptions", "result", "gate"}
    blob = json.dumps(rep, default=str).casefold()
    assert "guarantee" not in blob
    return rep["gate"]["disposition"]


def _nut(table: FoodTable, key: str, nid: str) -> Decimal:
    return table.get(key).nutrients[nid]


# --- bundled USDA subset ------------------------------------------------------


def test_bundle_provenance(table: FoodTable) -> None:
    ds = table.dataset
    assert ds["release"] == "2018-04"
    assert ds["name"] == "USDA FoodData Central SR Legacy"
    assert ds["download_url"] == ZIP_URL
    assert ds["sha256"] == ZIP_SHA
    assert "CC0" in ds["license"]
    assert ds["basis"] == "per 100 g edible portion"
    assert 25 <= len(table.foods) <= 40
    assert not table.empty


def test_bundle_every_value_carries_provenance(table: FoodTable) -> None:
    raw = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    assert set(raw["foods"]) == set(SR_LEGACY_SELECTIONS)
    for key, food in raw["foods"].items():
        assert food["fdc_id"] == SR_LEGACY_SELECTIONS[key]
        assert food["data_type"] == "sr_legacy_food"
        assert food["description"]
        for nid, amount in food["nutrients"].items():
            assert nid in raw["nutrients"], nid
            assert isinstance(amount, str)  # Decimal-safe; never a JSON float
            Decimal(amount)
    for meta in raw["nutrients"].values():
        assert meta["name"] and meta["unit"]
    for key in (
        "rice_white_raw",
        "lentils_raw",
        "chickpeas_raw",
        "milk_whole",
        "egg_whole_raw",
        "spinach_raw",
        "wheat_flour_whole_grain",
        "groundnut_oil",
        "sugar_granulated",
    ):
        assert key in raw["foods"], key


def test_bundle_has_no_restricted_tables() -> None:
    data_dir = BUNDLE_PATH.parent
    names = " ".join(p.name.casefold() for p in data_dir.iterdir())
    for bad in ("ifct", "indb", "glycemic", "gi_"):
        assert bad not in names
    raw = BUNDLE_PATH.read_text(encoding="utf-8").casefold()
    assert "glycemic" not in raw


def test_lookup_food(table: FoodTable) -> None:
    rep = lookup_food(table, "lentils_raw")
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["fdc_id"] == SR_LEGACY_SELECTIONS["lentils_raw"]
    assert r["per_100g"]["1003"]["amount"] == _nut(table, "lentils_raw", "1003")
    assert r["per_100g"]["1003"]["unit"] == "G"
    assert rep["data_source"]["sha256"] == ZIP_SHA


def test_lookup_unknown_food_holds(table: FoodTable) -> None:
    rep = lookup_food(table, "dragonfruit")
    assert _clean(rep) == HOLD and rep["result"] is None


def test_empty_bundle_holds(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    write_bundle({"dataset": {"name": "x", "release": "none"}, "nutrients": {}, "foods": {}}, path)
    empty = load_food_table(path)
    assert empty.empty
    assert _clean(lookup_food(empty, "rice_white_raw")) == HOLD
    rep = analyze_meal([MealItem("rice_white_raw", D(100))], table=empty)
    assert _clean(rep) == HOLD


# --- importer (synthetic fixture CSVs) ------------------------------------------


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(header)
        w.writerows(rows)


def _fixture(tmp_path: Path, *, drop_header: str | None = None) -> Path:
    d = tmp_path / "csv"
    d.mkdir()
    food_h = list(REQUIRED_HEADERS["food.csv"])
    if drop_header in food_h:
        food_h.remove(drop_header)
    _write(
        d / "food.csv",
        food_h,
        [
            ["900001", "sr_legacy_food", "Synthetic grain, raw", "20", "2019-04-01"][: len(food_h)],
            ["900002", "sr_legacy_food", "Synthetic bean, raw", "16", "2019-04-01"][: len(food_h)],
        ],
    )
    _write(
        d / "nutrient.csv",
        list(REQUIRED_HEADERS["nutrient.csv"]),
        [
            ["1003", "Protein", "G", "203", "600"],
            ["1008", "Energy", "KCAL", "208", "300"],
            ["1214", "Lysine", "G", "505", "16600"],
        ],
    )
    _write(
        d / "food_nutrient.csv",
        list(REQUIRED_HEADERS["food_nutrient.csv"]),
        [
            ["1", "900001", "1003", "10", "1", "1", "", "", "", "", ""],
            ["2", "900001", "1008", "350", "1", "1", "", "", "", "", ""],
            ["3", "900001", "1214", "0.25", "1", "1", "", "", "", "", ""],
            ["4", "900002", "1003", "20.5", "1", "1", "", "", "", "", ""],
            ["5", "900002", "9999", "1", "1", "1", "", "", "", "", ""],
        ],
    )
    _write(
        d / "food_portion.csv",
        list(REQUIRED_HEADERS["food_portion.csv"]),
        [
            ["1", "900001", "1", "1", "1000", "", "cup", "185", "", "", ""],
        ],
    )
    _write(d / "measure_unit.csv", ["id", "name"], [["1000", "cup"]])
    return d


def _build(d: Path, **kw: Any) -> dict:
    return build_subset(
        d,
        kw.pop("selections", {"grain": "900001", "bean": "900002"}),
        release="TEST",
        download_url="https://example.org/test.zip",
        sha256="0" * 64,
        retrieved_on="2026-10-11",
        dataset_name="synthetic",
        **kw,
    )


def test_importer_builds_subset(tmp_path: Path) -> None:
    bundle = _build(_fixture(tmp_path), nutrient_ids=("1003", "1008", "1214"))
    assert bundle["dataset"]["release"] == "TEST"
    assert bundle["foods"]["grain"]["nutrients"] == {"1003": "10", "1008": "350", "1214": "0.25"}
    assert bundle["foods"]["bean"]["nutrients"] == {"1003": "20.5"}  # 9999 not selected
    assert bundle["foods"]["grain"]["portions"] == [{"description": "1 cup", "gram_weight": "185"}]
    assert bundle["nutrients"]["1214"] == {"name": "Lysine", "unit": "G"}
    out = tmp_path / "b.json"
    write_bundle(bundle, out)
    t = load_food_table(out)
    assert t.get("grain").nutrients["1214"] == D("0.25")


def test_importer_rejects_bad_headers(tmp_path: Path) -> None:
    with pytest.raises(UsdaImportError):
        _build(_fixture(tmp_path, drop_header="description"))


def test_importer_rejects_unknown_fdc_id_and_missing_files(tmp_path: Path) -> None:
    d = _fixture(tmp_path)
    with pytest.raises(UsdaImportError):
        _build(d, selections={"ghost": "123456"})
    (d / "nutrient.csv").unlink()
    with pytest.raises(UsdaImportError):
        _build(d)


def test_importer_rejects_non_numeric_amount(tmp_path: Path) -> None:
    d = _fixture(tmp_path)
    with (d / "food_nutrient.csv").open("a", encoding="utf-8") as fh:
        fh.write('"6","900001","1003","abc","1","1","","","","",""\n')
    with pytest.raises(UsdaImportError):
        _build(d)


# --- meal analysis ----------------------------------------------------------------


def test_meal_sums_from_bundle(table: FoodTable) -> None:
    items = [
        MealItem("rice_white_cooked", D(200)),
        MealItem("lentils_cooked", D(150)),
        MealItem("spinach_raw", D(50)),
    ]
    rep = analyze_meal(items, table=table)
    assert _clean(rep) == SHIP
    prot = rep["result"]["totals"]["1003"]
    expected = sum((_nut(table, i.food, "1003") * i.grams / 100 for i in items), D(0))
    assert prot["amount"] == expected.quantize(D("0.01"))
    assert prot["unit"] == "G" and prot["foods_reporting"] == 3 and prot["foods_total"] == 3
    assert set(prot["fdc_ids"]) == {SR_LEGACY_SELECTIONS[i.food] for i in items}
    assert rep["result"]["total_grams"] == D(400)
    assert rep["result"]["items"][0]["fdc_id"] == SR_LEGACY_SELECTIONS["rice_white_cooked"]


def test_meal_partial_nutrient_marked_lower_bound(table: FoodTable) -> None:
    rep = analyze_meal([MealItem("sugar_granulated", D(10)), MealItem("egg_whole_raw", D(50))], table=table)
    lys = rep["result"]["totals"]["1214"]
    assert lys["foods_reporting"] < lys["foods_total"]
    assert lys["complete"] is False
    assert any("lower bound" in a for a in rep["assumptions"])


@pytest.mark.parametrize(
    ("item", "disp"),
    [
        (MealItem("rice_white_raw", None), HOLD),
        (MealItem("rice_white_raw", D(-5)), BLOCK),
        (MealItem("unobtainium", D(10)), HOLD),
    ],
)
def test_meal_gates(table: FoodTable, item: MealItem, disp: str) -> None:
    assert _clean(analyze_meal([item], table=table)) == disp


def test_meal_rejects_float_and_empty(table: FoodTable) -> None:
    with pytest.raises(TypeError):
        analyze_meal([MealItem("rice_white_raw", 100.0)], table=table)  # type: ignore[arg-type]
    assert _clean(analyze_meal([], table=table)) == HOLD


# --- reference targets ----------------------------------------------------------------


def test_refs_pack_cited(refs) -> None:
    raw = json.loads(NUTRITION_REFS_PATH.read_text(encoding="utf-8"))
    by = {v["id"]: v for v in raw["values"]}
    assert by["protein_ear_adult"]["source_url"] == "https://www.nin.res.in/rdabook/brief_note.pdf"
    assert by["iaa_scoring_pattern_older_child_adolescent_adult"]["source_url"] == (
        "https://www.fao.org/4/i3124e/i3124e.pdf"
    )
    for v in raw["values"]:
        assert v["verified_by_human"] is False and v["retrieved_on"] == "2026-10-11"
        assert v["source_tier"] in ("primary", "secondary")
        assert v["source_sha256"]
    assert refs.get("protein_ear_adult").value == D("0.66")
    assert refs.get("protein_rda_adult").value == D("0.83")
    pattern = refs.get("iaa_scoring_pattern_older_child_adolescent_adult").value
    assert set(pattern) == {"his", "ile", "leu", "lys", "saa", "aaa", "thr", "trp", "val"}


def test_protein_target_from_weight(refs) -> None:
    rep = protein_target(body_weight_kg=D(65), refs=refs, as_of=AS_OF)
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["ear_g_per_day"] == D("42.90")  # 0.66 x 65 (ICMR-NIN Table 2a: 42.9)
    assert r["rda_g_per_day"] == D("53.95")  # 0.83 x 65 (Table 2a rounds to 54.0)
    ids = {c["id"] for c in rep["rules_used"]}
    assert {"protein_ear_adult", "protein_rda_adult"} <= ids
    assert all(c["unverified"] for c in rep["rules_used"])


@pytest.mark.parametrize(("weight", "disp"), [(None, HOLD), (D(-1), BLOCK), (D(0), BLOCK)])
def test_protein_target_gates(refs, weight: Any, disp: str) -> None:
    assert _clean(protein_target(body_weight_kg=weight, refs=refs, as_of=AS_OF)) == disp


def test_protein_target_stale_refs_hold(refs) -> None:
    rep = protein_target(body_weight_kg=D(65), refs=refs, as_of=date(2028, 1, 1))
    assert _clean(rep) == HOLD


def test_planned_protein_above_ceiling_holds_without_clinician(refs) -> None:
    assert D("2.2") == PROTEIN_REVIEW_CEILING_G_PER_KG
    high = protein_target(body_weight_kg=D(70), refs=refs, as_of=AS_OF, planned_g_per_kg=D("2.5"))
    assert _clean(high) == HOLD
    ok = protein_target(
        body_weight_kg=D(70),
        refs=refs,
        as_of=AS_OF,
        planned_g_per_kg=D("2.5"),
        clinician_note="sports physician advised, 2026-09",
    )
    assert _clean(ok) == SHIP


def _profile(tmp_path: Path, *cats: str) -> Profile:
    prof = Profile(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    for c in cats:
        prof.consent.grant(c, purpose="nutrition")
    return prof


def test_protein_target_from_profile_needs_consent_and_weight(tmp_path: Path, refs) -> None:
    prof = _profile(tmp_path)
    rep = protein_target_from_profile(prof, refs, as_of=AS_OF)
    assert _clean(rep) == HOLD
    assert rep["questions"][0]["reason"] == "no_consent"
    prof.consent.grant("health", purpose="nutrition")
    rep = protein_target_from_profile(prof, refs, as_of=AS_OF)
    assert _clean(rep) == HOLD and rep["questions"][0]["key"] == "body_weight_kg"
    prof.set_fact("body_weight_kg", D(65), "health", "USER_ESTIMATE", "home scale")
    rep = protein_target_from_profile(prof, refs, as_of=AS_OF)
    assert _clean(rep) == SHIP
    assert rep["inputs"]["body_weight_kg"]["provenance"] == "USER_ESTIMATE"


@pytest.mark.parametrize(
    ("kw", "disp"),
    [
        ({"energy_kcal": D(1100)}, HOLD),
        ({"energy_kcal": D(1800)}, SHIP),
        ({"energy_kcal": D(1100), "clinician_note": "supervised plan"}, SHIP),
        ({"protein_g_per_kg": D("2.3")}, HOLD),
        ({"protein_g_per_kg": D("1.0")}, SHIP),
        ({"energy_kcal": D(-1)}, BLOCK),
        ({}, HOLD),
    ],
)
def test_extreme_targets(kw: dict, disp: str) -> None:
    assert D(1200) == ENERGY_REVIEW_FLOOR_KCAL
    assert _clean(check_targets(**kw)) == disp


# --- amino acids --------------------------------------------------------------------------


def test_amino_acid_profile_egg_matches_formula(table: FoodTable, refs) -> None:
    rep = amino_acid_profile([MealItem("egg_whole_raw", D(100))], table=table, refs=refs, as_of=AS_OF)
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["label"] == "uncorrected (no digestibility correction)"
    pattern = refs.get("iaa_scoring_pattern_older_child_adolescent_adult").value
    egg = table.get("egg_whole_raw").nutrients
    lys_mg_per_g = egg["1214"] * 1000 / egg["1003"]
    assert r["ratios"]["lys"] == (lys_mg_per_g / pattern["lys"]).quantize(D("0.01"))
    assert r["limiting_amino_acid"] in pattern
    # Review fix F11: the percent is rounded once from the unrounded limiting ratio.
    lim = r["limiting_amino_acid"]
    lim_raw = sum((egg[n] for n in AMINO_ACIDS[lim]), D(0)) * 1000 / egg["1003"] / pattern[lim]
    assert r["uncorrected_percent"] == (lim_raw * 100).quantize(D("0.1"))
    assert r["digestibility"] == {"value": None, "source_ref": None, "evidence_class": None}
    assert "digestibility_adjusted_percent" not in r
    assert not any("score" in k.casefold() for k in r)


def test_amino_acid_profile_rice_is_lysine_limited(table: FoodTable, refs) -> None:
    rep = amino_acid_profile([MealItem("rice_white_cooked", D(200))], table=table, refs=refs, as_of=AS_OF)
    assert rep["result"]["limiting_amino_acid"] == "lys"


def test_amino_acid_digestibility_cited(table: FoodTable, refs) -> None:
    dig = Digestibility(D("0.90"), "example study ref (user supplied)", "STRONG_EVIDENCE")
    rep = amino_acid_profile(
        [MealItem("egg_whole_raw", D(100))], table=table, refs=refs, as_of=AS_OF, digestibility=dig
    )
    r = rep["result"]
    assert r["digestibility"]["value"] == D("0.90")
    assert r["digestibility_adjusted_percent"] == (r["uncorrected_percent"] * D("0.90")).quantize(D("0.1"))
    bad = amino_acid_profile(
        [MealItem("egg_whole_raw", D(100))],
        table=table,
        refs=refs,
        as_of=AS_OF,
        digestibility=Digestibility(D("0.9"), "", "EMPIRICAL"),
    )
    assert _clean(bad) == HOLD
    worse = amino_acid_profile(
        [MealItem("egg_whole_raw", D(100))],
        table=table,
        refs=refs,
        as_of=AS_OF,
        digestibility=Digestibility(D("1.5"), "x", "EMPIRICAL"),
    )
    assert _clean(worse) == BLOCK


def test_amino_acid_missing_data_holds(table: FoodTable, refs) -> None:
    rep = amino_acid_profile([MealItem("sugar_granulated", D(50))], table=table, refs=refs, as_of=AS_OF)
    assert _clean(rep) == HOLD


# --- glycemic load -------------------------------------------------------------------------


def test_glycemic_load_only_with_cited_gi(table: FoodTable) -> None:
    items = [MealItem("rice_white_cooked", D(150)), MealItem("lentils_cooked", D(100))]
    none = glycemic_load(items, table=table, gi={})
    assert _clean(none) == HOLD
    gi = {"rice_white_cooked": GiInput(D(70), "user-cited table ref p.12", "published_table")}
    rep = glycemic_load(items, table=table, gi=gi)
    assert _clean(rep) == SHIP
    rice = table.get("rice_white_cooked").nutrients
    avail = (rice["1005"] - rice.get("1079", D(0))) * D(150) / 100
    assert rep["result"]["items"]["rice_white_cooked"]["glycemic_load"] == (D(70) * avail / 100).quantize(
        D("0.1")
    )
    assert rep["result"]["items"]["lentils_cooked"] == {
        "glycemic_load": None,
        "reason": "no cited GI supplied",
    }
    assert rep["inputs"]["gi:rice_white_cooked"]["basis"] == "user-cited table ref p.12"


@pytest.mark.parametrize(
    ("gi", "disp"),
    [
        (GiInput(D(70), "", "published_table"), HOLD),
        (GiInput(D(70), "ref", "folklore"), BLOCK),
        (GiInput(D(-1), "ref", "published_table"), BLOCK),
    ],
)
def test_glycemic_load_gates(table: FoodTable, gi: GiInput, disp: str) -> None:
    rep = glycemic_load([MealItem("rice_white_cooked", D(100))], table=table, gi={"rice_white_cooked": gi})
    assert _clean(rep) == disp


# --- safety ---------------------------------------------------------------------------------


def test_interactions_pack_cited(interactions) -> None:
    raw = json.loads(INTERACTIONS_PATH.read_text(encoding="utf-8"))
    ids = {v["id"] for v in raw["values"]}
    assert {
        "warfarin_vitamin_k",
        "maoi_tyramine",
        "grapefruit_statins",
        "ace_inhibitor_potassium",
        "kidney_disease_potassium",
    } <= ids
    for v in raw["values"]:
        assert v["source_url"].startswith(("https://medlineplus.gov/", "https://www.niddk.nih.gov/"))
        assert v["verified_by_human"] is False


def test_warfarin_flag_with_vitamin_k_food(table: FoodTable, interactions) -> None:
    rep = health_safety(
        medications=["Warfarin 5mg"],
        conditions=[],
        items=[MealItem("spinach_raw", D(100))],
        table=table,
        interactions=interactions,
        as_of=AS_OF,
    )
    assert _clean(rep) == HOLD
    [flag] = rep["result"]["flags"]
    assert flag["id"] == "warfarin_vitamin_k" and flag["relevant_to_meal"] is True
    assert flag["source_url"] == "https://medlineplus.gov/druginfo/meds/a682277.html"
    text = json.dumps(rep, default=str).casefold()
    assert "discuss with your clinician" in text
    for bad in ("reduce your dose", "increase your dose", "stop taking", "diagnos"):
        assert bad not in text.replace("no diagnosis", "")
    assert rep["result"]["constraints"]


@pytest.mark.parametrize(
    ("meds", "conds", "flag_id"),
    [
        (["phenelzine"], [], "maoi_tyramine"),
        (["simvastatin"], [], "grapefruit_statins"),
        (["lisinopril"], [], "ace_inhibitor_potassium"),
        ([], ["chronic kidney disease"], "kidney_disease_potassium"),
    ],
)
def test_other_flags(interactions, meds: list, conds: list, flag_id: str) -> None:
    rep = health_safety(medications=meds, conditions=conds, interactions=interactions, as_of=AS_OF)
    assert _clean(rep) == HOLD
    assert flag_id in [f["id"] for f in rep["result"]["flags"]]


def test_unknown_medication_still_holds(interactions) -> None:
    rep = health_safety(medications=["somedrugzol"], conditions=[], interactions=interactions, as_of=AS_OF)
    assert _clean(rep) == HOLD and rep["result"]["flags"] == []
    assert "pharmacist" in " ".join(rep["result"]["constraints"]).casefold()


def test_no_meds_no_conditions_ships_and_unknown_holds(interactions) -> None:
    assert (
        _clean(health_safety(medications=[], conditions=[], interactions=interactions, as_of=AS_OF)) == SHIP
    )
    assert (
        _clean(health_safety(medications=None, conditions=[], interactions=interactions, as_of=AS_OF)) == HOLD
    )


def test_safety_from_profile(tmp_path: Path, interactions) -> None:
    prof = _profile(tmp_path)
    assert _clean(health_safety_from_profile(prof, interactions, as_of=AS_OF)) == HOLD
    prof.consent.grant("health", purpose="safety")
    prof.set_fact("medications", ["warfarin"], "health", "VERIFIED", "prescription")
    prof.set_fact("conditions", [], "health", "VERIFIED", "doctor visit")
    rep = health_safety_from_profile(prof, interactions, as_of=AS_OF)
    assert _clean(rep) == HOLD
    assert [f["id"] for f in rep["result"]["flags"]] == ["warfarin_vitamin_k"]
    raw = (tmp_path / "home" / "profile" / "journal.jsonl").read_text(encoding="utf-8")
    assert "warfarin" not in raw.casefold()


# --- mechanism notes --------------------------------------------------------------------------


def test_mechanism_notes_cited_and_classed() -> None:
    assert MECHANISM_NOTES
    for note in MECHANISM_NOTES:
        assert note["evidence_class"] in ("VERIFIED_FACT", "STRONG_EVIDENCE", "EMPIRICAL", "HYPOTHESIS")
        assert note["basis"].startswith("https://")
        assert len(note["text"]) <= 240
        assert "cell" not in note["text"].casefold()
