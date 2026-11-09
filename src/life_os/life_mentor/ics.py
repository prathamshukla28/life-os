"""A small, stdlib-only iCalendar (RFC 5545) writer - local export, never sync.

Used by the Weekly Tracker planner export and the Life Mentor habit schedule. It
*writes a file you import yourself* into whatever calendar you use; it makes no
network call, signs in to nothing, and links no account. It is the honest
opposite of "calendar sync": your data stays local and you stay in control of
what, if anything, you import.

Scope is deliberately the subset both callers need: all-day events (`VALUE=DATE`),
an optional exclusive end date for multi-day spans, an optional `RRULE` for
recurring items (daily habits), a summary and a description. Text is escaped and
content lines are folded at 75 octets on UTF-8 character boundaries (RFC 5545
s3.1), with CRLF line endings. Output is deterministic given a fixed ``dtstamp``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone

__all__ = ["CalendarEvent", "build_calendar", "make_uid"]

_PRODID = "-//life-os//Life Mentor + Weekly Tracker//EN"
_UID_DOMAIN = "life-os.local"
_MAX_OCTETS = 75


@dataclass(frozen=True)
class CalendarEvent:
    """One all-day calendar event.

    ``start`` is the first day; ``end`` is the EXCLUSIVE last day (RFC 5545 all-day
    DTEND is exclusive) - leave it ``None`` for a single-day event. ``rrule`` is a
    bare recurrence rule body such as ``"FREQ=DAILY"`` (no ``RRULE:`` prefix).
    """

    uid: str
    summary: str
    start: date
    end: date | None = None
    description: str | None = None
    rrule: str | None = None

    def __post_init__(self) -> None:
        if self.end is not None and self.end <= self.start:
            raise ValueError("all-day DTEND is exclusive and must be after DTSTART")
        for name, value in (("uid", self.uid), ("rrule", self.rrule or "")):
            if any(ord(c) < 0x20 or c == "\x7f" for c in value):
                raise ValueError(f"{name} must not contain control characters")


def make_uid(*parts: str) -> str:
    """A stable, content-derived UID (so re-exporting the same item is idempotent).

    Deterministic: the same parts always produce the same UID, so importing an
    updated file updates the same calendar entry instead of duplicating it.
    """
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{digest}@{_UID_DOMAIN}"


def _escape(text: str) -> str:
    """RFC 5545 s3.3.11 TEXT: escape ``\\ ; ,`` and newlines; drop other controls.

    Every newline form (CRLF, lone CR, LF) becomes the two characters ``\\n``; a raw
    CR or other control character would otherwise break the content-line structure.
    HTAB is allowed in TEXT and kept.
    """
    text = (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )
    return "".join(c for c in text if c == "\t" or not (ord(c) < 0x20 or c == "\x7f"))


def _fold(line: str) -> str:
    """Fold a content line to at most 75 octets per physical line (RFC 5545 s3.1).

    Continuation lines start with one space (which counts toward their 75). Splits
    happen only between characters, never inside a multi-byte UTF-8 sequence, so no
    character is ever dropped or corrupted.
    """
    if len(line.encode("utf-8")) <= _MAX_OCTETS:
        return line
    parts: list[str] = []
    current: list[str] = []
    size = 0
    limit = _MAX_OCTETS
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            parts.append("".join(current))
            current, size, limit = [], 0, _MAX_OCTETS - 1
        current.append(ch)
        size += n
    parts.append("".join(current))
    return "\r\n ".join(parts)


def _date(day: date) -> str:
    # Not strftime: glibc does not zero-pad %Y below year 1000; RFC 5545 needs 4 digits.
    return f"{day.year:04d}{day.month:02d}{day.day:02d}"


def build_calendar(
    events: list[CalendarEvent],
    *,
    cal_name: str,
    dtstamp: datetime | None = None,
) -> str:
    """Render *events* as a single RFC 5545 VCALENDAR string (CRLF line endings).

    ``dtstamp`` stamps every VEVENT's DTSTAMP; pass a fixed value for a
    reproducible file (the default is the current UTC time). No network, no files
    written here - the caller decides where (if anywhere) the text goes.
    """
    stamp = (dtstamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp_text = f"{_date(stamp)}T{stamp.hour:02d}{stamp.minute:02d}{stamp.second:02d}Z"

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(cal_name)}",
    ]
    for ev in events:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{ev.uid}")
        lines.append(f"DTSTAMP:{stamp_text}")
        lines.append(f"DTSTART;VALUE=DATE:{_date(ev.start)}")
        if ev.end is not None:
            lines.append(f"DTEND;VALUE=DATE:{_date(ev.end)}")
        lines.append(f"SUMMARY:{_escape(ev.summary)}")
        if ev.description:
            lines.append(f"DESCRIPTION:{_escape(ev.description)}")
        if ev.rrule:
            lines.append(f"RRULE:{ev.rrule}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
