"""Bank / expense CSV import (LM10): local file -> monthly expense totals by user category.

Opt-in and local only. The user supplies the CSV (exported from their bank) and a JSON
column mapping:

    {"date_column": "...", "date_format": "%d/%m/%Y", "description_column": "...",
     "debit_column": "..."  (or "amount_column": "..." + "debit_is_negative": true),
     "credit_column": "..." (optional), "category_rules": [{"contains": "rent",
     "category": "rent"}, ...]}

Only debits are expenses. Descriptions are used ONLY to match category rules (case
insensitive substring) and are never stored or echoed; unmatched debits go to
"unmapped" (reported, never guessed). Rows whose date or amount cannot be read are
listed by line number with a reason - no values. Preview (dry run) is the default;
``apply_bank_csv`` writes month/category totals only with finance consent, refuses a
file already imported (same sha256), and refuses partial files unless
``allow_partial``. The journal entry is key-only (file hash, months, categories, counts).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from life_os.core.clock import utc_now_iso
from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.report import finalize, fixed_check
from ..profile import ConsentLedger
from ..profile.facts import MAX_AMOUNT, encode_value
from ..security import PrivateStore

__all__ = [
    "IMPORT_JOURNAL",
    "MAX_FILE_BYTES",
    "MAX_ROWS",
    "UNMAPPED",
    "apply_bank_csv",
    "load_mapping",
    "preview_bank_csv",
]

IMPORT_JOURNAL = "integrations/journal.jsonl"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 100_000
UNMAPPED = "unmapped"
_STRIP = ("₹", "Rs.", "Rs", "INR", " ")
# Comma grouping must be Western (1,234,567.89) or Indian (12,34,567.89); "1.234,50"
# (comma as decimal mark) is rejected rather than silently read as 1.23450.
# The LAST group must be exactly 3 digits, so a decimal comma such as "12,50" is
# rejected instead of being read as 1250.
_AMOUNT = re.compile(r"-?\d{1,3}(?:(?:,\d{2})*,\d{3}|(?:,\d{3})*)(\.\d+)?|-?\d+(\.\d+)?")


def load_mapping(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read the JSON mapping (structure is validated during preview)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("mapping must be a JSON object")
    return data


def _amount(text: str) -> Decimal | None:
    cleaned = text.strip()
    for token in _STRIP:
        cleaned = cleaned.replace(token, "")
    if not cleaned:
        return None
    if not _AMOUNT.fullmatch(cleaned):
        raise ValueError("amount is not a number")
    try:
        value = Decimal(cleaned.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("amount is not a number") from exc
    if not value.is_finite():
        raise ValueError("amount is not a number")
    if abs(value) > MAX_AMOUNT:
        raise ValueError("amount is out of range (limit 10^15)")
    return value


def _mapping_problems(mapping: dict[str, Any], header: list[str]) -> list[str]:
    problems: list[str] = []
    needed = ["date_column", "description_column"]
    if "debit_column" in mapping:
        needed.append("debit_column")
    elif "amount_column" in mapping:
        needed.append("amount_column")
    else:
        problems.append("mapping needs debit_column or amount_column")
    if "credit_column" in mapping:
        needed.append("credit_column")
    for key in needed:
        col = mapping.get(key)
        if not isinstance(col, str) or col not in header:
            problems.append(f"{key} {col!r} is not a column of the CSV header")
    if "debit_is_negative" in mapping and not isinstance(mapping["debit_is_negative"], bool):
        problems.append("debit_is_negative must be true or false (a JSON boolean, not text)")
    fmt = mapping.get("date_format")
    if not isinstance(fmt, str) or "%" not in fmt:
        problems.append("date_format must be a strftime pattern such as %d/%m/%Y")
    rules = mapping.get("category_rules", [])
    if not isinstance(rules, list):
        problems.append("category_rules must be a list")
    else:
        for i, rule in enumerate(rules):
            if (
                not isinstance(rule, dict)
                or not str(rule.get("contains", "")).strip()
                or not str(rule.get("category", "")).strip()
            ):
                problems.append(f"category_rules[{i}] needs non-empty 'contains' and 'category'")
    return problems


def _read(path: Path) -> tuple[bytes | None, str | None]:
    if not path.is_file():
        return None, "CSV file not found"
    if path.stat().st_size > MAX_FILE_BYTES:
        return None, f"CSV file is larger than {MAX_FILE_BYTES} bytes"
    return path.read_bytes(), None


def _parse(raw: bytes, mapping: dict[str, Any], checks: list[Check]) -> dict[str, Any] | None:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        checks.append(fixed_check(BLOCK, "encoding", "CSV is not UTF-8 text"))
        return None
    reader = csv.reader(io.StringIO(text))
    try:
        rows = list(reader)
    except csv.Error:
        checks.append(
            fixed_check(BLOCK, "csv", "CSV could not be parsed (malformed quoting or an oversized field)")
        )
        return None
    if not rows or not any(c.strip() for c in rows[0]):
        checks.append(fixed_check(BLOCK, "header", "CSV is empty or has no header row"))
        return None
    if len(rows) - 1 > MAX_ROWS:
        checks.append(fixed_check(BLOCK, "rows", f"more than {MAX_ROWS} rows"))
        return None
    header = [c.strip() for c in rows[0]]
    problems = _mapping_problems(mapping, header)
    if problems:
        for p in problems:
            checks.append(fixed_check(BLOCK, "mapping", p))
        return None
    idx = {name: header.index(name) for name in header}
    rules = [
        (str(r["contains"]).casefold(), str(r["category"]).strip()) for r in mapping.get("category_rules", [])
    ]
    fmt = mapping["date_format"]
    months: dict[str, dict[str, Decimal]] = {}
    rejected: list[dict[str, Any]] = []
    used = credits = unmapped = 0
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        if any(c.strip() for c in row[len(header) :]):
            # Typically an unquoted "25,000.00" split into two cells; reading only the
            # first piece would silently record 25.
            rejected.append({"line": line_no, "reason": "more cells than header columns (unquoted comma?)"})
            continue

        def cell(key: str, r: list[str] = row) -> str:
            col = idx[mapping[key]]
            return r[col] if col < len(r) else ""

        try:
            day = datetime.strptime(cell("date_column").strip(), fmt).date()  # date-only, tz N/A
        except ValueError:
            rejected.append({"line": line_no, "reason": f"date does not match {fmt}"})
            continue
        try:
            if "debit_column" in mapping:
                debit = _amount(cell("debit_column"))
                credit = _amount(cell("credit_column")) if "credit_column" in mapping else None
            else:
                signed = _amount(cell("amount_column"))
                negative_is_debit = mapping.get("debit_is_negative", True) is True
                debit = None
                if signed is not None and (signed < 0) == negative_is_debit and signed != 0:
                    debit = abs(signed)
                credit = None if debit is not None else signed
        except ValueError as exc:
            rejected.append({"line": line_no, "reason": str(exc)})
            continue
        if debit is None or debit == 0:
            if credit is not None:
                credits += 1
            else:
                rejected.append({"line": line_no, "reason": "no amount"})
            continue
        if debit < 0:
            rejected.append({"line": line_no, "reason": "negative debit amount"})
            continue
        desc = cell("description_column").casefold()
        category = next((cat for needle, cat in rules if needle in desc), UNMAPPED)
        unmapped += category == UNMAPPED
        month = f"{day.year:04d}-{day.month:02d}"
        bucket = months.setdefault(month, {})
        bucket[category] = bucket.get(category, Decimal(0)) + debit
        used += 1
    return {
        "months": {m: dict(sorted(c.items())) for m, c in sorted(months.items())},
        "rows_used": used,
        "credits_skipped": credits,
        "unmapped_rows": unmapped,
        "rejected": rejected,
    }


def _preview(path: Path, mapping: dict[str, Any]) -> tuple[dict[str, Any] | None, list[Check], str | None]:
    checks: list[Check] = []
    raw, err = _read(path)
    if raw is None:
        checks.append(fixed_check(BLOCK, "file", err or "unreadable file"))
        return None, checks, None
    parsed = _parse(raw, mapping, checks)
    digest = hashlib.sha256(raw).hexdigest()
    if parsed is not None:
        if parsed["rejected"]:
            checks.append(
                fixed_check(
                    HOLD,
                    "rejected_rows",
                    f"{len(parsed['rejected'])} row(s) could not be read; "
                    "fix them, or import with --allow-partial to skip them",
                )
            )
        if parsed["unmapped_rows"]:
            checks.append(
                fixed_check(
                    HOLD,
                    "unmapped",
                    f"{parsed['unmapped_rows']} debit(s) match no category rule; "
                    f"add rules, or import with --allow-partial to keep them as '{UNMAPPED}'",
                )
            )
        if not parsed["rows_used"]:
            checks.append(fixed_check(HOLD, "rows_used", "no expense rows found"))
        checks.append(fixed_check(SHIP, "parsed", f"{parsed['rows_used']} expense rows read"))
    return parsed, checks, digest


_ASSUMPTIONS = (
    (
        "Only debits count as expenses; credits are skipped. Descriptions are used only to match "
        "your category rules and are not stored."
    ),
    "Unmatched debits are grouped as 'unmapped' - Life Mentor does not guess categories.",
    "Amounts are read exactly as written in the file (currency symbols and commas removed).",
)


def preview_bank_csv(csv_path: str | os.PathLike[str], mapping: dict[str, Any]) -> dict[str, Any]:
    """Dry run: monthly totals by category, rejected rows, nothing written."""
    parsed, checks, digest = _preview(Path(csv_path), mapping)
    result = None if parsed is None else {**parsed, "dry_run": True, "file_sha256": digest}
    return finalize(
        "bank_csv_import", inputs=[], assumptions=list(_ASSUMPTIONS), result=result, checks=checks
    )


def apply_bank_csv(
    store: PrivateStore,
    csv_path: str | os.PathLike[str],
    mapping: dict[str, Any],
    *,
    allow_partial: bool = False,
    clock: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    """Write month/category totals (finance consent required). Never writes raw rows."""
    ConsentLedger(store, clock=clock).require("finance")
    parsed, checks, digest = _preview(Path(csv_path), mapping)
    journal = store.journal(IMPORT_JOURNAL, clock=clock)
    written = False
    if parsed is not None and digest is not None:
        already = any(e["event"].get("file_sha256") == digest for e in journal.entries())
        worst = max((c.fn()[0] for c in checks), key=(SHIP, HOLD, BLOCK).index)
        if already:
            checks.append(fixed_check(BLOCK, "duplicate", "this file was already imported"))
        elif worst == BLOCK or (worst == HOLD and not allow_partial):
            checks.append(
                fixed_check(
                    HOLD, "not_written", "nothing written; resolve the items above or pass --allow-partial"
                )
            )
        elif parsed["rows_used"]:
            checks = [c for c in checks if c.fn()[0] != HOLD] if allow_partial else checks
            if allow_partial and (parsed["rejected"] or parsed["unmapped_rows"]):
                checks.append(
                    fixed_check(SHIP, "partial", "imported with rejected or unmapped rows accepted by you")
                )
            # Journal first (verifies the chain; a tampered journal blocks the write).
            journal.append(
                {
                    "event": "bank_csv_import",
                    "file_sha256": digest,
                    "months": sorted(parsed["months"]),
                    "categories": sorted({c for m in parsed["months"].values() for c in m}),
                    "rows_used": parsed["rows_used"],
                    "rows_rejected": len(parsed["rejected"]),
                }
            )
            store.write_json(
                f"integrations/expenses/{digest[:16]}.json",
                {
                    "source": "bank_csv",
                    "file_sha256": digest,
                    "months": encode_value(parsed["months"]),
                    "rows_used": parsed["rows_used"],
                    "rejected_rows": len(parsed["rejected"]),
                    "unmapped_rows": parsed["unmapped_rows"],
                },
            )
            written = True
    result = (
        None if parsed is None else {**parsed, "dry_run": False, "written": written, "file_sha256": digest}
    )
    return finalize(
        "bank_csv_import", inputs=[], assumptions=list(_ASSUMPTIONS), result=result, checks=checks
    )
