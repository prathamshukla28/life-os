"""Per-category consent ledger (LM01).

Consent is not a flag in memory: it is the replay of a hash-chained journal
(``consent.jsonl`` in the private store). The current state of a category is
the most recent ``consent_grant`` / ``consent_revoke`` event for it; a category
with no events has NOT been consented (fail closed). A tampered journal raises
``JournalTampered`` on read, so consent can never be inferred from a broken log.
"""

from __future__ import annotations

from collections.abc import Callable

from life_os.core.clock import utc_now_iso

from ..security import Journal, PrivateStore
from .facts import CATEGORIES

__all__ = ["CONSENT_JOURNAL", "ConsentLedger", "ConsentRequired"]

CONSENT_JOURNAL = "consent.jsonl"
_GRANT = "consent_grant"
_REVOKE = "consent_revoke"


class ConsentRequired(PermissionError):
    """Raised when a fact is stored/used in a category without current consent."""

    def __init__(self, category: str) -> None:
        super().__init__(
            f"consent for category {category!r} has not been granted; "
            f"ask the user and call ConsentLedger.grant({category!r}, purpose=...)"
        )
        self.category = category


def _check_category(category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"unknown consent category {category!r}; expected one of {CATEGORIES}")


class ConsentLedger:
    """Grant/revoke consent per category; state derived from the journal."""

    def __init__(self, store: PrivateStore, *, clock: Callable[[], str] = utc_now_iso) -> None:
        self.store = store
        self.journal: Journal = store.journal(CONSENT_JOURNAL, clock=clock)

    def grant(self, category: str, *, purpose: str) -> None:
        _check_category(category)
        if not purpose or not purpose.strip():
            raise ValueError("consent requires a stated purpose")
        self.journal.append({"event": _GRANT, "category": category, "purpose": purpose})

    def revoke(self, category: str, *, reason: str = "") -> None:
        _check_category(category)
        self.journal.append({"event": _REVOKE, "category": category, "reason": reason})

    def status(self) -> dict[str, bool]:
        state = {cat: False for cat in CATEGORIES}
        for entry in self.journal.entries():
            event = entry["event"]
            if event.get("category") in state and event.get("event") in (_GRANT, _REVOKE):
                state[event["category"]] = event["event"] == _GRANT
        return state

    def has(self, category: str) -> bool:
        _check_category(category)
        return self.status()[category]

    def require(self, category: str) -> None:
        if not self.has(category):
            raise ConsentRequired(category)
