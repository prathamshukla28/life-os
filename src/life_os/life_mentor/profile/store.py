"""Profile store: consented, provenance-tagged facts with export/correct/delete (LM01).

Layout inside the private store root:

    consent.jsonl            hash-chained consent journal (ConsentLedger)
    profile/facts.json       current facts, atomically rewritten (0600)
    profile/journal.jsonl    hash-chained log of fact_set / fact_correct /
                             category_delete events (keys + provenance + counts,
                             NEVER values)

Rules:
  - storing, correcting, or reading a fact for use requires current consent
    for its category (``ConsentRequired``);
  - ``set_fact`` never silently overwrites: an existing key must go through
    ``correct`` so the change is journaled (ASUR-VERSION-01); it raises
    ``FactExists`` (a ``ValueError``);
  - money facts (``MONEY_FACTS``) must be a non-negative number within
    ``MAX_AMOUNT`` when written, by ``set_fact`` and ``correct`` alike;
  - ``export`` and ``delete`` work without consent - the data is the user's.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from life_os.core.clock import utc_now_iso
from life_os.core.gate import BLOCK, HOLD, SHIP, Check, GateResult, run_gate

from ..security import Journal, PrivateStore
from .consent import ConsentLedger
from .facts import ASSUMPTION, CATEGORIES, UNKNOWN, Fact, check_money_fact

__all__ = [
    "FACTS_FILE",
    "PROFILE_JOURNAL",
    "FactExists",
    "Profile",
    "ProfileGateOutcome",
    "Requirement",
    "profile_gate",
]

FACTS_FILE = "profile/facts.json"
PROFILE_JOURNAL = "profile/journal.jsonl"


class FactExists(ValueError):
    """``set_fact`` on a key that already exists (change it with ``correct``)."""

    def __init__(self, key: str) -> None:
        super().__init__(f"fact {key!r} already exists; use Profile.correct() so the change is journaled")
        self.key = key


@dataclass(frozen=True)
class Requirement:
    """A fact some analysis needs: its key, category, and (optional) question."""

    key: str
    category: str
    prompt: str = ""

    def __post_init__(self) -> None:
        if not self.key or not self.key.strip():
            raise ValueError("requirement key must be non-empty")
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category!r}")


class Profile:
    """The user's facts, gated by consent and journaled."""

    def __init__(self, store: PrivateStore, *, clock: Callable[[], str] = utc_now_iso) -> None:
        self.store = store
        self._clock = clock
        self.consent = ConsentLedger(store, clock=clock)
        self.journal: Journal = store.journal(PROFILE_JOURNAL, clock=clock)

    # -- persistence --------------------------------------------------------

    def _load(self) -> dict[str, Fact]:
        raw = self.store.read_json(FACTS_FILE, default={"facts": {}})
        return {k: Fact.from_dict(v) for k, v in raw.get("facts", {}).items()}

    def _save(self, facts: dict[str, Fact]) -> None:
        self.store.write_json(FACTS_FILE, {"facts": {k: f.to_dict() for k, f in sorted(facts.items())}})

    # -- API ----------------------------------------------------------------

    def set_fact(self, key: str, value: Any, category: str, provenance: str, basis: str) -> Fact:
        """Store a NEW fact. Requires consent; refuses to overwrite (use ``correct``)."""
        check_money_fact(key, value)
        fact = Fact(key, value, category, provenance, basis, self._clock())
        self.consent.require(category)
        facts = self._load()
        if key in facts:
            raise FactExists(key)
        facts[key] = fact
        # Journal FIRST: append() verifies the chain and raises JournalTampered, so a
        # tampered journal now blocks the write (it used to land unjournaled), and a
        # crash between the two steps can only over-report, never hide, a change.
        self.journal.append({"event": "fact_set", "key": key, "category": category, "provenance": provenance})
        self._save(facts)
        return fact

    def get(self, key: str) -> Fact | None:
        """Return the fact for use (consent required), or None if absent."""
        fact = self._load().get(key)
        if fact is None:
            return None
        self.consent.require(fact.category)
        return fact

    def correct(self, key: str, value: Any, provenance: str, basis: str) -> Fact:
        """Replace an existing fact's value/provenance; journals the change (no values)."""
        facts = self._load()
        if key not in facts:
            raise KeyError(key)
        old = facts[key]
        self.consent.require(old.category)
        check_money_fact(key, value)
        new = Fact(key, value, old.category, provenance, basis, self._clock())
        facts[key] = new
        self.journal.append(
            {
                "event": "fact_correct",
                "key": key,
                "category": old.category,
                "old_provenance": old.provenance,
                "new_provenance": provenance,
            }
        )
        self._save(facts)
        return new

    def delete(self, category: str) -> list[str]:
        """Delete every fact in *category*. Journaled; works without consent."""
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        facts = self._load()
        removed = sorted(k for k, f in facts.items() if f.category == category)
        for k in removed:
            del facts[k]
        self.journal.append(
            {"event": "category_delete", "category": category, "keys": removed, "count": len(removed)}
        )
        self._save(facts)
        return removed

    def export(self) -> dict[str, Any]:
        """Everything Life Mentor holds about the user.

        Export intentionally BYPASSES consent: it is the user's own right of
        access to their data, so it must work even after every consent is
        revoked. Each export is journaled as ``profile_export`` with the fact
        count and categories only (no keys' values). If a journal is broken the
        export still returns (``journal_ok: False``) but is not journaled -
        appending onto tampered history is refused.
        """
        consent_ok, _ = self.consent.journal.verify()
        profile_ok, _ = self.journal.verify()
        facts = self._load()
        if profile_ok:
            self.journal.append(
                {
                    "event": "profile_export",
                    "fact_count": len(facts),
                    "categories": sorted({f.category for f in facts.values()}),
                }
            )
        return {
            "facts": {k: f.to_dict() for k, f in sorted(facts.items())},
            "consent": self.consent.status() if consent_ok else {},
            "journal_ok": consent_ok and profile_ok,
        }

    def facts_in_category(self, category: str) -> dict[str, Fact]:
        """Facts of one category for use (consent required; not journaled)."""
        self.consent.require(category)
        return {k: f for k, f in sorted(self._load().items()) if f.category == category}

    def missing_facts(
        self, requirements: Iterable[Requirement], *, allow_assumptions: bool = False
    ) -> list[dict[str, str]]:
        """Return one question per requirement that is not satisfied.

        Each question: {"key", "category", "reason", "question"} where reason is
        no_consent | missing | category_mismatch | unknown | assumption.
        """
        facts = self._load()
        consent = self.consent.status()
        questions: list[dict[str, str]] = []
        for req in requirements:
            q = _question_for(req, facts.get(req.key), consent[req.category], allow_assumptions)
            if q is not None:
                questions.append(q)
        return questions


