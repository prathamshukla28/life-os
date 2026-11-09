"""Life Mentor - a local-first, stdlib-only personal guidance module beside ASUR.

Life Mentor helps one person reason about their own finances, health habits,
career, and lifestyle WITHOUT pretending to know things it does not:

  - every personal fact carries provenance (VERIFIED | USER_ESTIMATE |
    ASSUMPTION | UNKNOWN) and is stored only after per-category consent;
  - every rule value (tax slab, rate) carries a dated, cited source and is
    surfaced as stale/unverified instead of silently used;
  - every decision is a per-criterion table plus a fail-closed gate
    disposition (SHIP / HOLD / BLOCK from ``life_os.core.gate``) - never one score;
  - personal data lives only under ``~/.life_mentor`` (or ``LIFE_MENTOR_HOME``)
    with 0700 dirs / 0600 files, and consequential changes are journaled in a
    hash-chained append-only log.

No network, no third-party packages, no transactions, no diagnosis. See
``life_mentor/docs/CHARTER.md``.
"""

from __future__ import annotations

__all__ = ["DISPLAY_NAME", "__version__"]

__version__ = "0.1.0"
DISPLAY_NAME = "Life Mentor"
