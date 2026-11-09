"""Source records: where a claim or rule value came from (LM03, LM-RULE).

A ``SourceRecord`` is the citation for anything Life Mentor states about the
world. ``evidence_class`` reuses ASUR's honesty vocabulary
(``life_os.core.evidence.EVIDENCE_CLASSES``); ``tier`` says whether the
document is the primary authority (gazette, statute, regulator page) or a
secondary summary. Invalid records are rejected at construction (fail closed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from life_os.core.evidence import EVIDENCE_CLASSES

__all__ = ["SOURCE_TIERS", "SourceRecord", "parse_iso_date", "valid_source_url"]

SOURCE_TIERS = ("primary", "secondary")
_URL = re.compile(r"^https?://[^\s/?#]+[^\s]*$", re.IGNORECASE)


def valid_source_url(url: Any) -> bool:
    """True for a non-empty http(s) URL with a host. No network lookup."""
    return isinstance(url, str) and bool(_URL.match(url.strip())) and url == url.strip()


def parse_iso_date(value: Any, field_name: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string (or pass a date through)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name}: {value!r} is not an ISO date") from exc
    raise ValueError(f"{field_name}: expected ISO date string, got {type(value).__name__}")


@dataclass(frozen=True)
class SourceRecord:
    """One citation. ``published`` may be None when the source is undated."""

    url: str
    title: str
    publisher: str
    published: date | str | None
    retrieved_on: date | str
    tier: str
    evidence_class: str

    def __post_init__(self) -> None:
        if not valid_source_url(self.url):
            raise ValueError(f"url {self.url!r} is not an http(s) URL")
        for name in ("title", "publisher"):
            val = getattr(self, name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.tier not in SOURCE_TIERS:
            raise ValueError(f"tier {self.tier!r} not in {SOURCE_TIERS}")
        if self.evidence_class not in EVIDENCE_CLASSES:
            raise ValueError(f"evidence_class {self.evidence_class!r} not in {EVIDENCE_CLASSES}")
        object.__setattr__(self, "retrieved_on", parse_iso_date(self.retrieved_on, "retrieved_on"))
        if self.published is not None:
            object.__setattr__(self, "published", parse_iso_date(self.published, "published"))

    def to_dict(self) -> dict[str, Any]:
        published = self.published.isoformat() if isinstance(self.published, date) else None
        retrieved = self.retrieved_on
        return {
            "url": self.url,
            "title": self.title,
            "publisher": self.publisher,
            "published": published,
            "retrieved_on": retrieved.isoformat() if isinstance(retrieved, date) else retrieved,
            "tier": self.tier,
            "evidence_class": self.evidence_class,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceRecord:
        return cls(
            url=data["url"],
            title=data["title"],
            publisher=data["publisher"],
            published=data.get("published"),
            retrieved_on=data["retrieved_on"],
            tier=data["tier"],
            evidence_class=data["evidence_class"],
        )
