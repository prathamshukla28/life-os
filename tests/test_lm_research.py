"""LM03 research/evidence: SourceRecord, dated RulePack loader, staleness gate."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.research import (
    RulePack,
    RulePackBlocked,
    SourceRecord,
    check_rule_pack_file,
    load_rule_pack,
    rule_status,
    rules_gate,
)

URL = "https://example.gov.in/notification.pdf"


def _value(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "std_deduction_new",
        "value": 75000,
        "unit": "INR",
        "effective_from": "2026-04-01",
        "effective_to": "2027-03-31",
        "source_url": URL,
        "source_tier": "primary",
        "retrieved_on": "2026-10-01",
        "verified_by_human": True,
        "refresh_cadence_days": 30,
    }
    base.update(over)
    return base


def _pack(*values: dict[str, Any]) -> dict[str, Any]:
    return {"pack_id": "test_pack", "title": "synthetic", "values": list(values or [_value()])}


# --- SourceRecord ----------------------------------------------------------


def test_source_record_valid_roundtrip() -> None:
    src = SourceRecord(
        url=URL,
        title="Notification",
        publisher="Synthetic Dept",
        published="2026-03-31",
        retrieved_on="2026-10-11",
        tier="primary",
        evidence_class="VERIFIED_FACT",
    )
    assert src.retrieved_on == date(2026, 10, 11)
    assert src.published == date(2026, 3, 31)
    assert SourceRecord.from_dict(src.to_dict()) == src


@pytest.mark.parametrize(
    "over",
    [
        {"url": ""},
        {"url": "ftp://x"},
        {"tier": "tertiary"},
        {"evidence_class": "RUMOUR"},
        {"retrieved_on": "yesterday"},
        {"title": ""},
    ],
)
def test_source_record_rejects_invalid(over: dict[str, Any]) -> None:
    kwargs: dict[str, Any] = {
        "url": URL,
        "title": "t",
        "publisher": "p",
        "published": None,
        "retrieved_on": "2026-10-11",
        "tier": "secondary",
        "evidence_class": "STRONG_EVIDENCE",
    }
    kwargs.update(over)
    with pytest.raises(ValueError):
        SourceRecord(**kwargs)


# --- loader ----------------------------------------------------------------


def test_load_valid_pack_from_dict_and_file(tmp_path: Path) -> None:
    pack = load_rule_pack(_pack())
    assert isinstance(pack, RulePack)
    rv = pack.get("std_deduction_new")
    assert rv.value == Decimal(75000)
    assert rv.effective_from == date(2026, 4, 1)
    path = tmp_path / "p.json"
    path.write_text(json.dumps(_pack()), encoding="utf-8")
    assert load_rule_pack(path) == pack


def test_loader_never_produces_float(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    value_json = json.dumps(_value(value={"rate": 0.04, "slabs": [[0, 0.05]]}))
    path.write_text(f'{{"pack_id":"p","title":"t","values":[{value_json}]}}', encoding="utf-8")
    rv = load_rule_pack(path).get("std_deduction_new")
    assert rv.value["rate"] == Decimal("0.04")
    assert isinstance(rv.value["slabs"][0][1], Decimal)


@pytest.mark.parametrize("missing", ["", "   ", None])
def test_missing_source_url_blocks_on_load(missing: Any) -> None:
    data = _pack(_value(source_url=missing))
    with pytest.raises(RulePackBlocked) as exc:
        load_rule_pack(data)
    assert any("source_url" in r for r in exc.value.reasons)


def test_absent_source_url_key_blocks() -> None:
    v = _value()
    del v["source_url"]
    with pytest.raises(RulePackBlocked):
        load_rule_pack(_pack(v))


@pytest.mark.parametrize(
    "over",
    [
        {"source_tier": "blog"},
        {"retrieved_on": "not-a-date"},
        {"effective_from": "2027-04-01", "effective_to": "2026-03-31"},
        {"refresh_cadence_days": 0},
        {"refresh_cadence_days": "30"},
        {"verified_by_human": "yes"},
        {"id": ""},
        {"source_url": "javascript:alert(1)"},
    ],
)
def test_invalid_fields_block(over: dict[str, Any]) -> None:
    with pytest.raises(RulePackBlocked):
        load_rule_pack(_pack(_value(**over)))


def test_duplicate_ids_block() -> None:
    with pytest.raises(RulePackBlocked):
        load_rule_pack(_pack(_value(), _value()))


def test_empty_pack_blocks() -> None:
    with pytest.raises(RulePackBlocked):
        load_rule_pack({"pack_id": "p", "title": "t", "values": []})


def test_unreadable_file_blocks(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(RulePackBlocked):
        load_rule_pack(bad)
    with pytest.raises(RulePackBlocked):
        load_rule_pack(tmp_path / "absent.json")


def test_get_unknown_id_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        load_rule_pack(_pack()).get("nope")


def test_effective_to_may_be_open_ended() -> None:
    pack = load_rule_pack(_pack(_value(effective_to=None)))
    assert pack.get("std_deduction_new").effective_to is None


# --- staleness / verification ----------------------------------------------


def test_fresh_verified_primary_ships() -> None:
    pack = load_rule_pack(_pack())
    [st] = rule_status(pack, as_of=date(2026, 10, 11))
    assert not st.stale and not st.unverified and st.in_effect
    assert rules_gate(pack, as_of=date(2026, 10, 11)).disposition == SHIP


def test_stale_value_reported_and_holds() -> None:
    pack = load_rule_pack(_pack(_value(retrieved_on="2026-01-01", refresh_cadence_days=30)))
    [st] = rule_status(pack, as_of=date(2026, 10, 11))
    assert st.stale is True
    assert "stale" in st.flags
    gate = rules_gate(pack, as_of=date(2026, 10, 11))
    assert gate.disposition == HOLD
    assert any("stale" in r["reason"] for r in gate.reasons)


def test_staleness_boundary_is_inclusive() -> None:
    pack = load_rule_pack(_pack(_value(retrieved_on="2026-10-01", refresh_cadence_days=10)))
    assert not rule_status(pack, as_of=date(2026, 10, 11))[0].stale
    assert rule_status(pack, as_of=date(2026, 10, 12))[0].stale


def test_secondary_tier_flagged_unverified() -> None:
    pack = load_rule_pack(_pack(_value(source_tier="secondary", verified_by_human=True)))
    [st] = rule_status(pack, as_of=date(2026, 10, 11))
    assert st.unverified and "unverified" in st.flags
    gate = rules_gate(pack, as_of=date(2026, 10, 11))
    assert gate.disposition == SHIP
    assert any("unverified" in r["reason"].casefold() for r in gate.reasons)
    strict = rules_gate(pack, as_of=date(2026, 10, 11), require_verified=True)
    assert strict.disposition == HOLD


def test_not_human_verified_flagged_unverified() -> None:
    pack = load_rule_pack(_pack(_value(verified_by_human=False)))
    assert rule_status(pack, as_of=date(2026, 10, 11))[0].unverified


def test_out_of_effect_holds() -> None:
    pack = load_rule_pack(_pack())
    [st] = rule_status(pack, as_of=date(2027, 4, 1))
    assert not st.in_effect and "not_in_effect" in st.flags
    assert rules_gate(pack, as_of=date(2027, 4, 1)).disposition == HOLD


def test_retrieved_in_future_blocks() -> None:
    pack = load_rule_pack(_pack(_value(retrieved_on="2026-12-01")))
    assert rules_gate(pack, as_of=date(2026, 10, 11)).disposition == BLOCK


def test_gate_restricted_to_ids_and_unknown_id_holds() -> None:
    pack = load_rule_pack(_pack(_value(), _value(id="old", retrieved_on="2020-01-01")))
    assert rules_gate(pack, as_of=date(2026, 10, 11), ids=["std_deduction_new"]).disposition == SHIP
    assert rules_gate(pack, as_of=date(2026, 10, 11)).disposition == HOLD
    assert rules_gate(pack, as_of=date(2026, 10, 11), ids=["missing"]).disposition == HOLD


def test_as_of_is_required_and_must_be_a_date() -> None:
    pack = load_rule_pack(_pack())
    with pytest.raises(TypeError):
        rule_status(pack, as_of="2026-10-11")  # type: ignore[arg-type]


def test_check_rule_pack_file_blocks_on_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_pack(_value(source_url=""))), encoding="utf-8")
    gate, pack = check_rule_pack_file(bad, as_of=date(2026, 10, 11))
    assert gate.disposition == BLOCK and pack is None
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_pack()), encoding="utf-8")
    gate, pack = check_rule_pack_file(good, as_of=date(2026, 10, 11))
    assert gate.disposition == SHIP and pack is not None
