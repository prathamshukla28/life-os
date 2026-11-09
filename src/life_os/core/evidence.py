# Vendored from the Asur monorepo (asur/script/evidence.py).
# Copyright (c) 2026 prathamshukla28. Licensed under the MIT License (see LICENSE).
"""Evidence classes - ASUR-HONEST-01.

Every claim ASUR makes about the world (what an audience wants, why a hook
works, how a platform ranks content) must be tagged with how much we actually
know. This is the honesty backbone: the system is never allowed to launder a
guess into a fact.

Four classes, from strongest to weakest:

    VERIFIED_FACT     Something a platform or primary source stated outright,
                      or a definition that is true by construction.
    STRONG_EVIDENCE   Well-supported by studies/data, but transfer to this
                      exact case is by analogy (e.g. ad-recall studies applied
                      to organic feed).
    EMPIRICAL         Observed from THIS creator's own SCRIPT memory / past
                      performance. True for them, not claimed as universal.
    HYPOTHESIS        Plausible, commonly believed, but not rigorously shown.
                      Creator folklore lives here until measured.

A claim is a small record: {text, evidence_class, basis}. ``basis`` says where
the confidence comes from in one short phrase, so the decision is explainable.
"""

from __future__ import annotations

__all__ = [
    "EMPIRICAL",
    "EVIDENCE_CLASSES",
    "HYPOTHESIS",
    "STRONG_EVIDENCE",
    "VERIFIED_FACT",
    "claim",
]

VERIFIED_FACT = "VERIFIED_FACT"
STRONG_EVIDENCE = "STRONG_EVIDENCE"
EMPIRICAL = "EMPIRICAL"
HYPOTHESIS = "HYPOTHESIS"

EVIDENCE_CLASSES = (VERIFIED_FACT, STRONG_EVIDENCE, EMPIRICAL, HYPOTHESIS)


def claim(text: str, evidence_class: str, basis: str = "") -> dict[str, str]:
    """Build one evidence-tagged claim. Rejects unknown classes (fail-closed)."""
    if evidence_class not in EVIDENCE_CLASSES:
        raise ValueError(f"unknown evidence class: {evidence_class!r}")
    return {"text": text, "evidence_class": evidence_class, "basis": basis}
