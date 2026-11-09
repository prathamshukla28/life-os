"""Regression tests for the independent code + security review of life_mentor/.

Each test name carries the finding id (h1, h2, ..., c1 = code #1, ...). Every test
here failed before its fix.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.core.identity import Identity
from life_os.life_mentor.decision_engine import (
    BASELINE_NAME,
    Option,
    Situation,
    analyze_decision,
    benefits,
    load_rates_pack,
    record_confirmation,
    situation_from_profile,
)
from life_os.life_mentor.finance import (
    income_tax,
    income_tax_from_profile,
    load_tax_pack,
    take_home,
)
from life_os.life_mentor.finance.inputs import InputValue
from life_os.life_mentor.profile import Fact, Profile
from life_os.life_mentor.research import RulePackBlocked, load_rule_pack
from life_os.life_mentor.security import InsecureStore, PrivateStore, find_forbidden, redact

D = Decimal
AS_OF = date(2026, 10, 11)
NOW = "2026-10-11T00:00:00Z"
ME = Identity("tester", "Test User", "")
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def rates():
    return load_rates_pack()


def _sit(**over: Any) -> Situation:
    kw: dict[str, Any] = {
        "take_home_monthly": D(80000),
        "essential_monthly": D(55000),
        "emergency_fund_balance": D(300000),
        "emergency_fund_months": 6,
        "goals": ("goal_upskill",),
    }
    kw.update(over)
    return Situation(**kw)


def _course(**over: Any) -> Option:
    kw: dict[str, Any] = {
        "name": "data_course",
        "kind": "course",
        "upfront_cost": D(60000),
        "recurring_monthly_cost": D(0),
        "horizon_months": 12,
        "benefit_monthly": benefits(D(0), D(5000), D(10000), basis="estimate", evidence_class="HYPOTHESIS"),
        "downside_loss": D(60000),
        "goal_tags": ("goal_upskill",),
    }
    kw.update(over)
    return Option(**kw)


def _opt(rep: dict, name: str) -> dict:
    [o] = [o for o in rep["result"]["options"] if o["name"] == name]
    return o


def _row(opt: dict, step: int) -> dict:
    return opt["criteria"][step - 1]


def _profile(tmp_path: Path, *cats: str) -> Profile:
    prof = Profile(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    for c in cats:
        prof.consent.grant(c, purpose="test")
    return prof


# --- SECURITY ----------------------------------------------------------------


def test_h1_tail_truncation_detected(tmp_path: Path) -> None:
    j = PrivateStore(tmp_path / "home").journal("j.jsonl")
    for i in range(4):
        j.append({"event": "note", "n": i})
    anchor = j.path.with_name("j.jsonl.head")
    assert anchor.exists() and stat.S_IMODE(anchor.stat().st_mode) == 0o600
    assert json.loads(anchor.read_text())["count"] == 4
    lines = j.path.read_text(encoding="utf-8").splitlines()
    j.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    assert j.verify() == (False, 3)


def test_h1_missing_anchor_detected(tmp_path: Path) -> None:
    j = PrivateStore(tmp_path / "home").journal("j.jsonl")
    j.append({"event": "a"})
    j.append({"event": "b"})
    j.path.with_name("j.jsonl.head").unlink()
    assert j.verify() == (False, 0)


def test_h1_truncated_to_empty_and_forged_tail_detected(tmp_path: Path) -> None:
    j = PrivateStore(tmp_path / "home").journal("j.jsonl")
    j.append({"event": "a"})
    j.path.write_text("", encoding="utf-8")
    assert j.verify() == (False, 0)


@pytest.mark.parametrize(
    "text",
    [
        "2345.1234.5678",
        "2345\xa01234\xa05678",
        "2345/1234/5678",
        "4111. 1111. 1111. 1111",
        "９８７６５４３２１０",
    ],
)
def test_h2_wider_separators_masked(text: str) -> None:
    out = redact(f"id {text} end")
    assert not any(ch.isdigit() for ch in out), out


@pytest.mark.parametrize(
    "text",
    [
        "Spent Rs 12,75,000.50 on 2026-10-11; goal year 2030",
        "effective 2026-04-01 to 2027-03-31",
        "source https://egazette.gov.in/WriteReadData/2026/271439.pdf, tier primary",
        "source https://www.who.int/publications/i/item/9789240015128 (ISBN in path)",
        "café ﬁnance",
    ],
)
def test_h2_dates_amounts_urls_untouched(text: str) -> None:
    assert redact(text) == text


def test_h2_digits_in_url_query_still_masked() -> None:
    out = redact("see https://example.org/form?phone=9876543210&id=1")
    assert "9876543210" not in out


def test_h3_basis_redacted_before_persisting(tmp_path: Path) -> None:
    prof = _profile(tmp_path, "finance")
    prof.set_fact(
        "rent", D(20000), "finance", "VERIFIED", "paid by card 4111 1111 1111 1111, receipt to me@example.com"
    )
    raw = (tmp_path / "home" / "profile" / "facts.json").read_text(encoding="utf-8")
    assert "4111" not in raw and "me@example.com" not in raw
    assert "[EMAIL]" in prof.get("rent").basis  # type: ignore[union-attr]


def test_m1_export_is_journaled_without_values(tmp_path: Path) -> None:
    prof = _profile(tmp_path, "finance")
    prof.set_fact("rent", D(20000), "finance", "VERIFIED", "lease")
    prof.export()
    last = prof.journal.entries()[-1]["event"]
    assert last == {"event": "profile_export", "fact_count": 1, "categories": ["finance"]}
    assert "20000" not in prof.journal.path.read_text(encoding="utf-8")
    assert "consent" in (Profile.export.__doc__ or "").casefold()


def test_m2_mkdir_result_symlink_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    real_mkdir = os.mkdir

    def fake(p: Any, mode: int = 0o777, *a: Any, **k: Any) -> None:
        if Path(p) == root:
            os.symlink(elsewhere, p)
            return
        real_mkdir(p, mode, *a, **k)

    monkeypatch.setattr(os, "mkdir", fake)
    with pytest.raises(InsecureStore):
        PrivateStore(root).ensure()


def test_m2_mkdir_result_mode_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "home"
    real_mkdir = os.mkdir

    def fake(p: Any, mode: int = 0o777, *a: Any, **k: Any) -> None:
        real_mkdir(p, 0o755) if Path(p) == root else real_mkdir(p, mode, *a, **k)

    monkeypatch.setattr(os, "mkdir", fake)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    old = os.umask(0o022)
    try:
        with pytest.raises(InsecureStore):
            PrivateStore(root).ensure()
    finally:
        os.umask(old)


def test_m3_symlink_escape_rejected(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "home").ensure()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "home" / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        store.path("link/x.json")
    (tmp_path / "home" / "f.json").symlink_to(outside / "f.json")
    with pytest.raises(ValueError):
        store.path("f.json")


def test_l1_deep_rule_value_blocks() -> None:
    value: Any = 1
    for _ in range(40):
        value = [value]
    pack = {
        "pack_id": "p",
        "title": "t",
        "values": [
            {
                "id": "deep",
                "value": value,
                "unit": "x",
                "effective_from": "2026-01-01",
                "effective_to": None,
                "source_url": "https://example.gov.in/x",
                "source_tier": "primary",
                "retrieved_on": "2026-10-01",
                "verified_by_human": True,
                "refresh_cadence_days": 30,
            }
        ],
    }
    with pytest.raises(RulePackBlocked):
        load_rule_pack(pack)


def test_l2_gitignore_excludes_life_mentor_home() -> None:
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".life_mentor/" in lines


def test_l4_fact_repr_hides_value_and_basis() -> None:
    fact = Fact("take_home_monthly", D("85123"), "finance", "VERIFIED", "payslip sept", NOW)
    text = repr(fact)
    assert "85123" not in text and "payslip" not in text
    assert "take_home_monthly" in text and "VERIFIED" in text


# --- CODE --------------------------------------------------------------------


def test_c1_money_losing_option_not_recommended(rates) -> None:
    gym = _course(
        name="gym",
        kind="subscription",
        upfront_cost=D(0),
        recurring_monthly_cost=D(5000),
        benefit_monthly=benefits(D(0), D(100), D(200), basis="feel better", evidence_class="EMPIRICAL"),
    )
    rep = analyze_decision(gym, situation=_sit(), rates=rates, as_of=AS_OF)
    opt = _opt(rep, "gym")
    assert _row(opt, 6)["disposition"] == HOLD
    assert _row(opt, 7)["disposition"] == HOLD
    assert opt["gate"]["disposition"] == HOLD
    rec = rep["result"]["recommendation"]
    assert rec["status"] == "recommend" and rec["option"] == BASELINE_NAME


def test_c2_missing_benefit_is_insufficient_information(rates) -> None:
    rep = analyze_decision(_course(benefit_monthly=None), situation=_sit(), rates=rates, as_of=AS_OF)
    rec = rep["result"]["recommendation"]
    assert rec["status"] == "insufficient_information" and rec["option"] is None
    assert "benefit_monthly" in rec["missing"]


def test_c2_situation_question_is_insufficient_information(rates) -> None:
    q = {"key": "goals", "category": "goals", "reason": "no_consent", "question": "May I?"}
    rep = analyze_decision(
        _course(), situation=_sit(questions=(q,), emergency_fund_balance=D(500000)), rates=rates, as_of=AS_OF
    )
    assert rep["result"]["recommendation"]["status"] == "insufficient_information"


def test_c3_upfront_above_emergency_fund_blocks(tmp_path: Path, rates) -> None:
    store = PrivateStore(tmp_path / "home")
    rep = analyze_decision(
        _course(upfront_cost=D(400000)), situation=_sit(), rates=rates, as_of=AS_OF, store=store
    )
    assert _row(_opt(rep, "data_course"), 3)["disposition"] == BLOCK
    with pytest.raises(ValueError):
        record_confirmation(store, rep, "data_course", identity=ME)


def test_c4_profile_category_mismatch_blocks_decision(tmp_path: Path, rates) -> None:
    prof = _profile(tmp_path, "finance", "career", "goals")
    prof.set_fact("take_home_monthly", D(80000), "career", "VERIFIED", "payslip")
    prof.set_fact("essential_spend_monthly", D(55000), "finance", "VERIFIED", "bank")
    prof.set_fact("emergency_fund_balance", D(500000), "finance", "VERIFIED", "bank")
    prof.set_fact("goal_upskill", "learn", "goals", "USER_ESTIMATE", "stated")
    sit = situation_from_profile(prof)
    assert any(r["disposition"] == BLOCK for r in sit.profile_reasons)
    rep = analyze_decision(_course(), situation=sit, rates=rates, as_of=AS_OF)
    assert rep["gate"]["disposition"] == BLOCK
    assert rep["result"]["recommendation"]["status"] != "recommend"


def test_c4_profile_category_mismatch_blocks_tax(tmp_path: Path) -> None:
    prof = _profile(tmp_path, "finance", "career")
    prof.set_fact("gross_salary_annual", D(1300000), "career", "VERIFIED", "Form 16")
    rep = income_tax_from_profile(prof, load_tax_pack(), as_of=AS_OF, regime="new")
    assert rep["gate"]["disposition"] == BLOCK


@pytest.mark.parametrize(("months", "target", "disp"), [(D(3), D(165000), None), (D("2.5"), None, BLOCK)])
def test_c5_decimal_emergency_months(
    tmp_path: Path, rates, months: Decimal, target: Decimal | None, disp: str | None
) -> None:
    prof = _profile(tmp_path, "finance", "goals")
    prof.set_fact("take_home_monthly", D(80000), "finance", "VERIFIED", "payslip")
    prof.set_fact("essential_spend_monthly", D(55000), "finance", "VERIFIED", "bank")
    prof.set_fact("emergency_fund_balance", D(500000), "finance", "VERIFIED", "bank")
    prof.set_fact("emergency_fund_months", months, "finance", "USER_ESTIMATE", "chosen")
    rep = analyze_decision(_course(), situation=situation_from_profile(prof), rates=rates, as_of=AS_OF)
    row3 = _row(_opt(rep, "data_course"), 3)
    if target is not None:
        assert row3["values"]["emergency_fund_target"] == target
    else:
        assert row3["disposition"] == disp


def _th_report() -> dict:
    return take_home(
        load_tax_pack(), as_of=AS_OF, regime="new", gross_salary=D(1300000), payroll_deductions=D(21600)
    )


def test_c6_take_home_report_validated(tmp_path: Path) -> None:
    prof = _profile(tmp_path, "finance", "goals")
    wrong = income_tax(load_tax_pack(), as_of=AS_OF, regime="new", gross_salary=D(1300000))
    with pytest.raises(ValueError):
        situation_from_profile(prof, take_home_report=wrong)
    for bad in (104366.67, "104366.67"):
        rep = _th_report()
        rep["result"]["take_home_monthly"] = bad
        with pytest.raises(TypeError):
            situation_from_profile(prof, take_home_report=rep)


def test_c6_take_home_report_as_of_mismatch_holds(tmp_path: Path, rates) -> None:
    prof = _profile(tmp_path, "finance", "goals")
    sit = situation_from_profile(prof, take_home_report=_th_report())
    rep = analyze_decision(_course(), situation=sit, rates=rates, as_of=date(2026, 11, 1))
    assert _row(_opt(rep, "data_course"), 2)["disposition"] == HOLD
    assert "take_home_monthly" in rep["result"]["recommendation"]["missing"]


@pytest.mark.parametrize("bad", [5.0, 5, "5"])
def test_c6_input_value_requires_decimal(bad: Any) -> None:
    with pytest.raises(TypeError):
        InputValue("x", bad, "VERIFIED", "b")


def test_c7_edited_report_cannot_be_confirmed(tmp_path: Path, rates) -> None:
    store = PrivateStore(tmp_path / "home")
    sub = _course(name="gym", kind="subscription", upfront_cost=D(0), recurring_monthly_cost=D(30000))
    rep = analyze_decision(sub, situation=_sit(), rates=rates, as_of=AS_OF, store=store)
    assert _opt(rep, "gym")["gate"]["disposition"] == BLOCK
    _opt(rep, "gym")["gate"]["disposition"] = SHIP  # tamper
    with pytest.raises(ValueError):
        record_confirmation(store, rep, "gym", identity=ME)
    with pytest.raises(ValueError):
        record_confirmation(store, {**rep, "result": None}, "gym", identity=ME)


def test_c9_compounding_mismatch_stated(rates) -> None:
    rep = analyze_decision(_course(), situation=_sit(), rates=rates, as_of=AS_OF)
    text = " ".join(rep["assumptions"]).casefold()
    assert "quarterly" in text and "monthly compounding" in text


def test_c10_tax_rate_key_is_explicit() -> None:
    rep = income_tax(load_tax_pack(), as_of=AS_OF, regime="new", gross_salary=D(1300000))
    r = rep["result"]
    assert "effective_rate_percent" not in r
    assert r["tax_as_percent_of_taxable_income"] == D("2.12")  # 26,000 / 12,25,000
    assert find_forbidden(rep) == []
