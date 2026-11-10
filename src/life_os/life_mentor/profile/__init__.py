"""Life Mentor profile & consent (LM01): typed facts with provenance, consent, gate."""

from __future__ import annotations

from .consent import CONSENT_JOURNAL, ConsentLedger, ConsentRequired
from .facts import (
    ASSUMPTION,
    CATEGORIES,
    PROVENANCES,
    UNKNOWN,
    USER_ESTIMATE,
    VERIFIED,
    Fact,
)
from .store import (
    FACTS_FILE,
    PROFILE_JOURNAL,
    FactExists,
    Profile,
    ProfileGateOutcome,
    Requirement,
    profile_gate,
)

__all__ = [
    "ASSUMPTION",
    "CATEGORIES",
    "CONSENT_JOURNAL",
    "FACTS_FILE",
    "PROFILE_JOURNAL",
    "PROVENANCES",
    "UNKNOWN",
    "USER_ESTIMATE",
    "VERIFIED",
    "ConsentLedger",
    "ConsentRequired",
    "Fact",
    "FactExists",
    "Profile",
    "ProfileGateOutcome",
    "Requirement",
    "profile_gate",
]
