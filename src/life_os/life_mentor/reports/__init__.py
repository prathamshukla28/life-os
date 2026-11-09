"""Life Mentor reports (LM09): personalised Markdown assembled from the calculators and the
decision engine - inputs with provenance, cited rules (UNVERIFIED flagged), assumptions,
gate status, questions, what would change it, and at most three next steps. No score."""

from __future__ import annotations

from .builders import (
    career_report,
    career_sections,
    decisions_report,
    decisions_sections,
    finance_report,
    finance_sections,
    health_report,
    health_sections,
    life_plan_report,
    lifestyle_report,
    lifestyle_sections,
    requirements_for,
)
from .render import DISCLAIMERS, RenderedReport, Section, fmt_value, render_report

__all__ = [
    "DISCLAIMERS",
    "RenderedReport",
    "Section",
    "career_report",
    "career_sections",
    "decisions_report",
    "decisions_sections",
    "finance_report",
    "finance_sections",
    "fmt_value",
    "health_report",
    "health_sections",
    "life_plan_report",
    "lifestyle_report",
    "lifestyle_sections",
    "render_report",
    "requirements_for",
]