def _question_for(
    req: Requirement, fact: Fact | None, consented: bool, allow_assumptions: bool
) -> dict[str, str] | None:
    ask = req.prompt or f"Please provide '{req.key}' ({req.category}) and how you know it."
    if not consented:
        reason = "no_consent"
        text = f"May Life Mentor store and use your {req.category} information? It is needed for '{req.key}'."
    elif fact is None:
        reason, text = "missing", ask
    elif fact.category != req.category:
        reason = "category_mismatch"
        text = (
            f"'{req.key}' is recorded under {fact.category!r} but is needed as "
            f"{req.category!r}; please confirm or correct it."
        )
    elif fact.provenance == UNKNOWN:
        reason, text = "unknown", ask
    elif fact.provenance == ASSUMPTION and not allow_assumptions:
        reason = "assumption"
        text = f"'{req.key}' is currently an assumption; please confirm or correct it. {ask}"
    else:
        return None
    return {"key": req.key, "category": req.category, "reason": reason, "question": text}


@dataclass
class ProfileGateOutcome:
    """Gate disposition plus the questions that would clear a HOLD."""

    gate: GateResult
    questions: list[dict[str, str]] = field(default_factory=list)


def profile_gate(
    profile: Profile,
    requirements: Iterable[Requirement],
    *,
    allow_assumptions: bool = False,
    name: str = "life_os.life_mentor.profile",
) -> ProfileGateOutcome:
    """Fail-closed gate: missing/UNKNOWN/unconsented -> HOLD; mismatch -> BLOCK.

    No requirements -> HOLD (``run_gate`` with no checks: nothing was verified).
    """
    reqs = list(requirements)
    questions = profile.missing_facts(reqs, allow_assumptions=allow_assumptions)
    by_key = {q["key"]: q for q in questions}

    def make_check(req: Requirement) -> Check:
        def fn() -> tuple[str, str]:
            q = by_key.get(req.key)
            if q is None:
                return SHIP, f"{req.key}: present"
            disposition = BLOCK if q["reason"] == "category_mismatch" else HOLD
            return disposition, f"{req.key}: {q['reason']} - {q['question']}"

        return Check(name=f"fact:{req.key}", fn=fn)

    gate = run_gate(name, [make_check(r) for r in reqs])
    return ProfileGateOutcome(gate=gate, questions=questions)
