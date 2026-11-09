"""LM-G21 (review #8): tax_year is separate from as_of.

as_of decides rule STALENESS; the tax year (explicit, or derived from as_of as the
Indian financial year April-March containing it) decides which rules are IN EFFECT.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.finance import TAX_PACK_PATH, income_tax, load_tax_pack, take_home
from life_os.life_mentor.finance.tax import financial_year_for, financial_year_start
from life_os.life_mentor.research import load_rule_pack, rule_status, rules_gate

D = Decimal


def _long_cadence_pack():
    raw = json.loads(TAX_PACK_PATH.read_text(encoding="utf-8"))
    for v in raw["values"]:
        v["refresh_cadence_days"] = 400
    return load_rule_pack(raw)


@pytest.mark.parametrize(
    ("day", "fy"),
    [
        (date(2026, 4, 1), "FY2026-27"),
        (date(2027, 3, 31), "FY2026-27"),
        (date(2027, 4, 1), "FY2027-28"),
        (date(2026, 3, 31), "FY2025-26"),
        (date(2099, 12, 31), "FY2099-00"),
    ],
)
def test_financial_year_for(day: date, fy: str) -> None:
    assert financial_year_for(day) == fy


def test_financial_year_start() -> None:
    assert financial_year_start("FY2026-27") == date(2026, 4, 1)
    for bad in ("2026", "FY2026-28", "FY26-27", "fy2026-27", ""):
        with pytest.raises(ValueError):
            financial_year_start(bad)


def test_tax_year_derived_from_as_of_and_recorded() -> None:
    rep = income_tax(load_tax_pack(), as_of=date(2026, 10, 11), regime="new", gross_salary=D(1300000))
    assert rep["gate"]["disposition"] == SHIP
    assert rep["tax_year"] == "FY2026-27" and rep["as_of"] == "2026-10-11"
    assert rep["result"]["total_tax"] == D(26000)


def test_filing_after_year_end_uses_explicit_tax_year() -> None:
    # Filing for FY2026-27 in May 2027: rules are in effect for that tax year;
    # with a long refresh cadence they are not stale either -> SHIP.
    rep = income_tax(
        _long_cadence_pack(),
        as_of=date(2027, 5, 1),
        tax_year="FY2026-27",
        regime="new",
        gross_salary=D(1300000),
    )
    assert rep["gate"]["disposition"] == SHIP, rep["gate"]
    assert rep["result"]["total_tax"] == D(26000)
    # Same day without tax_year derives FY2027-28 -> not in effect -> HOLD.
    rep = income_tax(_long_cadence_pack(), as_of=date(2027, 5, 1), regime="new", gross_salary=D(1300000))
    assert rep["gate"]["disposition"] == HOLD
    assert any("not_in_effect" in r["reason"] for r in rep["gate"]["reasons"])
    assert rep["result"] is None  # review fix F4: no FY2026-27 numbers for FY2027-28


def test_staleness_still_uses_as_of() -> None:
    rep = income_tax(
        load_tax_pack(), as_of=date(2027, 5, 1), tax_year="FY2026-27", regime="new", gross_salary=D(1300000)
    )
    reasons = " ".join(r["reason"] for r in rep["gate"]["reasons"])
    assert rep["gate"]["disposition"] == HOLD
    assert "stale" in reasons and "not_in_effect" not in reasons


def test_future_tax_year_not_in_effect() -> None:
    rep = income_tax(
        load_tax_pack(), as_of=date(2026, 10, 11), tax_year="FY2027-28", regime="new", gross_salary=D(1300000)
    )
    assert rep["gate"]["disposition"] == HOLD
    assert any("not_in_effect" in r["reason"] for r in rep["gate"]["reasons"])


def test_invalid_tax_year_blocks() -> None:
    rep = income_tax(
        load_tax_pack(), as_of=date(2026, 10, 11), tax_year="2026-27", regime="new", gross_salary=D(1300000)
    )
    assert rep["gate"]["disposition"] == BLOCK and rep["result"] is None


def test_take_home_passes_tax_year() -> None:
    rep = take_home(
        _long_cadence_pack(),
        as_of=date(2027, 5, 1),
        tax_year="FY2026-27",
        regime="new",
        gross_salary=D(1300000),
        payroll_deductions=D(0),
    )
    assert rep["gate"]["disposition"] == SHIP and rep["tax_year"] == "FY2026-27"


def test_rule_status_effective_on_separate_from_as_of() -> None:
    pack = _long_cadence_pack()
    [st] = rule_status(pack, as_of=date(2027, 5, 1), effective_on=date(2026, 4, 1), ids=["new_regime_slabs"])
    assert st.in_effect and not st.stale
    [st] = rule_status(pack, as_of=date(2027, 5, 1), ids=["new_regime_slabs"])
    assert not st.in_effect
    gate = rules_gate(pack, as_of=date(2027, 5, 1), effective_on=date(2026, 4, 1), ids=["new_regime_slabs"])
    assert gate.disposition == SHIP
