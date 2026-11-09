"""LM01 profile & consent: typed facts with provenance, consent ledger, gate."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.profile import (
    CATEGORIES,
    PROVENANCES,
    ConsentLedger,
    ConsentRequired,
    Fact,
    Profile,
    Requirement,
    profile_gate,
)
from life_os.life_mentor.security import PrivateStore

NOW = "2026-10-11T00:00:00Z"


def _profile(tmp_path: Path, *, consent: tuple[str, ...] = ()) -> Profile:
    prof = Profile(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    for cat in consent:
        prof.consent.grant(cat, purpose="test")
    return prof


# --- Fact ------------------------------------------------------------------


def test_vocabularies() -> None:
    assert set(CATEGORIES) == {"identity", "finance", "health", "career", "lifestyle", "goals"}
    assert PROVENANCES == ("VERIFIED", "USER_ESTIMATE", "ASSUMPTION", "UNKNOWN")


def test_fact_rejects_unknown_category_and_provenance() -> None:
    with pytest.raises(ValueError):
        Fact("k", 1, "wealth", "VERIFIED", "payslip", NOW)
    with pytest.raises(ValueError):
        Fact("k", 1, "finance", "GUESS", "payslip", NOW)


def test_fact_rejects_float_money() -> None:
    with pytest.raises(TypeError):
        Fact("take_home_monthly", 85000.5, "finance", "VERIFIED", "payslip", NOW)


def test_fact_unknown_must_have_no_value_and_known_must_have_value() -> None:
    with pytest.raises(ValueError):
        Fact("k", 5, "finance", "UNKNOWN", "", NOW)
    with pytest.raises(ValueError):
        Fact("k", None, "finance", "VERIFIED", "payslip", NOW)
    Fact("k", None, "finance", "UNKNOWN", "user does not know", NOW)


def test_fact_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        Fact("", 1, "finance", "VERIFIED", "x", NOW)


def test_fact_roundtrip_preserves_decimal() -> None:
    fact = Fact("take_home_monthly", Decimal("85000.50"), "finance", "VERIFIED", "payslip", NOW)
    again = Fact.from_dict(fact.to_dict())
    assert again == fact
    assert isinstance(again.value, Decimal)


# --- consent ---------------------------------------------------------------


def test_consent_grant_revoke_journaled(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "home")
    ledger = ConsentLedger(store, clock=lambda: NOW)
    assert not ledger.has("health")
    ledger.grant("health", purpose="nutrition advice")
    assert ledger.has("health")
    ledger.revoke("health", reason="changed mind")
    assert not ledger.has("health")
    # A fresh ledger replays the journal, not an in-memory cache.
    again = ConsentLedger(store, clock=lambda: NOW)
    assert not again.has("health")
    events = [e["event"]["event"] for e in again.journal.entries()]
    assert events == ["consent_grant", "consent_revoke"]
    assert again.journal.verify() == (True, None)


def test_consent_unknown_category_rejected(tmp_path: Path) -> None:
    ledger = ConsentLedger(PrivateStore(tmp_path / "home"))
    with pytest.raises(ValueError):
        ledger.grant("wealth", purpose="x")


def test_consent_status_lists_every_category(tmp_path: Path) -> None:
    ledger = ConsentLedger(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    ledger.grant("finance", purpose="budget")
    status = ledger.status()
    assert set(status) == set(CATEGORIES)
    assert status["finance"] is True and status["health"] is False


# --- profile store ---------------------------------------------------------


def test_health_fact_without_consent_raises(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    with pytest.raises(ConsentRequired) as exc:
        prof.set_fact("resting_hr", 60, "health", "USER_ESTIMATE", "watch")
    assert exc.value.category == "health"
    assert prof.export()["facts"] == {}


@pytest.mark.parametrize("category", ["identity", "goals"])
def test_every_category_requires_consent(tmp_path: Path, category: str) -> None:
    prof = _profile(tmp_path)
    with pytest.raises(ConsentRequired):
        prof.set_fact("x", "y", category, "USER_ESTIMATE", "said so")


def test_set_and_get_fact_persists(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    prof.set_fact("take_home_monthly", Decimal(85000), "finance", "VERIFIED", "payslip")
    again = Profile(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    fact = again.get("take_home_monthly")
    assert fact is not None
    assert fact.value == Decimal(85000) and fact.provenance == "VERIFIED"
    assert fact.recorded_at == NOW


def test_set_fact_refuses_silent_overwrite(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    prof.set_fact("rent", Decimal(20000), "finance", "VERIFIED", "lease")
    with pytest.raises(ValueError):
        prof.set_fact("rent", Decimal(21000), "finance", "VERIFIED", "lease")


def test_get_after_revoke_requires_consent(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    prof.set_fact("rent", Decimal(20000), "finance", "VERIFIED", "lease")
    prof.consent.revoke("finance", reason="pause")
    with pytest.raises(ConsentRequired):
        prof.get("rent")


def test_get_missing_returns_none(tmp_path: Path) -> None:
    assert _profile(tmp_path).get("nothing") is None


def test_export_returns_all_facts(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance", "career", "goals"))
    prof.set_fact("rent", Decimal(20000), "finance", "VERIFIED", "lease")
    prof.set_fact("role", "analyst", "career", "VERIFIED", "offer letter")
    prof.set_fact("goal_house", "buy home by 2030", "goals", "USER_ESTIMATE", "stated")
    data = prof.export()
    assert set(data["facts"]) == {"rent", "role", "goal_house"}
    assert data["facts"]["rent"]["provenance"] == "VERIFIED"
    assert data["consent"]["finance"] is True
    assert data["journal_ok"] is True


def test_correct_replaces_value_and_journals_without_value(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    prof.set_fact("rent", Decimal(20000), "finance", "USER_ESTIMATE", "guess")
    prof.correct("rent", Decimal(21500), "VERIFIED", "lease p2")
    fact = prof.get("rent")
    assert fact is not None and fact.value == Decimal(21500) and fact.provenance == "VERIFIED"
    events = [e["event"] for e in prof.journal.entries()]
    assert [e["event"] for e in events] == ["fact_set", "fact_correct"]
    assert "21500" not in prof.journal.path.read_text(encoding="utf-8")


def test_correct_missing_fact_raises(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    with pytest.raises(KeyError):
        prof.correct("rent", Decimal(1), "VERIFIED", "x")


def test_delete_category_removes_and_journals(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance", "career"))
    prof.set_fact("rent", Decimal(20000), "finance", "VERIFIED", "lease")
    prof.set_fact("emi", Decimal(5000), "finance", "VERIFIED", "loan")
    prof.set_fact("role", "analyst", "career", "VERIFIED", "offer")
    removed = prof.delete("finance")
    assert sorted(removed) == ["emi", "rent"]
    assert set(prof.export()["facts"]) == {"role"}
    # Review fix M1: export() is now journaled, so the delete is the second-to-last event.
    events = [e["event"] for e in prof.journal.entries()]
    assert events[-1]["event"] == "profile_export"
    last = events[-2]
    assert last["event"] == "category_delete"
    assert last["category"] == "finance" and last["count"] == 2
    assert prof.journal.verify() == (True, None)
    # Persisted, not just in memory.
    fresh = Profile(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    assert "rent" not in fresh.export()["facts"]


def test_delete_works_without_consent(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("health",))
    prof.set_fact("resting_hr", 60, "health", "USER_ESTIMATE", "watch")
    prof.consent.revoke("health", reason="stop")
    assert prof.delete("health") == ["resting_hr"]


def test_facts_file_is_private(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    prof.set_fact("rent", Decimal(20000), "finance", "VERIFIED", "lease")
    files = [p for p in (tmp_path / "home").rglob("*") if p.is_file()]
    assert files
    for f in files:
        assert (f.stat().st_mode & 0o777) == 0o600, f


# --- missing facts / gate --------------------------------------------------


REQUIRED = [
    Requirement("take_home_monthly", "finance", "What is your monthly take-home pay?"),
    Requirement("essential_spend_monthly", "finance"),
]


def test_missing_required_finance_fact_holds_with_question(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    prof.set_fact("take_home_monthly", Decimal(85000), "finance", "VERIFIED", "payslip")
    questions = prof.missing_facts(REQUIRED)
    assert [q["key"] for q in questions] == ["essential_spend_monthly"]
    assert "essential_spend_monthly" in questions[0]["question"]
    outcome = profile_gate(prof, REQUIRED)
    assert outcome.gate.disposition == HOLD
    assert outcome.questions == questions
    assert any("essential_spend_monthly" in r["reason"] for r in outcome.gate.reasons)


def test_unknown_fact_counts_as_missing(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    prof.set_fact("take_home_monthly", None, "finance", "UNKNOWN", "not sure")
    prof.set_fact("essential_spend_monthly", Decimal(30000), "finance", "USER_ESTIMATE", "app")
    qs = prof.missing_facts(REQUIRED)
    assert [(q["key"], q["reason"]) for q in qs] == [("take_home_monthly", "unknown")]
    assert qs[0]["question"] == "What is your monthly take-home pay?"
    assert profile_gate(prof, REQUIRED).gate.disposition == HOLD


def test_missing_consent_produces_consent_question(tmp_path: Path) -> None:
    prof = _profile(tmp_path)
    qs = prof.missing_facts(REQUIRED)
    assert {q["reason"] for q in qs} == {"no_consent"}
    assert profile_gate(prof, REQUIRED).gate.disposition == HOLD


def test_assumption_holds_unless_allowed(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    prof.set_fact("take_home_monthly", Decimal(85000), "finance", "ASSUMPTION", "typical")
    prof.set_fact("essential_spend_monthly", Decimal(30000), "finance", "VERIFIED", "bank")
    assert profile_gate(prof, REQUIRED).gate.disposition == HOLD
    assert [q["reason"] for q in prof.missing_facts(REQUIRED)] == ["assumption"]
    allowed = profile_gate(prof, REQUIRED, allow_assumptions=True)
    assert allowed.gate.disposition == SHIP
    assert allowed.questions == []


def test_all_present_ships(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    prof.set_fact("take_home_monthly", Decimal(85000), "finance", "VERIFIED", "payslip")
    prof.set_fact("essential_spend_monthly", Decimal(30000), "finance", "USER_ESTIMATE", "app")
    outcome = profile_gate(prof, REQUIRED)
    assert outcome.gate.disposition == SHIP
    assert outcome.questions == []


def test_category_mismatch_blocks(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance", "career"))
    prof.set_fact("take_home_monthly", Decimal(85000), "career", "VERIFIED", "payslip")
    prof.set_fact("essential_spend_monthly", Decimal(30000), "finance", "VERIFIED", "bank")
    assert profile_gate(prof, REQUIRED).gate.disposition == BLOCK


def test_empty_requirements_hold(tmp_path: Path) -> None:
    assert profile_gate(_profile(tmp_path), []).gate.disposition == HOLD


def test_default_question_names_key_and_category(tmp_path: Path) -> None:
    prof = _profile(tmp_path, consent=("finance",))
    qs = prof.missing_facts([Requirement("rent", "finance")])
    assert qs[0]["category"] == "finance" and qs[0]["reason"] == "missing"
    assert "rent" in qs[0]["question"] and "finance" in qs[0]["question"]


def test_requirement_rejects_unknown_category() -> None:
    with pytest.raises(ValueError):
        Requirement("rent", "wealth")
