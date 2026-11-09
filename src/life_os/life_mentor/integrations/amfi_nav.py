"""AMFI NAVAll.txt parser and mutual-fund holdings valuation (LM10).

Parses a NAVAll.txt file the USER downloaded themselves (Life Mentor never fetches it).
The header must match one of ``AMFI_HEADERS`` exactly, otherwise BLOCK:

  * the header of https://www.amfiindia.com/spages/NAVAll.txt as fetched on 2026-10-11
    (8 columns; note the space in "Payout/ ISIN"; file sha256 28e7b126...b92e), and
  * the spelling given in the owner's brief (no space after "/") - SPEC-GAP LM-G51.

The older 6-column header (no Plan/Option) is not accepted: it could not be cited.
Data lines are ``code;isin1;isin2;name;plan;option;nav;dd-Mon-yyyy``; section and fund-house
lines are skipped. NAV "N.A." or any non-number -> no NAV (HOLD for that holding).

Valuation = units x NAV, rounded to the paisa. A NAV dated more than 5 business days
(Mon-Fri; exchange holidays not modelled) before ``as_of`` is stale -> HOLD.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from life_os.core.clock import utc_now_iso
from life_os.core.gate import BLOCK, HOLD, SHIP, Check

from ..finance.inputs import InputValue, input_checks, resolve
from ..finance.report import finalize, fixed_check
from ..profile import ConsentLedger
from ..profile.facts import encode_value
from ..security import PrivateStore, redact
from .bank_csv import IMPORT_JOURNAL

__all__ = [
    "AMFI_HEADERS",
    "AMFI_SOURCE",
    "NAV_STALE_BUSINESS_DAYS",
    "NavFormatError",
    "apply_nav_valuation",
    "business_days_between",
    "parse_navall",
    "value_holdings",
]

AMFI_HEADERS = (
    (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;"
        "Net Asset Value;Date"
    ),
    (
        "Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;"
        "Net Asset Value;Date"
    ),
)
AMFI_SOURCE = {
    "url": "https://www.amfiindia.com/spages/NAVAll.txt",
    "retrieved_on": "2026-10-11",
    "sha256": "28e7b12628b87e60eaf948024a81e02b3cc565f967d4f0543635f27081c1b92e",
    "note": "header format checked against this file; NAV values are read from YOUR file",
}
NAV_STALE_BUSINESS_DAYS = 5
MAX_FILE_BYTES = 20 * 1024 * 1024


class NavFormatError(ValueError):
    """The file is not in the AMFI NAVAll.txt format (maps to BLOCK)."""


def business_days_between(start: date, end: date) -> int:
    """Weekdays strictly after *start* up to and including *end*."""
    days, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        days += cur.weekday() < 5
    return days


def parse_navall(path: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """{scheme_code: {isin_growth, isin_reinvest, scheme_name, plan, option, nav, nav_date}}."""
    p = Path(path)
    if not p.is_file():
        raise NavFormatError("NAV file not found")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise NavFormatError("NAV file is unexpectedly large")
    try:
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise NavFormatError("NAV file is not UTF-8 text") from exc
    header = lines[0].strip() if lines else ""
    if header not in AMFI_HEADERS:
        raise NavFormatError(
            f"header does not match the AMFI NAVAll.txt format (expected: {AMFI_HEADERS[0]!r})"
        )
    out: dict[str, dict[str, Any]] = {}
    for line in lines[1:]:
        cols = line.strip().split(";")
        if len(cols) != 8 or not cols[0].strip().isdigit():
            continue
        code, isin_g, isin_r, name, plan, option, nav_text, date_text = (c.strip() for c in cols)
        nav: Decimal | None
        # Strict: Decimal() alone would also accept "1_0.5" (as 10.5) and "1e2".
        parsed = Decimal(nav_text) if _NAV.fullmatch(nav_text) else Decimal("NaN")
        nav = parsed if parsed.is_finite() and parsed > 0 else None
        try:
            nav_date: date | None = datetime.strptime(date_text, "%d-%b-%Y").date()  # date-only
        except ValueError:
            nav_date = None
        if code in out:
            # Two rows for one scheme code: we cannot know which NAV is right, so
            # neither is used (that holding HOLDs as unpriced) instead of last-wins.
            out[code]["nav"] = None
            continue
        out[code] = {
            "isin_growth": isin_g,
            "isin_reinvest": isin_r,
            "scheme_name": name,
            "plan": plan,
            "option": option,
            "nav": nav,
            "nav_date": nav_date,
        }
    return out


_NAV = re.compile(r"[0-9]+(?:\.[0-9]+)?")

_ASSUMPTIONS = (
    "NAVs are read from the NAVAll.txt file you downloaded; Life Mentor does not fetch them.",
    (
        f"A NAV older than {NAV_STALE_BUSINESS_DAYS} business days (Mon-Fri; holidays not "
        "modelled) before the as-of date is stale."
    ),
    (
        "Value = units x NAV, rounded to the paisa; exit loads, taxes and pending transactions are "
        "not included. Past NAVs say nothing about future value."
    ),
)


def value_holdings(
    nav_path: str | os.PathLike[str], holdings: Sequence[dict[str, Any]], *, as_of: date
) -> dict[str, Any]:
    """Value ``[{"scheme_code", "units"}]`` with dated NAVs from the user's file."""
    checks: list[Check] = []
    inputs: list[InputValue] = []
    sha = None
    try:
        navs = parse_navall(nav_path)
        sha = hashlib.sha256(Path(nav_path).read_bytes()).hexdigest()
        if not navs:
            checks.append(
                fixed_check(BLOCK, "nav_file", "nav_file: no scheme rows could be read from the file")
            )
    except NavFormatError as exc:
        checks.append(fixed_check(BLOCK, "nav_file", str(exc)))
        navs = {}
    if not holdings:
        checks.append(fixed_check(HOLD, "holdings", "no holdings given"))
    rows: list[dict[str, Any]] = []
    for i, h in enumerate(holdings):
        code = str(h.get("scheme_code", "")).strip()
        units = resolve(f"holding{i}.units", h.get("units"))
        inputs.append(units)
        checks += input_checks([units])
        if units.value is not None and units.value == 0:
            checks.append(fixed_check(BLOCK, f"holding{i}", "units must be above zero"))
        if not navs:
            continue
        nav = navs.get(code)
        if nav is None:
            checks.append(fixed_check(HOLD, f"holding{i}", f"scheme code {code} not in the file"))
            continue
        if nav["nav"] is None or nav["nav_date"] is None:
            checks.append(fixed_check(HOLD, f"holding{i}", f"scheme {code}: NAV not available"))
            continue
        age = business_days_between(nav["nav_date"], as_of)
        if nav["nav_date"] > as_of:
            checks.append(fixed_check(BLOCK, f"holding{i}", f"scheme {code}: NAV dated after as_of"))
        elif age > NAV_STALE_BUSINESS_DAYS:
            checks.append(
                fixed_check(
                    HOLD,
                    f"holding{i}",
                    f"scheme {code}: NAV of {nav['nav_date']} is stale ({age} business days)",
                )
            )
        if units.value is None or units.value <= 0:
            continue
        rows.append(
            {
                "scheme_code": code,
                "scheme_name": redact(nav["scheme_name"]),
                "plan": redact(nav["plan"]),
                "option": redact(nav["option"]),
                "units": units.value,
                "nav": nav["nav"],
                "nav_date": nav["nav_date"].isoformat(),
                "business_days_old": age,
                "value": (units.value * nav["nav"]).quantize(Decimal("0.01"), ROUND_HALF_UP),
            }
        )
    result = None
    if rows and not any(c.fn()[0] == BLOCK for c in checks):
        result = {
            "holdings": rows,
            "total_value": sum((r["value"] for r in rows), Decimal(0)),
            "as_of": as_of.isoformat(),
        }
        checks.append(fixed_check(SHIP, "valued", f"{len(rows)} holding(s) valued"))
    elif holdings and not any(c.fn()[0] != SHIP for c in checks):
        checks.append(fixed_check(HOLD, "valued", "no holding could be valued"))
    return finalize(
        "mf_valuation",
        inputs=inputs,
        assumptions=list(_ASSUMPTIONS),
        result=result,
        checks=checks,
        extra={"data_source": {"file_sha256": sha, "sha256": sha, "format_reference": AMFI_SOURCE}},
    )


