"""LM04 non-tax calculators: cash flow, emergency fund, net worth, debt payoff, goal SIP."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.finance import (
    Debt,
    cash_flow,
    debt_payoff,
    emergency_fund,
    goal_sip,
    net_worth,
)
from life_os.life_mentor.profile import Fact
from life_os.life_mentor.research import load_rule_pack
from life_os.life_mentor.security import find_forbidden

D = Decimal
NOW = "2026-10-11T00:00:00Z"


def _disp(rep: dict) -> str:
    assert find_forbidden(rep) == [], find_forbidden(rep)
    assert set(rep) >= {"calculator", "inputs", "rules_used", "assumptions", "result", "gate"}
    return rep["gate"]["disposition"]


# --- cash flow -------------------------------------------------------------


def test_cash_flow_surplus_and_savings_rate() -> None:
    rep = cash_flow(
        take_home_monthly=D(100000),
        essential_expenses={"rent": D(25000), "groceries": D(10000)},
        discretionary_expenses={"dining": D(5000)},
    )
    assert _disp(rep) == SHIP
    r = rep["result"]
    assert r["total_essential"] == D(35000)
    assert r["total_discretionary"] == D(5000)
    assert r["surplus"] == D(60000)
    assert r["savings_rate_percent"] == D("60.00")
    assert r["essential_share_percent"] == D("35.00")
    assert rep["inputs"]["essential:rent"]["value"] == D(25000)


def test_cash_flow_deficit_holds() -> None:
    rep = cash_flow(take_home_monthly=D(30000), essential_expenses={"rent": D(40000)})
    assert _disp(rep) == HOLD
    assert rep["result"]["surplus"] == D(-10000)


def test_cash_flow_unknown_take_home_holds() -> None:
    fact = Fact("take_home_monthly", None, "finance", "UNKNOWN", "not sure", NOW)
    rep = cash_flow(take_home_monthly=fact, essential_expenses={"rent": D(1)})
    assert _disp(rep) == HOLD and rep["result"] is None


def test_cash_flow_negative_expense_blocks_and_float_rejected() -> None:
    assert _disp(cash_flow(take_home_monthly=D(1), essential_expenses={"rent": D(-1)})) == BLOCK
    with pytest.raises(TypeError):
        cash_flow(take_home_monthly=1000.0, essential_expenses={"rent": D(1)})


def test_cash_flow_no_essentials_holds() -> None:
    assert _disp(cash_flow(take_home_monthly=D(1000), essential_expenses={})) == HOLD


def test_cash_flow_assumption_fact_listed() -> None:
    fact = Fact("rent", D(20000), "finance", "ASSUMPTION", "typical city rent", NOW)
    rep = cash_flow(take_home_monthly=D(50000), essential_expenses={"rent": fact})
    assert _disp(rep) == SHIP
    assert any("essential:rent" in a and "ASSUMPTION" in a for a in rep["assumptions"])


def test_cash_flow_redacts_pii_in_labels() -> None:
    rep = cash_flow(take_home_monthly=D(50000), essential_expenses={"EMI acct 001234567890123": D(1000)})
    assert _disp(rep) == SHIP
    assert all("001234567890123" not in k for k in rep["inputs"])


# --- emergency fund --------------------------------------------------------


def test_emergency_fund_default_months_is_assumption() -> None:
    rep = emergency_fund(essential_monthly=D(30000), current_balance=D(50000), monthly_surplus=D(20000))
    assert _disp(rep) == SHIP
    r = rep["result"]
    assert r["months"] == 6
    assert r["target"] == D(180000)
    assert r["gap"] == D(130000)
    assert r["months_to_target"] == 7
    assert r["months_of_cover"] == D("1.7")
    assert rep["inputs"]["months"]["provenance"] == "ASSUMPTION"
    assert any("6 months" in a for a in rep["assumptions"])


def test_emergency_fund_already_met() -> None:
    rep = emergency_fund(essential_monthly=D(10000), current_balance=D(90000), monthly_surplus=D(0), months=6)
    assert _disp(rep) == SHIP
    assert rep["result"]["gap"] == D(0) and rep["result"]["months_to_target"] == 0


def test_emergency_fund_no_surplus_never_holds() -> None:
    rep = emergency_fund(essential_monthly=D(10000), current_balance=D(0), monthly_surplus=D(0), months=3)
    assert _disp(rep) == HOLD
    assert rep["result"]["months_to_target"] == "never"


@pytest.mark.parametrize("months", [0, -2])
def test_emergency_fund_bad_months_blocks(months: int) -> None:
    rep = emergency_fund(essential_monthly=D(1), current_balance=D(0), monthly_surplus=D(1), months=months)
    assert _disp(rep) == BLOCK


def test_emergency_fund_missing_input_holds() -> None:
    rep = emergency_fund(essential_monthly=None, current_balance=D(0), monthly_surplus=D(1))
    assert _disp(rep) == HOLD


# --- net worth -------------------------------------------------------------


def test_net_worth() -> None:
    rep = net_worth(assets={"savings": D(500000), "ppf": D(300000)}, liabilities={"car_loan": D(200000)})
    assert _disp(rep) == SHIP
    assert rep["result"]["total_assets"] == D(800000)
    assert rep["result"]["total_liabilities"] == D(200000)
    assert rep["result"]["net_worth"] == D(600000)


def test_net_worth_negative_is_reported_not_blocked() -> None:
    rep = net_worth(assets={"cash": D(1000)}, liabilities={"loan": D(5000)})
    assert _disp(rep) == SHIP and rep["result"]["net_worth"] == D(-4000)


def test_net_worth_empty_holds_and_negative_entry_blocks() -> None:
    assert _disp(net_worth(assets={}, liabilities={})) == HOLD
    assert _disp(net_worth(assets={"x": D(-1)}, liabilities={})) == BLOCK


def test_net_worth_large_amounts_pass_scanner() -> None:
    rep = net_worth(assets={"house": D(250000000)}, liabilities={})
    assert _disp(rep) == SHIP


# --- debt payoff -----------------------------------------------------------


def test_debt_hand_verified_two_months() -> None:
    rep = debt_payoff([Debt("card", D(1000), D(12), D(0))], monthly_budget=D(600))
    assert _disp(rep) == SHIP
    for strat in ("avalanche", "snowball"):
        r = rep["result"][strat]
        assert r["status"] == "paid_off"
        assert r["months"] == 2
        assert r["total_interest"] == D("14.10")
        assert r["total_paid"] == D("1014.10")
        assert r["schedule"][0]["interest"]["card"] == D("10.00")
        assert r["schedule"][1]["balances"]["card"] == D(0)


def test_debt_zero_rate() -> None:
    rep = debt_payoff([Debt("loan", D(1000), D(0), D(100))], monthly_budget=D(250))
    r = rep["result"]["avalanche"]
    assert (r["months"], r["total_interest"]) == (4, D(0))


def test_avalanche_vs_snowball_order() -> None:
    debts = [Debt("big_high", D(30000), D(24), D(500)), Debt("small_low", D(5000), D(12), D(300))]
    rep = debt_payoff(debts, monthly_budget=D(4000))
    assert _disp(rep) == SHIP
    av, sb = rep["result"]["avalanche"], rep["result"]["snowball"]
    assert av["order"] == ["big_high", "small_low"]
    assert sb["order"] == ["small_low", "big_high"]
    assert av["total_interest"] <= sb["total_interest"]
    assert rep["result"]["comparison"]["interest_saved_by_avalanche"] == (
        sb["total_interest"] - av["total_interest"]
    )


def test_minimums_exceed_budget_blocks() -> None:
    rep = debt_payoff([Debt("a", D(1000), D(10), D(300))], monthly_budget=D(200))
    assert _disp(rep) == BLOCK


def test_budget_not_covering_interest_never_holds() -> None:
    rep = debt_payoff([Debt("a", D(10000), D(24), D(100))], monthly_budget=D(200))
    assert _disp(rep) == HOLD
    assert rep["result"]["avalanche"]["status"] == "never"
    assert rep["result"]["avalanche"]["months"] is None


def test_slow_payoff_beyond_cap_is_never() -> None:
    rep = debt_payoff([Debt("a", D(100000), D(12), D(1001))], monthly_budget=D(1001), max_months=60)
    assert _disp(rep) == HOLD
    assert rep["result"]["snowball"]["status"] == "never"
    assert len(rep["result"]["snowball"]["schedule"]) == 60


def test_minimum_below_interest_is_warned() -> None:
    rep = debt_payoff(
        [Debt("a", D(10000), D(24), D(100)), Debt("b", D(100), D(0), D(10))], monthly_budget=D(5000)
    )
    assert _disp(rep) == SHIP
    assert any("a" in w and "interest" in w for w in rep["result"]["warnings"])


@pytest.mark.parametrize(
    "debts",
    [
        [Debt("a", D(-1), D(10), D(10))],
        [Debt("a", D(1), D(-10), D(10))],
        [Debt("a", D(1), D(10), D(-10))],
        [Debt("a", D(1), D(10), D(1)), Debt("a", D(2), D(1), D(1))],
        [Debt("", D(1), D(10), D(1))],
    ],
)
def test_invalid_debts_block(debts: list) -> None:
    assert _disp(debt_payoff(debts, monthly_budget=D(1000))) == BLOCK


def test_no_debts_or_budget_holds() -> None:
    assert _disp(debt_payoff([], monthly_budget=D(1000))) == HOLD
    assert _disp(debt_payoff([Debt("a", D(1), D(1), D(1))], monthly_budget=None)) == HOLD


def test_debt_float_rejected() -> None:
    with pytest.raises(TypeError):
        debt_payoff([Debt("a", 100.0, D(1), D(1))], monthly_budget=D(10))


def test_debt_sweep_avalanche_never_costs_more_interest() -> None:
    for budget in range(1500, 6001, 500):
        debts = [
            Debt("a", D(20000), D(18), D(400)),
            Debt("b", D(8000), D(30), D(300)),
            Debt("c", D(3000), D(9), D(200)),
        ]
        rep = debt_payoff(debts, monthly_budget=D(budget))
        av, sb = rep["result"]["avalanche"], rep["result"]["snowball"]
        assert av["status"] == sb["status"] == "paid_off"
        assert av["total_interest"] <= sb["total_interest"]
        assert av["total_paid"] == D(31000) + av["total_interest"]


# --- goal SIP --------------------------------------------------------------


RETURNS = {"conservative": D(6), "base": D(10), "optimistic": D(12)}


def test_sip_zero_return_is_simple_division() -> None:
    rep = goal_sip(
        target_corpus=D(120000),
        months=12,
        annual_return_percent={"conservative": D(0), "base": D(0), "optimistic": D(0)},
        current_savings=D(0),
    )
    assert _disp(rep) == SHIP
    for s in ("conservative", "base", "optimistic"):
        assert rep["result"]["scenarios"][s]["monthly_contribution"] == D(10000)


def test_sip_hand_verified_12_percent() -> None:
    rep = goal_sip(
        target_corpus=D(120000),
        months=12,
        annual_return_percent={"conservative": D(12), "base": D(12), "optimistic": D(12)},
        current_savings=D(0),
    )
    sc = rep["result"]["scenarios"]["base"]
    assert sc["monthly_contribution"] == D(9369)
    assert sc["projected_corpus"] >= D(120000)


def test_sip_scenarios_ordered_and_labelled_assumptions() -> None:
    rep = goal_sip(
        target_corpus=D(1000000), months=60, annual_return_percent=RETURNS, current_savings=D(100000)
    )
    assert _disp(rep) == SHIP
    sc = rep["result"]["scenarios"]
    assert (
        sc["conservative"]["monthly_contribution"]
        > sc["base"]["monthly_contribution"]
        > sc["optimistic"]["monthly_contribution"]
    )
    for s in RETURNS:
        assert rep["inputs"][f"annual_return_percent:{s}"]["provenance"] == "ASSUMPTION"
    text = " ".join(rep["assumptions"]).casefold()
    assert "assumption" in text and "not promises" in text
    assert "guarantee" not in text


def test_sip_already_on_track_needs_zero() -> None:
    rep = goal_sip(
        target_corpus=D(100000), months=12, annual_return_percent=RETURNS, current_savings=D(200000)
    )
    assert rep["result"]["scenarios"]["base"]["monthly_contribution"] == D(0)


def test_sip_missing_scenario_holds_and_misordered_blocks() -> None:
    assert (
        _disp(
            goal_sip(
                target_corpus=D(1), months=12, annual_return_percent={"base": D(10)}, current_savings=D(0)
            )
        )
        == HOLD
    )
    assert (
        _disp(
            goal_sip(
                target_corpus=D(1),
                months=12,
                annual_return_percent={"conservative": D(12), "base": D(10), "optimistic": D(8)},
                current_savings=D(0),
            )
        )
        == BLOCK
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"target_corpus": D(0), "months": 12},
        {"target_corpus": D(-5), "months": 12},
        {"target_corpus": D(100), "months": 0},
        {
            "target_corpus": D(100),
            "months": 12,
            "annual_return_percent": {"conservative": D(-100), "base": D(1), "optimistic": D(2)},
        },
    ],
)
def test_sip_invalid_blocks(kw: dict) -> None:
    kw.setdefault("annual_return_percent", RETURNS)
    assert _disp(goal_sip(current_savings=D(0), **kw)) == BLOCK


def test_sip_missing_savings_is_assumption() -> None:
    rep = goal_sip(target_corpus=D(120000), months=12, annual_return_percent=RETURNS)
    assert _disp(rep) == SHIP
    assert any("current_savings" in a for a in rep["assumptions"])


def _rate_pack(retrieved: str) -> object:
    return load_rule_pack(
        {
            "pack_id": "rates",
            "title": "synthetic",
            "values": [
                {
                    "id": "ppf_rate",
                    "value": 7.1,
                    "unit": "percent p.a.",
                    "effective_from": "2026-10-01",
                    "effective_to": "2026-12-31",
                    "source_url": "https://example.gov.in/ppf",
                    "source_tier": "primary",
                    "retrieved_on": retrieved,
                    "verified_by_human": False,
                    "refresh_cadence_days": 90,
                }
            ],
        }
    )


def test_sip_rule_supplied_return_cited_and_gated() -> None:
    pack = _rate_pack("2026-10-01")
    rv = pack.get("ppf_rate")  # type: ignore[attr-defined]
    rets = {"conservative": rv, "base": D(9), "optimistic": D(11)}
    rep = goal_sip(
        target_corpus=D(500000),
        months=36,
        annual_return_percent=rets,
        current_savings=D(0),
        as_of=date(2026, 10, 11),
    )
    assert _disp(rep) == SHIP
    [cite] = rep["rules_used"]
    assert cite["id"] == "ppf_rate" and cite["unverified"] is True
    assert rep["result"]["scenarios"]["conservative"]["annual_return_percent"] == D("7.1")
    no_date = goal_sip(target_corpus=D(500000), months=36, annual_return_percent=rets, current_savings=D(0))
    assert _disp(no_date) == HOLD
    late = goal_sip(
        target_corpus=D(500000),
        months=36,
        annual_return_percent=rets,
        current_savings=D(0),
        as_of=date(2027, 2, 1),
    )
    assert _disp(late) == HOLD


def test_sip_float_rejected() -> None:
    with pytest.raises(TypeError):
        goal_sip(
            target_corpus=D(1),
            months=12,
            annual_return_percent={"conservative": 6.0, "base": D(1), "optimistic": D(2)},
        )
