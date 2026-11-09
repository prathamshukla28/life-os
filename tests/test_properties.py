"""Property-based tests (Hypothesis) for the pure, whole-domain parts of Life OS.

- iCalendar: folding never exceeds 75 octets and unfold+unescape recovers the text
- ISO weeks: containment, round-trips and adjacency across year boundaries / W53
- money: Indian-grouped formatting round-trips to the paise-rounded value
- git config guard: every key git itself reports is seen by our parser (differential)
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from life_os.life_mentor.finance.money import format_inr, quantize_paise
from life_os.life_mentor.ics import CalendarEvent, build_calendar
from life_os.weekly_tracker.sources.git_guard import parse_git_config
from life_os.weekly_tracker.week import IsoWeek, weeks_in_year

FIXED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _expected_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in s if c in "\t\n" or not (ord(c) < 0x20 or c == "\x7f"))


# --- iCalendar --------------------------------------------------------------------------------

text_st = st.text(alphabet=st.characters(blacklist_categories=["Cs"]), max_size=300)


@given(summary=text_st, description=text_st)
def test_ics_fold_and_escape_roundtrip(summary: str, description: str) -> None:
    ev = CalendarEvent("u@x", summary, date(2026, 1, 1), description=description or None)
    ics = build_calendar([ev], cal_name="c", dtstamp=FIXED)
    assert ics.endswith("\r\n")
    physical = ics.split("\r\n")[:-1]
    for line in physical:
        assert len(line.encode("utf-8")) <= 75
        assert "\r" not in line and "\n" not in line
    logical = ics.replace("\r\n ", "").split("\r\n")
    got = next(line for line in logical if line.startswith("SUMMARY:"))[len("SUMMARY:") :]
    assert _unescape(got) == _expected_text(summary)
    if description:
        got_d = next(line for line in logical if line.startswith("DESCRIPTION:"))[len("DESCRIPTION:") :]
        assert _unescape(got_d) == _expected_text(description)


@given(st.text(max_size=200))
def test_ics_escaped_text_has_no_bare_separators(s: str) -> None:
    ics = build_calendar([CalendarEvent("u@x", s, date(2026, 1, 1))], cal_name="c", dtstamp=FIXED)
    summary = next(line for line in ics.replace("\r\n ", "").split("\r\n") if line.startswith("SUMMARY:"))
    body = summary[len("SUMMARY:") :]
    # every ; and , must be escaped (preceded by an odd number of backslashes)
    for m in re.finditer(r"[;,]", body):
        run = len(body[: m.start()]) - len(body[: m.start()].rstrip("\\"))
        assert run % 2 == 1


# --- ISO weeks ----------------------------------------------------------------------------------

dates_st = st.dates(min_value=date(1, 1, 8), max_value=date(9999, 12, 20))


@given(dates_st)
def test_isoweek_contains_its_day_and_roundtrips(d: date) -> None:
    w = IsoWeek.containing(d)
    assert w.contains(d)
    assert IsoWeek.parse(str(w)) == w
    days = w.days()
    assert days[0].isoweekday() == 1 and days[-1].isoweekday() == 7
    assert all(b - a == timedelta(days=1) for a, b in zip(days, days[1:], strict=False))
    assert 1 <= w.week <= weeks_in_year(w.year)


@given(dates_st)
def test_isoweek_next_previous_are_adjacent(d: date) -> None:
    w = IsoWeek.containing(d)
    assume(date(1, 1, 15) < w.start < date(9999, 12, 15))
    assert w.next().previous() == w
    assert w.next().start - w.end == timedelta(days=1)
    assert w < w.next()


@given(st.integers(min_value=1, max_value=9998))
def test_week_53_exists_exactly_when_dec_28_says_so(year: int) -> None:
    n = weeks_in_year(year)
    assert n in (52, 53)
    IsoWeek(year, n)  # last week is valid
    with pytest.raises(ValueError):
        IsoWeek(year, n + 1)
    # Jan 4 is always in week 1; Dec 28 is always in the last week.
    assert IsoWeek.containing(date(year, 1, 4)) == IsoWeek(year, 1)
    assert IsoWeek.containing(date(year, 12, 28)) == IsoWeek(year, n)


# --- money ----------------------------------------------------------------------------------------

money_st = st.decimals(min_value=Decimal("-1e12"), max_value=Decimal("1e12"), allow_nan=False, places=4)


def _parse_inr(text: str) -> Decimal:
    neg = text.startswith("-")
    body = text.removeprefix("-").removeprefix("Rs ")
    value = Decimal(body.replace(",", ""))
    return -value if neg else value


@given(money_st)
def test_format_inr_roundtrips_to_paise(amount: Decimal) -> None:
    text = format_inr(amount)
    assert _parse_inr(text) == quantize_paise(amount)
    whole = text.removeprefix("-").removeprefix("Rs ").split(".")[0]
    groups = whole.split(",")
    # Indian grouping: last group 3 digits, earlier groups 2 (first may be 1-2).
    if len(groups) > 1:
        assert len(groups[-1]) == 3
        assert all(len(g) == 2 for g in groups[1:-1]) and 1 <= len(groups[0]) <= 2


@given(money_st)
def test_quantize_paise_is_half_up_and_idempotent(amount: Decimal) -> None:
    q = quantize_paise(amount)
    assert q == quantize_paise(q)
    assert abs(q - amount) <= Decimal("0.005")
    assert q == amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- git config guard: differential against git itself --------------------------------------------

_keys = st.sampled_from(["name", "fsmonitor", "pager", "editor", "x", "hookspath", "helper"])
_sections = st.sampled_from(["[core]", "[user]", '[diff "a"]', "[alias]", "[credential]", "[Core]"])
_values = st.lists(st.sampled_from(["a", " ", ";", "#", '"', "\\", "b c", "\\\\", '\\"']), max_size=6).map(
    "".join
)


@st.composite
def _configs(draw: st.DrawFn) -> str:
    lines = []
    for _ in range(draw(st.integers(1, 5))):
        lines.append(draw(_sections))
        for _ in range(draw(st.integers(0, 3))):
            lines.append(f"\t{draw(_keys)} = {draw(_values)}")
    return "\n".join(lines) + "\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(text=_configs())
def test_git_guard_sees_every_key_git_sees(tmp_path: Path, text: str) -> None:
    cfg = tmp_path / "config"
    cfg.write_text(text, encoding="utf-8")
    out = subprocess.run(
        ["git", "config", "--file", str(cfg), "--list", "--name-only"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path)},
    )
    try:
        ours = {f"{s}.{sub + '.' if sub else ''}{k}".lower() for s, sub, k in parse_git_config(text)}
    except ValueError:
        return  # we fail closed (refuse) - always safe
    if out.returncode != 0:
        return  # git refuses the file too, so it cannot act on it
    theirs = {line.strip().lower() for line in out.stdout.splitlines() if line.strip()}
    assert theirs <= ours, (text, theirs - ours)
