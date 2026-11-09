"""LM10 local-file integrations: bank/expense CSV import and AMFI NAVAll.txt valuation."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.integrations import (
    AMFI_HEADERS,
    NAV_STALE_BUSINESS_DAYS,
    apply_bank_csv,
    apply_nav_valuation,
    business_days_between,
    load_mapping,
    parse_navall,
    preview_bank_csv,
    value_holdings,
)
from life_os.life_mentor.profile import ConsentLedger, ConsentRequired
from life_os.life_mentor.security import PrivateStore, find_forbidden

D = Decimal
FIX = Path(__file__).resolve().parent / "fixtures"
CSV = FIX / "lm_bank_statement.csv"
MAPPING = FIX / "lm_bank_mapping.json"
NAV = FIX / "lm_navall_sample.txt"
AS_OF = date(2026, 10, 11)
NOW = "2026-10-11T00:00:00Z"


def _store(tmp_path: Path, *cats: str) -> PrivateStore:
    store = PrivateStore(tmp_path / "lm")
    ledger = ConsentLedger(store, clock=lambda: NOW)
    for c in cats:
        ledger.grant(c, purpose="import")
    return store


def _files_text(root: Path) -> str:
    return "".join(p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("*") if p.is_file())


# --- bank CSV ---------------------------------------------------------------------------


def test_preview_totals_and_rejects() -> None:
    rep = preview_bank_csv(CSV, load_mapping(MAPPING))
    assert find_forbidden(rep) == []
    assert rep["gate"]["disposition"] == HOLD
    r = rep["result"]
    assert r["months"] == {
        "2026-09": {
            "food": D("1250.50"),
            "groceries": D("2340.00"),
            "rent": D("25000.00"),
            "unmapped": D("5000"),
        },
        "2026-10": {"rent": D("25000.00")},
    }
    assert (r["rows_used"], r["credits_skipped"], r["unmapped_rows"]) == (5, 1, 1)
    assert r["rejected"] == [
        {"line": 7, "reason": "date does not match %d/%m/%Y"},
        {"line": 8, "reason": "amount is not a number"},
    ]
    blob = json.dumps(rep, default=str)
    for raw in ("SWIGGY", "LANDLORD", "4111111111111111", "123456789012", "ACME"):
        assert raw not in blob
    assert r["dry_run"] is True


def test_apply_requires_consent_and_partial_flag(tmp_path: Path) -> None:
    mapping = load_mapping(MAPPING)
    with pytest.raises(ConsentRequired):
        apply_bank_csv(_store(tmp_path), CSV, mapping, clock=lambda: NOW)
    store = _store(tmp_path, "finance")
    refused = apply_bank_csv(store, CSV, mapping, clock=lambda: NOW)
    assert refused["gate"]["disposition"] == HOLD and refused["result"]["written"] is False
    assert not (store.root / "integrations").exists()
    done = apply_bank_csv(store, CSV, mapping, allow_partial=True, clock=lambda: NOW)
    assert done["result"]["written"] is True
    j = store.journal("integrations/journal.jsonl")
    [entry] = j.entries()
    assert entry["event"]["event"] == "bank_csv_import"
    assert entry["event"]["months"] == ["2026-09", "2026-10"]
    assert j.verify() == (True, None)
    data = _files_text(store.root)
    for raw in ("SWIGGY", "LANDLORD", "4111111111111111", "ATM", "1,04,366.67"):
        assert raw not in data
    again = apply_bank_csv(store, CSV, mapping, allow_partial=True, clock=lambda: NOW)
    assert again["gate"]["disposition"] == BLOCK  # same file twice is refused


def test_clean_file_ships(tmp_path: Path) -> None:
    csv_path = tmp_path / "ok.csv"
    csv_path.write_text(
        "Txn Date,Narration,Withdrawal Amt,Deposit Amt\n01/09/2026,RENT,25000,\n02/09/2026,swiggy,100.25,\n",
        encoding="utf-8",
    )
    rep = preview_bank_csv(csv_path, load_mapping(MAPPING))
    assert rep["gate"]["disposition"] == SHIP
    assert rep["result"]["months"]["2026-09"] == {"rent": D(25000), "food": D("100.25")}


@pytest.mark.parametrize(
    "change",
    [
        {"debit_column": "Missing Column"},
        {"date_format": ""},
        {"category_rules": [{"contains": "", "category": "x"}]},
    ],
)
def test_bad_mapping_blocks(tmp_path: Path, change: dict) -> None:
    raw = json.loads(MAPPING.read_text(encoding="utf-8"))
    raw.update(change)
    path = tmp_path / "m.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert preview_bank_csv(CSV, load_mapping(path))["gate"]["disposition"] == BLOCK


def test_empty_or_missing_csv(tmp_path: Path) -> None:
    empty = tmp_path / "e.csv"
    empty.write_text("", encoding="utf-8")
    assert preview_bank_csv(empty, load_mapping(MAPPING))["gate"]["disposition"] == BLOCK
    assert preview_bank_csv(tmp_path / "nope.csv", load_mapping(MAPPING))["gate"]["disposition"] == BLOCK


# --- AMFI NAV ----------------------------------------------------------------------------------


def test_amfi_headers() -> None:
    assert (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;"
        "Option;Net Asset Value;Date"
    ) in AMFI_HEADERS
    assert NAV_STALE_BUSINESS_DAYS == 5


def test_parse_navall() -> None:
    navs = parse_navall(NAV)
    assert navs["900001"]["nav"] == D("123.4567")
    assert navs["900001"]["nav_date"] == date(2026, 10, 9)
    assert navs["900004"]["nav"] is None
    assert navs["900001"]["scheme_name"] == "SYNTHETIC EQUITY FUND"


@pytest.mark.parametrize(
    "header",
    [
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date",
        "scheme code;whatever",
        "",
    ],
)
def test_wrong_header_blocks(tmp_path: Path, header: str) -> None:
    path = tmp_path / "nav.txt"
    body = NAV.read_text(encoding="utf-8").split("\n", 1)[1]
    path.write_text(header + "\n" + body, encoding="utf-8")
    rep = value_holdings(path, [{"scheme_code": "900001", "units": D(1)}], as_of=AS_OF)
    assert rep["gate"]["disposition"] == BLOCK
    assert "header" in json.dumps(rep, default=str).casefold()


def test_brief_header_spelling_accepted(tmp_path: Path) -> None:
    path = tmp_path / "nav.txt"
    text = NAV.read_text(encoding="utf-8").replace("Payout/ ISIN", "Payout/ISIN", 1)
    path.write_text(text, encoding="utf-8")
    assert "900001" in parse_navall(path)


def test_valuation() -> None:
    rep = value_holdings(
        NAV,
        [{"scheme_code": "900001", "units": D("100.5")}, {"scheme_code": "900002", "units": D(10)}],
        as_of=AS_OF,
    )
    assert find_forbidden(rep) == []
    assert rep["gate"]["disposition"] == SHIP
    r = rep["result"]
    assert [h["value"] for h in r["holdings"]] == [D("12407.40"), D("184.80")]
    assert r["total_value"] == D("12592.20")
    assert r["holdings"][0]["nav_date"] == "2026-10-09"
    assert rep["data_source"]["sha256"]


@pytest.mark.parametrize(("code", "disp"), [("900003", HOLD), ("900004", HOLD), ("999999", HOLD)])
def test_valuation_holds(code: str, disp: str) -> None:
    rep = value_holdings(NAV, [{"scheme_code": code, "units": D(1)}], as_of=AS_OF)
    assert rep["gate"]["disposition"] == disp


def test_valuation_bad_units() -> None:
    assert (
        value_holdings(NAV, [{"scheme_code": "900001", "units": D(-1)}], as_of=AS_OF)["gate"]["disposition"]
        == BLOCK
    )
    with pytest.raises(TypeError):
        value_holdings(NAV, [{"scheme_code": "900001", "units": 1.5}], as_of=AS_OF)


def test_business_days() -> None:
    assert business_days_between(date(2026, 10, 9), date(2026, 10, 11)) == 0
    assert business_days_between(date(2026, 10, 9), date(2026, 10, 16)) == 5
    assert business_days_between(date(2026, 10, 9), date(2026, 10, 19)) == 6


def test_apply_nav_valuation(tmp_path: Path) -> None:
    rep = value_holdings(NAV, [{"scheme_code": "900001", "units": D("100.5")}], as_of=AS_OF)
    with pytest.raises(ConsentRequired):
        apply_nav_valuation(_store(tmp_path), rep, clock=lambda: NOW)
    store = _store(tmp_path, "finance")
    out = apply_nav_valuation(store, rep, clock=lambda: NOW)
    assert out["result"]["written"] is True
    [entry] = store.journal("integrations/journal.jsonl").entries()
    assert entry["event"]["event"] == "amfi_nav_valuation"
    assert "12407" not in json.dumps(entry)
    held = value_holdings(NAV, [{"scheme_code": "900003", "units": D(1)}], as_of=AS_OF)
    assert apply_nav_valuation(store, held, clock=lambda: NOW)["result"]["written"] is False
