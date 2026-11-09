"""Weekly Tracker - a local, stdlib-only weekly summary ("trackwe") beside ASUR and Life Mentor.

One Markdown file per ISO week (e.g. ``2026-W41.md``): a Mon-Sun grid plus a short factual
summary built from three read-only/opt-in sources - the manual planner (append-only log),
the project's git history, and Life Mentor data (consent respected). No network, no score.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
