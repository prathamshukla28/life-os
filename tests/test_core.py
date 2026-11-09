"""Vendored core primitives: gate fail-closed semantics, canonical hashing, evidence, clock."""

from __future__ import annotations

import re

import pytest

from life_os.core.canonical import canonical_sha256, canonicalize, parse_json
from life_os.core.clock import utc_now_iso
from life_os.core.evidence import EVIDENCE_CLASSES, claim
from life_os.core.gate import BLOCK, HOLD, SHIP, Check, run_gate
from life_os.core.identity import Identity


def test_empty_gate_holds_with_dict_reason() -> None:
    # Regression: the NO_CHECKS reason used to be a bare string while every other
    # reason is a dict; renderers that read r["check"] would raise TypeError.
    g = run_gate("g", [])
    assert g.disposition == HOLD and g.substate == "NO_CHECKS"
    assert all(isinstance(r, dict) and {"check", "disposition", "reason"} <= set(r) for r in g.reasons)


def test_gate_takes_worst_and_fails_closed() -> None:
    def boom() -> tuple[str, str]:
        raise RuntimeError("x")

    assert run_gate("g", [Check("a", lambda: (SHIP, "ok"))]).passed
    assert run_gate("g", [Check("a", lambda: (SHIP, "")), Check("b", lambda: (HOLD, ""))]).disposition == HOLD
    assert run_gate("g", [Check("a", boom)]).disposition == BLOCK
    assert run_gate("g", [Check("a", lambda: ("MAYBE", ""))]).disposition == BLOCK


def test_canonical_is_deterministic_and_strict() -> None:
    assert canonicalize({"b": 1, "a": "é"}) == '{"a":"é","b":1}\n'
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})
    assert parse_json(canonicalize([1, "x"])) == [1, "x"]
    with pytest.raises(ValueError):
        canonicalize(float("nan"))


def test_evidence_and_identity_and_clock() -> None:
    assert claim("t", EVIDENCE_CLASSES[0])["evidence_class"] == EVIDENCE_CLASSES[0]
    with pytest.raises(ValueError):
        claim("t", "VIBES")
    assert Identity("os", "", "").principal() == "os"
    assert Identity("os", "Name", "a@example.com").principal() == "a@example.com"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", utc_now_iso())
