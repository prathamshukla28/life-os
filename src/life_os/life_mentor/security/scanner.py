"""Forbidden-field scanner for Life Mentor reports (LM02, LM-HONEST).

Modelled on ``asur.tools.projection.conformance_scan`` but tuned for reports:

  - **Keys** are checked by underscore-delimited TOKEN, so ``score`` and
    ``overall_score`` are caught while ``underscored`` is not. Forbidden key
    tokens are fake single numbers (``score``) and raw identifiers that must
    never appear in a report (``account_number``, ``aadhaar``, ...).
  - **String values** are checked for promise language (``guaranteed``,
    ``risk-free``, ``assured return``) and for raw PII (anything
    ``redact()`` would change).

``find_forbidden`` returns human-readable findings; ``assert_report_clean``
raises ``ForbiddenFieldLeak`` (fail closed) if there are any.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from .redact import redact

__all__ = [
    "REPORT_FORBIDDEN_KEYS",
    "REPORT_FORBIDDEN_PHRASES",
    "ForbiddenFieldLeak",
    "assert_report_clean",
    "find_forbidden",
]

# Matched against the "_"-normalised key as whole token sequences.
REPORT_FORBIDDEN_KEYS = frozenset(
    {
        "score",
        "guaranteed",
        "guarantee",
        "aadhaar",
        "pan_number",
        "account_number",
        "card_number",
        "cvv",
        "password",
        "otp",
    }
)

REPORT_FORBIDDEN_PHRASES = (
    re.compile(r"\bguaranteed\b", re.IGNORECASE),
    re.compile(r"\brisk[\s-]?free\b", re.IGNORECASE),
    re.compile(r"\bassured\s+returns?\b", re.IGNORECASE),
)


class ForbiddenFieldLeak(ValueError):
    """A report carries a forbidden field, promise, or raw PII. Maps to BLOCK."""


def _norm_key(key: str) -> str:
    return "_" + re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_") + "_"


def _walk(obj: Any, path: str) -> Iterator[tuple[str, str, str]]:
    """Yield (kind, path, text) for every key and string value."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            skey = str(key)
            child = f"{path}.{skey}" if path else skey
            yield "key", child, skey
            yield from _walk(value, child)
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            yield from _walk(item, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield "value", path, obj


def find_forbidden(report: Any) -> list[str]:
    """Return every finding (empty list = clean)."""
    findings: list[str] = []
    for kind, path, text in _walk(report, ""):
        if kind == "key":
            norm = _norm_key(text)
            for bad in sorted(REPORT_FORBIDDEN_KEYS):
                if f"_{bad}_" in norm:
                    findings.append(f"forbidden key {bad!r} at {path or '<root>'}")
            if redact(text) != text:
                findings.append(f"raw PII in key at {path or '<root>'}")
        else:
            for pattern in REPORT_FORBIDDEN_PHRASES:
                if pattern.search(text):
                    findings.append(f"forbidden phrase {pattern.pattern!r} at {path or '<root>'}")
            if redact(text) != text:
                findings.append(f"raw PII in value at {path or '<root>'}")
    return findings


def assert_report_clean(report: Any) -> None:
    """Raise ``ForbiddenFieldLeak`` if ``find_forbidden`` reports anything."""
    findings = find_forbidden(report)
    if findings:
        raise ForbiddenFieldLeak("; ".join(findings))
