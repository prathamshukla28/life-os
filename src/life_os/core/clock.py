# Vendored from the Asur monorepo (asur/core/envelope.py: ``utc_now_iso`` only).
# Copyright (c) 2026 prathamshukla28. Licensed under the MIT License (see LICENSE).
"""UTC timestamp helper shared by every Life OS store and journal."""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utc_now_iso"]


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with a trailing ``Z``, seconds precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
