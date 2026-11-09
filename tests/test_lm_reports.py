"""LM09 reports: personalised Markdown assembled from calculator outputs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.profile import Profile
from life_os.life_mentor.reports import (
    DISCLAIMERS,
    Section,
    career_report,
    decisions_report,
    finance_report,
    health_report,
    life_plan_report,
    lifestyle_report,
    render_report,
)
from life_os.life_mentor.security import PrivateStore, find_forbidden, redact
from tests.lm_persona import AS_OF, NOW, build_persona, course_decision

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "lm_golden_life_plan.md"
GEN = "2026-10-11"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _clean(text: str) -> None:
    assert find_forbidden(text) == []
    assert redact(text) == text
    low = text.casefold()
    assert "guarantee" not in low


@pytest.fixture
def persona(tmp_path: Path) -> Profile:
    return build_persona(tmp_path / "lm")


# --- golden life plan ---------------------------------------------------------------------


def test_life_plan_golden(persona: Profile) -> None:
    rep = life_plan_report(
        persona, as_of=AS_OF, generated_on=GEN, decision_reports=[course_decision(persona)]
    )
    assert rep.markdown == GOLDEN.read_text(encoding="utf-8")


def test_life_plan_contains_worked_numbers(persona: Profile) -> None:
    text = life_plan_report(
        persona, as_of=AS_OF, generated_on=GEN, decision_reports=[course_decision(persona)]
    ).markdown
    _clean(text)
    for number in (
        "13,00,000",
        "12,25,000",
        "26,000",
        "12,52,400",
        "1,04,366.67",
        "39,366.67",
        "3,30,000",
        "30,000",
        "3,50,000",
        "4,273.47",
        "3,90,000",
        "90,000",
        "42.90",
        "53.95",
    ):
        assert number in text, number
    for heading in (
        "## At a glance",
        "## Finance",
        "## Health",
        "## Lifestyle",
        "## Career",
        "## Decisions",
        "## Next steps",
    ):
        assert heading in text
    steps = text.split("## Next steps", 1)[1].split("\n## ", 1)[0]
    assert 1 <= sum(1 for line in steps.splitlines() if line[:2] in ("1.", "2.", "3.")) <= 3
    assert "UNVERIFIED" in text and "https://" in text and "2026-10-11" in text
    assert "not tax, investment or legal advice" in text and "not medical advice" in text


def test_reports_are_read_only_and_deterministic(persona: Profile) -> None:
    before = _snapshot(persona.store.root)
    a = life_plan_report(persona, as_of=AS_OF, generated_on=GEN).markdown
    b = life_plan_report(persona, as_of=AS_OF, generated_on=GEN).markdown
    assert a == b and _snapshot(persona.store.root) == before


# --- domain reports ---------------------------------------------------------------------------


def test_finance_report_sections(persona: Profile) -> None:
    rep = finance_report(persona, as_of=AS_OF, generated_on=GEN)
    _clean(rep.markdown)
    titles = [s.title for s in rep.sections]
    assert titles[:3] == ["Take-home pay", "Monthly cash flow", "Emergency fund"]
    assert rep.disposition in (SHIP, HOLD)
    for part in (
        "### Inputs",
        "### Rules and sources",
        "### Assumptions",
        "### Result",
        "### What would change this",
        "### Suggested actions (up to 3)",
        "## Next steps",
        "VERIFIED",
        "USER_ESTIMATE",
    ):
        assert part in rep.markdown, part
    debts = next(s for s in rep.sections if s.title == "Debt payoff")
    assert debts.report is None and not debts.required
    assert "debts" in debts.missing


def test_missing_required_facts_hold_with_questions(tmp_path: Path) -> None:
    prof = Profile(PrivateStore(tmp_path / "lm"), clock=lambda: NOW)
    prof.consent.grant("finance", purpose="t")
    rep = finance_report(prof, as_of=AS_OF, generated_on=GEN)
    assert rep.disposition == HOLD
    take = rep.sections[0]
    assert take.report is None and "gross_salary_annual" in take.missing
    assert "### Questions" in rep.markdown and "gross_salary_annual" in rep.markdown
    assert "Not available" in rep.markdown
    _clean(rep.markdown)


def test_no_consent_never_shows_values(tmp_path: Path) -> None:
    prof = build_persona(tmp_path / "lm")
    prof.consent.revoke("health", reason="pause")
    rep = health_report(prof, as_of=AS_OF, generated_on=GEN)
    assert rep.disposition == HOLD
    assert "53.95" not in rep.markdown and "no_consent" in rep.markdown


def test_health_lifestyle_career_decisions(persona: Profile) -> None:
    h = health_report(persona, as_of=AS_OF, generated_on=GEN)
    assert "42.90" in h.markdown and "not medical advice" in h.markdown
    life = lifestyle_report(persona, as_of=AS_OF, generated_on=GEN)
    assert "walk 20 min" in life.markdown
    assert "correlation is not causation" in life.markdown.casefold()
    c = career_report(persona, as_of=AS_OF, generated_on=GEN)
    assert c.disposition == HOLD and "ambitions" in c.markdown
    d = decisions_report(persona, as_of=AS_OF, generated_on=GEN)
    assert "decision_analyzed" in d.markdown and "aaaaaaaaaaaa" in d.markdown
    for r in (h, life, c, d):
        _clean(r.markdown)


def test_decisions_need_finance_consent(tmp_path: Path) -> None:
    prof = build_persona(tmp_path / "lm")
    prof.consent.revoke("finance", reason="pause")
    d = decisions_report(prof, as_of=AS_OF, generated_on=GEN)
    assert d.disposition == HOLD and "aaaaaaaaaaaa" not in d.markdown


def test_tampered_journal_blocks_decisions(tmp_path: Path) -> None:
    prof = build_persona(tmp_path / "lm")
    (prof.store.root / "decisions" / "journal.jsonl.head").unlink()
    assert decisions_report(prof, as_of=AS_OF, generated_on=GEN).disposition == BLOCK


# --- renderer contract --------------------------------------------------------------------------


def test_render_report_dispositions_and_optional_sections() -> None:
    ok = {
        "calculator": "net_worth",
        "inputs": {},
        "rules_used": [],
        "assumptions": [],
        "result": {"net_worth": 1},
        "gate": {"disposition": SHIP, "reasons": []},
    }
    rep = render_report(
        "finance",
        "Test",
        [Section("A", ok), Section("B", None, missing=("x",), required=False)],
        as_of=date(2026, 10, 11),
        generated_on=GEN,
        disclaimer=DISCLAIMERS["finance"],
    )
    assert rep.disposition == SHIP
    assert "Not provided (optional)" in rep.markdown
    req = render_report(
        "finance",
        "Test",
        [Section("B", None, missing=("x",))],
        as_of=date(2026, 10, 11),
        generated_on=GEN,
        disclaimer=DISCLAIMERS["finance"],
    )
    assert req.disposition == HOLD


def test_render_rejects_promise_language() -> None:
    bad = {
        "calculator": "x",
        "inputs": {},
        "rules_used": [],
        "assumptions": [],
        "result": {"note": "returns are guaranteed"},
        "gate": {"disposition": SHIP, "reasons": []},
    }
    with pytest.raises(ValueError):
        render_report("finance", "T", [Section("A", bad)], as_of=AS_OF, generated_on=GEN, disclaimer="")