def apply_nav_valuation(
    store: PrivateStore, report: dict[str, Any], *, clock: Callable[[], str] = utc_now_iso
) -> dict[str, Any]:
    """Store a SHIP valuation (finance consent); journal key-only. Others are not written."""
    ConsentLedger(store, clock=clock).require("finance")
    checks: list[Check] = []
    written = False
    if report.get("calculator") != "mf_valuation":
        raise ValueError("report must come from value_holdings")
    if report["gate"]["disposition"] != SHIP or not report.get("result"):
        checks.append(fixed_check(HOLD, "not_written", "only a valuation with every check passed is stored"))
    else:
        r = report["result"]
        sha = str(report["data_source"]["file_sha256"])
        rel = f"integrations/mf/{r['as_of']}_{sha[:12]}.json"
        if store.path(rel).exists():
            checks.append(
                fixed_check(
                    BLOCK,
                    "exists",
                    "a valuation from this NAV file for this date is already stored; it is never overwritten",
                )
            )
            return finalize(
                "mf_valuation_store", inputs=[], assumptions=[], result={"written": False}, checks=checks
            )
        # Journal first (verifies the chain; a tampered journal blocks the write).
        store.journal(IMPORT_JOURNAL, clock=clock).append(
            {
                "event": "amfi_nav_valuation",
                "as_of": r["as_of"],
                "file_sha256": report["data_source"]["file_sha256"],
                "schemes": len(r["holdings"]),
            }
        )
        store.write_json(
            rel,
            {
                "source": "amfi_navall",
                "as_of": r["as_of"],
                "file_sha256": report["data_source"]["file_sha256"],
                "holdings": encode_value(r["holdings"]),
                "total_value": encode_value(r["total_value"]),
            },
        )
        written = True
        checks.append(fixed_check(SHIP, "written", "valuation stored"))
    return finalize(
        "mf_valuation_store", inputs=[], assumptions=[], result={"written": written}, checks=checks
    )
