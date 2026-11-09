# Vendored from the Asur monorepo (asur/core/canonical.py).
# Copyright (c) 2026 prathamshukla28. Licensed under the MIT License (see LICENSE).
"""Canonical JSON + SHA-256 content hashing - the key-free integrity spine.

This is the ONE place that defines how ASUR turns a Python object into bytes
and those bytes into a content hash. Every checksum, every artifact_id, and
every firewall projection digest flows through here, so determinism matters
more than anything.

Canonical form ("asur.jcs-lf/v1"):
  - UTF-8 text
  - object keys sorted (lexicographically by code point)
  - no insignificant whitespace (compact separators)
  - non-ASCII preserved as real UTF-8 (ensure_ascii=False)
  - exactly one trailing newline (LF)

De-keying note: this is HASHING for byte-integrity and lineage, which we KEEP.
It is NOT signing for authorship, which we dropped. A SHA-256 here proves two
byte-strings are identical; it proves nothing about *who* produced them (that
is identity + gate's job).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "CANONICAL_PROFILE",
    "canonical_bytes",
    "canonical_sha256",
    "canonicalize",
    "parse_json",
]

CANONICAL_PROFILE = "asur.jcs-lf/v1"


def canonicalize(obj: Any) -> str:
    """Return the canonical JSON text for *obj* (with one trailing LF).

    Raises TypeError if the object is not JSON-serializable - we never want a
    silent lossy hash of something we could not represent exactly.
    """
    text = json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text + "\n"


def canonical_bytes(obj: Any) -> bytes:
    """Canonical UTF-8 bytes for *obj*."""
    return canonicalize(obj).encode("utf-8")


def canonical_sha256(obj: Any) -> str:
    """Hex SHA-256 over the canonical bytes of *obj*. Deterministic."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def parse_json(text: str) -> Any:
    """Parse JSON text back into Python objects. Thin wrapper for symmetry."""
    return json.loads(text)
