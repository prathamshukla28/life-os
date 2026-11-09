"""ISO 8601 week model: ``YYYY-Www`` parsing/formatting, Mon..Sun dates, W53 and year edges."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

__all__ = ["DAY_LABELS", "DAY_NAMES", "IsoWeek", "day_index", "weeks_in_year"]

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_FULL = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEK = re.compile(r"([0-9]{4})-W([0-9]{2})")


def weeks_in_year(year: int) -> int:
    """52 or 53: 28 December always falls in the last ISO week of its year."""
    return date(year, 12, 28).isocalendar()[1]


def day_index(name: str) -> int:
    """0 for 'mon'/'Monday' ... 6 for 'sun'. ValueError otherwise."""
    key = name.strip().casefold() if isinstance(name, str) else ""
    if key in DAY_NAMES:
        return DAY_NAMES.index(key)
    if key in _FULL:
        return _FULL.index(key)
    raise ValueError(f"unknown day {name!r}; use one of {', '.join(DAY_NAMES)}")


@dataclass(frozen=True, order=True)
class IsoWeek:
    year: int
    week: int

    def __post_init__(self) -> None:
        if not 1 <= self.year <= 9999:
            raise ValueError(f"year {self.year} out of range")
        if not 1 <= self.week <= weeks_in_year(self.year):
            raise ValueError(f"{self.year} has {weeks_in_year(self.year)} ISO weeks, not week {self.week}")
        try:
            date.fromisocalendar(self.year, self.week, 7)
        except ValueError as exc:  # 9999-W52 ends in year 10000
            raise ValueError(f"{self} ends outside the supported date range") from exc

    @classmethod
    def parse(cls, text: str) -> IsoWeek:
        m = _WEEK.fullmatch(text) if isinstance(text, str) else None
        if m is None:
            raise ValueError(f"week {text!r} must look like '2026-W41'")
        return cls(int(m.group(1)), int(m.group(2)))

    @classmethod
    def containing(cls, day: date) -> IsoWeek:
        iso = day.isocalendar()
        return cls(iso[0], iso[1])

    def __str__(self) -> str:
        return f"{self.year:04d}-W{self.week:02d}"

    @property
    def start(self) -> date:
        return date.fromisocalendar(self.year, self.week, 1)

    @property
    def end(self) -> date:
        return date.fromisocalendar(self.year, self.week, 7)

    def days(self) -> tuple[date, ...]:
        return tuple(self.start + timedelta(days=i) for i in range(7))

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def next(self) -> IsoWeek:
        return IsoWeek.containing(self.start + timedelta(days=7))

    def previous(self) -> IsoWeek:
        return IsoWeek.containing(self.start - timedelta(days=7))
