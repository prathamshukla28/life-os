"""LM04 India income tax FY2026-27 (new + old regime) and take-home.

Worked examples are the plan's hand-verified figures (`.omo/plans/life-mentor.md`).
Hypothesis is not installed in this environment, so the properties are checked
with deterministic sweeps instead (no new dependency).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.finance import (
    TAX_PACK_PATH,
    compute_tax,
    income_tax,
    income_tax_from_profile,
    load_tax_pack,
    regime_rules,
    round_288,
    take_home,
)
from life_os.life_mentor.profile import Profile
from life_os.life_mentor.research import RulePackBlocked, load_rule_pack
from life_os.life_mentor.security import PrivateStore, find_forbidden

AS_OF = date(2026, 10, 11)
D = Decimal


@pytest.fixture(scope="module")
def pack():
    return load_tax_pack()


def _tax(pack, regime: str, **kw: Any) -> dict:
    return income_tax(pack, as_of=AS_OF, regime=regime, **kw)


# --- rule pack file --------------------------------------------------------


def test_rule_pack_file_loads_and_fields_present() -> None:
    assert TAX_PACK_PATH.name == "in_income_tax_fy2026_27.json"
    raw = json.loads(TAX_PACK_PATH.read_text(encoding="utf-8"))
    pack = load_rule_pack(TAX_PACK_PATH)
    assert len(pack.values) >= 14
    for v in raw["values"]:
        for key in (
            "id",
            "value",
            "unit",
            "effective_from",
            "effective_to",
            "tax_year",
            "act",
            "act_section_1961",
            "act_section_2025",
            "source_url",
            "source_doc_id",
            "source_tier",
            "retrieved_on",
            "verified_by_human",
            "refresh_cadence",
            "refresh_cadence_days",
            "next_expected_change",
        ):
            assert key in v, (v["id"], key)
        assert v["effective_from"] == "2026-04-01"
        assert v["effective_to"] == "2027-03-31"
        assert v["tax_year"] == "FY2026-27"
        assert v["retrieved_on"] == "2026-10-11"
        assert v["verified_by_human"] is False  # never claim human verification
        assert v["source_tier"] in ("primary", "secondary")
    by_id = {v["id"]: v for v in raw["values"]}
    for slab_id in (
        "new_regime_slabs",
        "old_regime_slabs",
        "new_regime_standard_deduction",
        "old_regime_standard_deduction",
    ):
        assert by_id[slab_id]["source_tier"] == "secondary"
    assert "egazette.gov.in" in by_id["health_education_cess"]["source_url"]
    assert "incometaxindia.gov.in" in by_id["new_regime_rebate_87a"]["source_url"]


def test_rule_pack_values_match_plan(pack) -> None:
    new = regime_rules(pack, "new")
    old = regime_rules(pack, "old")
    assert new.std_deduction == D(75000) and old.std_deduction == D(50000)
    assert (new.rebate_max, new.rebate_limit, new.rebate_marginal_relief) == (D(60000), D(1200000), True)
    assert (old.rebate_max, old.rebate_limit, old.rebate_marginal_relief) == (D(12500), D(500000), False)
    assert [r for _, r in new.surcharge_bands] == [D("0.10"), D("0.15"), D("0.25")]
    assert [r for _, r in old.surcharge_bands] == [D("0.10"), D("0.15"), D("0.25"), D("0.37")]
    assert new.cess_rate == D("0.04")
    assert {(e.age_min, e.limit) for e in old.age_exemptions} == {(60, D(300000)), (80, D(500000))}
    assert new.age_exemptions == ()


def test_no_float_in_rules(pack) -> None:
    def walk(o: Any) -> None:
        assert not isinstance(o, float)
        if isinstance(o, (list, tuple)):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for x in o.values():
                walk(x)

    for v in pack.values:
        walk(v.value)


def test_no_tax_numbers_hard_coded_in_python() -> None:
    src = (
        Path(__file__).resolve().parent.parent / "src" / "life_os" / "life_mentor" / "finance" / "tax.py"
    ).read_text(encoding="utf-8")
    for literal in (
        "75000",
        "75_000",
        "1200000",
        "12_00_000",
        "60000",
        "0.04",
        "0.37",
        "250000",
        "500000",
        "5000000",
        "12500",
    ):
        assert literal not in src, literal


# --- worked examples (exact) -----------------------------------------------


@pytest.mark.parametrize(
    ("regime", "kwargs", "expected"),
    [
        ("new", {"gross_salary": D(1275000)}, D(0)),
        ("new", {"gross_salary": D(1300000)}, D(26000)),
        ("new", {"gross_salary": D(2575000)}, D(343200)),
        ("new", {"taxable_income": D(6000000)}, D(1578720)),
        ("new", {"taxable_income": D(5010000)}, D(1133600)),
        ("old", {"gross_salary": D(1000000), "age": 35, "other_deductions": D(0)}, D(106600)),
        ("old", {"taxable_income": D(500000), "age": 35}, D(0)),
        ("old", {"taxable_income": D(500100), "age": 35}, D(13020)),
    ],
)
def test_worked_examples_exact(pack, regime: str, kwargs: dict, expected: Decimal) -> None:
    report = _tax(pack, regime, **kwargs)
    assert report["gate"]["disposition"] == SHIP, report["gate"]
    total = report["result"]["total_tax"]
    assert isinstance(total, Decimal)
    assert total == expected


def test_worked_example_breakdowns(pack) -> None:
    r = _tax(pack, "new", gross_salary=D(1300000))["result"]
    assert r["taxable_income"] == D(1225000)
    assert r["slab_tax"] == D(63750)
    assert r["rebate_marginal_relief"] == D(38750)
    assert r["tax_and_surcharge"] == D(25000)
    r = _tax(pack, "new", taxable_income=D(5010000))["result"]
    assert r["slab_tax"] == D(1083000)
    assert r["tax_and_surcharge"] == D(1090000)
    assert r["surcharge_marginal_relief"] == D(1191300) - D(1090000)
    r = _tax(pack, "new", taxable_income=D(6000000))["result"]
    assert (r["surcharge_rate"], r["surcharge"]) == (D("0.10"), D(138000))
    r = _tax(pack, "old", gross_salary=D(1000000), age=35, other_deductions=D(0))["result"]
    assert r["standard_deduction"] == D(50000) and r["taxable_income"] == D(950000)
    r = _tax(pack, "old", taxable_income=D(500100), age=35)["result"]
    assert r["rebate_87a"] == D(0) and r["cess"] == D("500.80")
    assert r["tax_before_rounding"] == D("13020.80")


@pytest.mark.parametrize(
    ("age", "taxable", "expected"),
    [
        (65, D(500000), D(0)),  # 3L exemption, 10,000 slab tax, rebate
        (65, D(1000000), D(114400)),  # 10,000 + 1,00,000 + 4% cess
        (85, D(700000), D(41600)),  # 5L exemption: 40,000 + cess
        (59, D(1000000), D(117000)),  # 12,500 + 1,00,000 + cess
    ],
)
def test_old_regime_age_exemptions(pack, age: int, taxable: Decimal, expected: Decimal) -> None:
    assert _tax(pack, "old", taxable_income=taxable, age=age)["result"]["total_tax"] == expected


def test_top_surcharge_bands(pack) -> None:
    # Hand-computed: old 37% band above 5 Cr; new regime capped at 25%.
    assert _tax(pack, "old", taxable_income=D(60000000), age=40)["result"]["total_tax"] == D(25379250)
    new = _tax(pack, "new", taxable_income=D(60000000))["result"]
    assert new["surcharge_rate"] == D("0.25")
    assert new["total_tax"] == D(22854000)


def test_new_regime_age_does_not_change_tax(pack) -> None:
    a = _tax(pack, "new", taxable_income=D(900000), age=30)["result"]["total_tax"]
    b = _tax(pack, "new", taxable_income=D(900000), age=82)["result"]["total_tax"]
    assert a == b == D(0)


# --- rounding --------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (D("13020.80"), D(13020)),
        (D("13024.99"), D(13020)),
        (D("13025"), D(13030)),
        (D("13025.10"), D(13030)),
        (D(0), D(0)),
        (D("4.99"), D(0)),
        (D(5), D(10)),
    ],
)
def test_round_288(amount: Decimal, expected: Decimal) -> None:
    assert round_288(amount, D(10)) == expected


def test_taxable_income_rounded_to_ten(pack) -> None:
    r = _tax(pack, "new", taxable_income=D("1500004.99"))["result"]
    assert r["taxable_income"] == D(1500000)


# --- report contract -------------------------------------------------------


def test_report_carries_inputs_rules_assumptions_gate(pack) -> None:
    rep = _tax(pack, "new", gross_salary=D(1300000))
    assert set(rep) >= {"calculator", "inputs", "rules_used", "assumptions", "result", "gate"}
    assert rep["inputs"]["gross_salary"]["value"] == D(1300000)
    assert rep["inputs"]["gross_salary"]["provenance"] == "USER_ESTIMATE"
    ids = {r["id"] for r in rep["rules_used"]}
    assert {
        "new_regime_slabs",
        "new_regime_standard_deduction",
        "new_regime_rebate_87a",
        "health_education_cess",
        "rounding_tax",
    } <= ids
    for r in rep["rules_used"]:
        assert r["source_url"].startswith("https://")
        assert r["retrieved_on"] == "2026-10-11"
        assert r["unverified"] is True and "unverified" in r["flags"]
        assert r["verified_by_human"] is False
    text = " ".join(rep["assumptions"]).casefold()
    for needle in ("salary income only", "capital-gains", "80c", "resident", "288b", "not human-verified"):
        assert needle in text, needle
    assert any("UNVERIFIED" in r["reason"] for r in rep["gate"]["reasons"])
    assert find_forbidden(rep) == []


def test_float_inputs_rejected(pack) -> None:
    with pytest.raises(TypeError):
        _tax(pack, "new", gross_salary=1300000.0)
    with pytest.raises(TypeError):
        _tax(pack, "old", taxable_income=D(1), age=30, other_deductions=1.5)


# --- gates -----------------------------------------------------------------


def test_missing_income_holds(pack) -> None:
    rep = _tax(pack, "new")
    assert rep["gate"]["disposition"] == HOLD and rep["result"] is None


def test_negative_salary_blocks(pack) -> None:
    rep = _tax(pack, "new", gross_salary=D(-1))
    assert rep["gate"]["disposition"] == BLOCK and rep["result"] is None


@pytest.mark.parametrize(
    ("regime", "kw"),
    [
        ("new", {"gross_salary": D(10), "taxable_income": D(10)}),
        ("new", {"gross_salary": D(1000000), "other_deductions": D(10)}),
        ("old", {"taxable_income": D(1000000), "age": 30, "other_deductions": D(1)}),
        ("old", {"gross_salary": D(100000), "age": 30, "other_deductions": D(60000)}),
        ("old", {"gross_salary": D(100000), "age": -1}),
        ("flat", {"gross_salary": D(100000)}),
    ],
)
def test_contradictions_block(pack, regime: str, kw: dict) -> None:
    assert _tax(pack, regime, **kw)["gate"]["disposition"] == BLOCK


def test_old_regime_needs_age(pack) -> None:
    rep = _tax(pack, "old", gross_salary=D(1000000), other_deductions=D(0))
    assert rep["gate"]["disposition"] == HOLD
    assert any("age" in r["reason"] for r in rep["gate"]["reasons"])


def test_old_regime_missing_deductions_is_assumption(pack) -> None:
    rep = _tax(pack, "old", gross_salary=D(1000000), age=35)
    assert rep["gate"]["disposition"] == SHIP
    assert rep["result"]["total_tax"] == D(106600)
    assert any("other_deductions" in a for a in rep["assumptions"])


def test_out_of_period_holds(pack) -> None:
    rep = income_tax(pack, as_of=date(2027, 5, 1), regime="new", gross_salary=D(1300000))
    assert rep["gate"]["disposition"] == HOLD
    assert any("not_in_effect" in r["reason"] for r in rep["gate"]["reasons"])


def test_stale_pack_holds() -> None:
    raw = json.loads(TAX_PACK_PATH.read_text(encoding="utf-8"))
    for v in raw["values"]:
        v["retrieved_on"] = "2026-04-01"
        v["refresh_cadence_days"] = 30
    stale = load_rule_pack(raw)
    rep = income_tax(stale, as_of=AS_OF, regime="new", gross_salary=D(1300000))
    assert rep["gate"]["disposition"] == HOLD
    assert any("stale" in r["reason"] for r in rep["gate"]["reasons"])
    assert rep["result"]["total_tax"] == D(26000)  # still explained, but not usable


def test_require_verified_holds(pack) -> None:
    rep = income_tax(pack, as_of=AS_OF, regime="new", gross_salary=D(1300000), require_verified=True)
    assert rep["gate"]["disposition"] == HOLD


def test_malformed_pack_blocks() -> None:
    raw = json.loads(TAX_PACK_PATH.read_text(encoding="utf-8"))
    raw["values"] = [v for v in raw["values"] if v["id"] != "health_education_cess"]
    rep = income_tax(load_rule_pack(raw), as_of=AS_OF, regime="new", gross_salary=D(1300000))
    assert rep["gate"]["disposition"] == BLOCK
    raw = json.loads(TAX_PACK_PATH.read_text(encoding="utf-8"))
    for v in raw["values"]:
        if v["id"] == "new_regime_slabs":
            v["value"] = [[0, 0], [800000, 0.10], [400000, 0.05]]  # not ascending
    with pytest.raises(RulePackBlocked):
        regime_rules(load_rule_pack(raw), "new")


# --- take-home -------------------------------------------------------------


def test_take_home_example(pack) -> None:
    rep = take_home(pack, as_of=AS_OF, regime="new", gross_salary=D(1300000), payroll_deductions=D(21600))
    assert rep["gate"]["disposition"] == SHIP
    r = rep["result"]
    assert r["income_tax"] == D(26000)
    assert r["take_home_annual"] == D(1252400)
    assert r["take_home_monthly"] == D("104366.67")
    text = " ".join(rep["assumptions"]).casefold()
    assert "professional tax" in text and "epf" in text
    assert find_forbidden(rep) == []


def test_take_home_missing_payroll_deductions_holds(pack) -> None:
    rep = take_home(pack, as_of=AS_OF, regime="new", gross_salary=D(1300000))
    assert rep["gate"]["disposition"] == HOLD


def test_take_home_negative_payroll_blocks(pack) -> None:
    rep = take_home(pack, as_of=AS_OF, regime="new", gross_salary=D(1300000), payroll_deductions=D(-5))
    assert rep["gate"]["disposition"] == BLOCK


# --- profile integration ---------------------------------------------------


def test_from_profile_missing_fact_holds_with_question(tmp_path: Path, pack) -> None:
    prof = Profile(PrivateStore(tmp_path / "home"), clock=lambda: "2026-10-11T00:00:00Z")
    prof.consent.grant("finance", purpose="tax")
    rep = income_tax_from_profile(prof, pack, as_of=AS_OF, regime="new")
    assert rep["gate"]["disposition"] == HOLD
    assert [q["key"] for q in rep["questions"]] == ["gross_salary_annual"]


def test_from_profile_unknown_holds_and_verified_ships(tmp_path: Path, pack) -> None:
    prof = Profile(PrivateStore(tmp_path / "home"), clock=lambda: "2026-10-11T00:00:00Z")
    prof.consent.grant("finance", purpose="tax")
    prof.consent.grant("identity", purpose="age for slabs")
    prof.set_fact("gross_salary_annual", None, "finance", "UNKNOWN", "not sure")
    assert income_tax_from_profile(prof, pack, as_of=AS_OF, regime="new")["gate"]["disposition"] == HOLD
    prof.correct("gross_salary_annual", D(1000000), "VERIFIED", "Form 16")
    prof.set_fact("age", 35, "identity", "VERIFIED", "PAN card")
    rep = income_tax_from_profile(prof, pack, as_of=AS_OF, regime="old")
    assert rep["gate"]["disposition"] == SHIP
    assert rep["result"]["total_tax"] == D(106600)
    assert rep["inputs"]["gross_salary"]["provenance"] == "VERIFIED"
    assert rep["questions"] == []


def test_from_profile_without_consent_holds(tmp_path: Path, pack) -> None:
    prof = Profile(PrivateStore(tmp_path / "home"))
    rep = income_tax_from_profile(prof, pack, as_of=AS_OF, regime="new")
    assert rep["gate"]["disposition"] == HOLD
    assert rep["questions"][0]["reason"] == "no_consent"


# --- property sweeps (deterministic; Hypothesis not installed) --------------


def _incomes() -> list[Decimal]:
    pts = set(range(0, 6_000_001, 2_500))
    for edge in (1_200_000, 5_000_000, 10_000_000, 20_000_000, 50_000_000):
        pts.update(range(edge - 30_000, edge + 150_001, 250))
    pts.update(range(6_000_000, 70_000_001, 250_000))
    return [D(p) for p in sorted(pts)]


@pytest.mark.parametrize("regime", ["new", "old"])
def test_tax_never_negative_and_monotonic(pack, regime: str) -> None:
    rules = regime_rules(pack, regime)
    prev = D(-1)
    for inc in _incomes():
        t = compute_tax(rules, inc, age=40).total_tax
        assert t >= 0
        assert t <= inc
        assert t >= prev, (regime, inc, t, prev)
        prev = t


@pytest.mark.parametrize("regime", ["new", "old"])
def test_marginal_relief_bounds_tax_increase(pack, regime: str) -> None:
    """Relief guarantee as the law states it: tax+surcharge (before cess) never
    rises by more than the income rise. Final tax adds 4% cess on top, plus up
    to one rounding step - see SPEC-GAP LM-G7."""
    rules = regime_rules(pack, regime)
    incs = _incomes()
    pre = [compute_tax(rules, i, age=40) for i in incs]
    for (i0, a), (i1, b) in zip(
        zip(incs, pre, strict=True), zip(incs[1:], pre[1:], strict=True), strict=False
    ):
        d_inc = b.taxable_income - a.taxable_income
        if not rules.rebate_marginal_relief and a.taxable_income <= rules.rebate_limit < b.taxable_income:
            continue  # old-regime rebate cliff: no marginal relief by statute (see below)
        assert b.tax_and_surcharge - a.tax_and_surcharge <= d_inc, (regime, i0, i1)
        slack = (1 + rules.cess_rate) * d_inc + rules.tax_rounding
        assert b.total_tax - a.total_tax <= slack, (regime, i0, i1)


def test_old_regime_rebate_cliff_has_no_marginal_relief(pack) -> None:
    """Old regime: Rs 100 above the rebate limit costs Rs 13,020 (plan worked example).
    Post-tax income falls across the cliff - statutory, pinned here (LM-G7)."""
    rules = regime_rules(pack, "old")
    at = compute_tax(rules, D(500000), age=40).total_tax
    above = compute_tax(rules, D(500100), age=40).total_tax
    assert (at, above) == (D(0), D(13020))
    assert D(500100) - above < D(500000) - at


def test_post_tax_strict_monotonicity_is_false_with_cess(pack) -> None:
    """The naive property 'post-tax income never falls' does NOT hold under the
    statute: 87A marginal relief caps tax at the excess over 12L, then 4% cess
    applies on top (LM-G7). Pin the counterexample so the gap stays visible."""
    rules = regime_rules(pack, "new")
    at = compute_tax(rules, D(1200000), age=40)
    above = compute_tax(rules, D(1210000), age=40)
    assert above.total_tax == D(10400)
    assert D(1210000) - above.total_tax < D(1200000) - at.total_tax


def test_take_home_sweep(pack) -> None:
    for gross in range(0, 8_000_001, 160_000):
        g = D(gross)
        for regime, extra in (("new", {}), ("old", {"age": 40, "other_deductions": D(0)})):
            rep = take_home(
                pack, as_of=AS_OF, regime=regime, gross_salary=g, payroll_deductions=D(0), **extra
            )
            r = rep["result"]
            assert rep["gate"]["disposition"] == SHIP
            assert 0 <= r["take_home_annual"] <= g
            assert r["income_tax"] >= 0
