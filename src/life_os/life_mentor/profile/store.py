"""Profile store: consented, provenance-tagged facts with export/correct/delete (LM01).

Layout inside the private store root:

    consent.jsonl            hash-chained consent journal (ConsentLedger)
    profile/facts.json       current facts plus a random salt, atomically
                             rewritten (0600)
    profile/journal.jsonl    hash-chained log of fact_set / fact_correct /
                             category_delete events (keys + provenance + counts,
                             NEVER values) and a salted digest of facts.json

Rules:
  - storing, correcting, or reading a fact for use requires current consent
    for its category (``ConsentRequired``);
  - ``set_fact`` never silently overwrites: an existing key must go through
    ``correct`` so the change is journaled (ASUR-VERSION-01); it raises
    ``FactExists`` (a ``ValueError``);
  - money facts (``MONEY_FACTS``) must be a non-negative number within
    ``MAX_AMOUNT`` when written, by ``set_fact`` and ``correct`` alike;
  - ``export`` and ``delete`` work without consent - the data is the user's.

facts.json must agree with the journal (``Profile.facts_problem``), else every read
or write raises ``FactsMismatch`` (fail closed) and ``export`` reports
``journal_ok: False``. Two checks:

  - replay: the keys, categories and provenance that the fact_set / fact_correct /
    category_delete events add up to must be exactly those in facts.json (catches an
    added, removed, re-categorised or re-labelled fact);
  - digest: each of those events records ``facts_digest`` = SHA-256 of the canonical
    facts.json it wrote; the current file must hash to the LAST one (catches an edited
    value or basis). The digest covers a random ``salt`` kept in facts.json, so the
    journal alone cannot be used to confirm a guessed value.

Journals written before the digest existed stay loadable: when the last fact event has
no ``facts_digest`` only the replay check applies, and the next write adds the salt and
starts recording digests. Like the journal's head anchor, this detects hand edits and
accidents, not someone who rewrites the whole directory; a crash between the journal
append and the facts.json write also fails it (recovery is a human decision).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from life_os.core.canonical import canonical_sha256
from life_os.core.clock import utc_now_iso
from life_os.core.gate import BLOCK, HOLD, SHIP, Check, GateResult, run_gate

from ..security import Journal, JournalTampered, PrivateStore, redact
from .consent import ConsentLedger
from .facts import ASSUMPTION, CATEGORIES, UNKNOWN, Fact, check_money_fact

__all__ = [
    "FACTS_FILE",
    "PROFILE_JOURNAL",
    "FactExists",
    "FactsMismatch",
    "Profile",
    "ProfileGateOutcome",
    "Requirement",
    "profile_gate",
]

FACTS_FILE = "profile/facts.json"
PROFILE_JOURNAL = "profile/journal.jsonl"
# Held across load -> journal -> write: two processes changing facts at once would
# otherwise lose one change (and then fail the facts/journal check).
FACTS_LOCK = "profile/facts.json.lock"
FACT_EVENTS = ("fact_set", "fact_correct", "category_delete")


class FactsMismatch(JournalTampered):
    """facts.json does not agree with the profile journal (see ``Profile.facts_problem``).

    A ``JournalTampered``, so every caller that already refuses a broken journal refuses
    this too.
    """

    def __init__(self, detail: str, index: int = 0) -> None:
        RuntimeError.__init__(
            self,
            f"{FACTS_FILE} does not match {PROFILE_JOURNAL}: {detail}. A file was changed outside "
            "Life Mentor (or a write was interrupted), so the facts are not used; restore it from "
            "a backup and re-check with `life-mentor journal verify`",
        )
        self.index = index
        self.detail = detail


def _facts_digest(doc: dict[str, Any]) -> str:
    """The digest recorded in the journal, as the journal stores it (strings are redacted)."""
    return redact(canonical_sha256(doc))


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

    def _read_doc(self) -> dict[str, Any]:
        raw = self.store.read_json(FACTS_FILE, default={"facts": {}})
        if not isinstance(raw, dict) or not isinstance(raw.get("facts", {}), dict):
            raise FactsMismatch("it is not a facts document")
        return raw

    def facts_problem(self) -> str | None:
        """Why facts.json disagrees with the profile journal, or None if it agrees.

        Raises ``JournalTampered`` if the journal itself is broken, ``ValueError`` if
        facts.json is not JSON.
        """
        return self._problem(self._read_doc())

    def _problem(self, doc: dict[str, Any]) -> str | None:
        expected: dict[str, tuple[Any, Any]] = {}
        digest: Any = None
        for entry in self.journal.entries():
            ev = entry["event"]
            kind = ev.get("event")
            if kind not in FACT_EVENTS:
                continue
            digest = ev.get("facts_digest")
            if kind == "fact_set":
                expected[str(ev.get("key"))] = (ev.get("category"), ev.get("provenance"))
            elif kind == "fact_correct":
                expected[str(ev.get("key"))] = (ev.get("category"), ev.get("new_provenance"))
            else:
                for key in ev.get("keys") or []:
                    expected.pop(str(key), None)
        # The journal holds redacted keys, so compare redacted keys. Two keys that redact
        # alike share one journal key: either one's category/provenance may match it.
        actual: dict[str, set[tuple[Any, Any]]] = {}
        for key, f in doc.get("facts", {}).items():
            meta = (f.get("category"), f.get("provenance")) if isinstance(f, dict) else (None, None)
            actual.setdefault(redact(str(key)), set()).add(meta)
        added = sorted(set(actual) - set(expected))
        removed = sorted(set(expected) - set(actual))
        changed = sorted(k for k in set(actual) & set(expected) if expected[k] not in actual[k])
        if added or removed or changed:
            parts = [
                f"{label} {', '.join(keys)}"
                for label, keys in (
                    ("facts not in the journal:", added),
                    ("journaled facts missing:", removed),
                    ("category or provenance differs for:", changed),
                )
                if keys
            ]
            return "; ".join(parts)
        if digest is not None and digest != _facts_digest(doc):
            return "its contents differ from what Life Mentor last wrote: digest mismatch"
        return None

    def _load_doc(self) -> dict[str, Any]:
        """facts.json, verified against the journal (``FactsMismatch`` if they disagree)."""
        doc = self._read_doc()
        try:
            problem = self._problem(doc)
        except FactsMismatch:
            raise
        except JournalTampered as exc:  # facts cannot be vouched for without the journal
            raise FactsMismatch(f"the journal is broken at entry {exc.index}", exc.index) from exc
        if problem is not None:
            raise FactsMismatch(problem, len(self.journal.entries()))
        return doc

    def _load(self) -> dict[str, Fact]:
        return {k: Fact.from_dict(v) for k, v in self._load_doc().get("facts", {}).items()}

    def _write(self, facts: dict[str, Fact], event: dict[str, Any]) -> None:
        """Journal *event* (with the digest of the new facts.json), then write facts.json.

        Journal FIRST: append() verifies the chain and raises JournalTampered, so a
        tampered journal blocks the write. A crash between the two steps leaves facts.json
        behind the journal, which the facts check then refuses: it can never hide a change.
        """
        doc = self._read_doc()
        salt = doc.get("salt")
        if not isinstance(salt, str) or not salt:
            salt = secrets.token_hex(16)  # first write, or a store from before digests
        new = {"facts": {k: f.to_dict() for k, f in sorted(facts.items())}, "salt": salt}
        self.journal.append({**event, "facts_digest": _facts_digest(new)})
        self.store.write_json(FACTS_FILE, new)

    # -- API ----------------------------------------------------------------

    def set_fact(self, key: str, value: Any, category: str, provenance: str, basis: str) -> Fact:
        """Store a NEW fact. Requires consent; refuses to overwrite (use ``correct``)."""
        check_money_fact(key, value)
        fact = Fact(key, value, category, provenance, basis, self._clock())
        self.consent.require(category)
        with self.store.lock(FACTS_LOCK):
            facts = self._load()
            if key in facts:
                raise FactExists(key)
            facts[key] = fact
            self._write(
                facts, {"event": "fact_set", "key": key, "category": category, "provenance": provenance}
            )
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
        with self.store.lock(FACTS_LOCK):
            facts = self._load()
            if key not in facts:
                raise KeyError(key)
            old = facts[key]
            self.consent.require(old.category)
            check_money_fact(key, value)
            new = Fact(key, value, old.category, provenance, basis, self._clock())
            facts[key] = new
            self._write(
                facts,
                {
                    "event": "fact_correct",
                    "key": key,
                    "category": old.category,
                    "old_provenance": old.provenance,
                    "new_provenance": provenance,
                },
            )
        return new

    def delete(self, category: str) -> list[str]:
        """Delete every fact in *category*. Journaled; works without consent."""
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        with self.store.lock(FACTS_LOCK):
            facts = self._load()
            removed = sorted(k for k, f in facts.items() if f.category == category)
            for k in removed:
                del facts[k]
            self._write(
                facts,
                {"event": "category_delete", "category": category, "keys": removed, "count": len(removed)},
            )
        return removed

    def export(self) -> dict[str, Any]:
        """Everything Life Mentor holds about the user.

        Export intentionally BYPASSES consent: it is the user's own right of
        access to their data, so it must work even after every consent is
        revoked. Each export is journaled as ``profile_export`` with the fact
        count and categories only (no keys' values). If a journal is broken, or
        facts.json does not match the profile journal, the export still returns
        what facts.json holds (``journal_ok: False``) but is not journaled -
        appending onto tampered history is refused.
        """
        consent_ok, _ = self.consent.journal.verify()
        profile_ok, _ = self.journal.verify()
        doc = self._read_doc()
        facts = {k: Fact.from_dict(v) for k, v in doc.get("facts", {}).items()}
        profile_ok = profile_ok and self._problem(doc) is None
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
