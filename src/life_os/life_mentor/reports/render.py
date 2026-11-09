"""Markdown rendering of calculator / engine reports (LM09).

A report is a list of ``Section`` objects, each wrapping one existing calculator output
(``calculator, inputs, rules_used, assumptions, result, gate``) - or ``None`` when the
facts it needs are missing. Every rendered section shows: status (gate disposition), key
results, inputs with provenance, rules and sources (url, retrieved date, UNVERIFIED),
assumptions, non-passing checks, questions, what would change it, and at most three
suggested actions. Nothing is invented: a missing section says what is missing.

The finished Markdown gets a final ``redact`` pass and must pass the LM02 scanner
(``assert_report_clean`` raises ``ForbiddenFieldLeak``, a ValueError) - fail closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP

from ..security import assert_report_clean, redact

__all__ = [
    "DISCLAIMERS",
    "DISPOSITION_WORDS",
    "RenderedReport",
    "Section",
    "fmt_value",
    "render_report",
    "section_actions",
    "section_lines",
    "worst",
]

DISCLAIMERS = {
    "finance": (
        "General information computed from your own figures and cited, dated rules - "
        "not tax, investment or legal advice. Rule values marked UNVERIFIED have not "
        "been checked by a person; check them before acting. Life Mentor never makes "
        "payments, purchases or investments."
    ),
    "health": (
        "General information, not medical advice and no diagnosis. Talk to your "
        "clinician before changing your diet, exercise or medicines, especially if you "
        "have a condition or take medicines."
    ),
    "career": ("Based only on your own figures and the sources you cited; Life Mentor adds no market data."),
    "decisions": ("Analyses, not instructions: Life Mentor never executes a decision - you decide and act."),
}
DISPOSITION_WORDS = {
    SHIP: "clear: every check passed",
    HOLD: "on hold: something is missing or needs your confirmation",
    BLOCK: "blocked: something is contradictory or invalid",
}
_RANK = {SHIP: 0, HOLD: 1, BLOCK: 2}
DASH = "—"

# Key result fields per calculator (dotted paths into ``result``).
KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "income_tax": (
        "regime",
        "tax_year",
        "gross_salary",
        "taxable_income",
        "slab_tax",
        "total_tax",
        "tax_as_percent_of_taxable_income",
    ),
    "take_home": (
        "regime",
        "gross_annual",
        "tax_breakdown.taxable_income",
        "income_tax",
        "payroll_deductions",
        "take_home_annual",
        "take_home_monthly",
    ),
    "cash_flow": (
        "take_home_monthly",
        "total_essential",
        "total_discretionary",
        "surplus",
        "savings_rate_percent",
    ),
    "emergency_fund": ("months", "target", "current_balance", "gap", "months_to_target", "months_of_cover"),
    "net_worth": ("total_assets", "total_liabilities", "net_worth"),
    "debt_payoff": (
        "avalanche.status",
        "avalanche.months",
        "avalanche.total_interest",
        "snowball.status",
        "snowball.months",
        "snowball.total_interest",
        "comparison.interest_saved_by_avalanche",
    ),
    "goal_sip": (
        "target_corpus",
        "months",
        "scenarios.conservative.monthly_contribution",
        "scenarios.base.monthly_contribution",
        "scenarios.optimistic.monthly_contribution",
    ),
    "protein_target": ("body_weight_kg", "ear_g_per_day", "rda_g_per_day", "cereal_based_diet_g_per_day"),
    "health_safety": ("medications_listed", "conditions_listed"),
    "exercise_plan": (
        "strength_sessions",
        "aerobic_sessions",
        "mobility_minutes_per_session",
        "weekly_aerobic_moderate_minutes",
    ),
    "sleep_analysis": (
        "nights",
        "average_hours",
        "bedtime_sd_minutes",
        "wake_sd_minutes",
        "nights_below_reference",
        "flags",
    ),
    "metric_review": (
        "metric",
        "unit",
        "period",
        "current.points",
        "current.average",
        "change_in_average",
        "first_to_last_in_current",
        "note",
    ),
    "job_offer_comparison": (
        "difference_monthly.take_home",
        "difference_monthly.net_after_work_costs",
        "batna.option",
    ),
    "salary_benchmarks": ("cited_range.low", "cited_range.high", "sources_used", "evidence_note"),
    "negotiation_prep": ("offer_position_in_cited_range", "target_position_in_cited_range"),
    "decision": ("recommendation.status", "recommendation.option", "recommendation.text"),
    "decision_history": ("analysed", "confirmed"),
    "meal_analysis": ("total_grams",),
    "bank_csv_import": ("rows_used", "credits_skipped", "unmapped_rows", "dry_run", "written"),
    "mf_valuation": ("as_of", "total_value"),
}
_NUTRIENTS = ("1008", "1003", "1004", "1005", "1079")


@dataclass(frozen=True)
class Section:
    title: str
    report: dict[str, Any] | None
    missing: tuple[str, ...] = ()
    questions: tuple[dict[str, str], ...] = ()
    required: bool = True
    note: str = ""

    @property
    def disposition(self) -> str | None:
        """Gate disposition; a missing required section is HOLD, a missing optional one None."""
        if self.report is not None:
            return str(self.report["gate"]["disposition"])
        return HOLD if self.required else None


@dataclass(frozen=True)
class RenderedReport:
    kind: str
    title: str
    markdown: str
    disposition: str
    sections: tuple[Section, ...] = field(default_factory=tuple)


def worst(dispositions: Iterable[str | None]) -> str | None:
    found = [d for d in dispositions if d is not None]
    return max(found, key=lambda d: _RANK[d]) if found else None


# -- formatting ------------------------------------------------------------------------


def _group(digits: str) -> str:
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt_value(value: Any) -> str:
    """Indian digit grouping for numbers (keeps recorded decimals); compact lists/dicts."""
    if value is None:
        return DASH
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (Decimal, int)):
        text = format(value, "f") if isinstance(value, Decimal) else str(value)
        sign = "-" if text.startswith("-") else ""
        whole, _, frac = text.lstrip("-").partition(".")
        return f"{sign}{_group(whole)}" + (f".{frac}" if frac else "")
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return fmt_value(Decimal(value["$decimal"]))
        return "; ".join(f"{k}: {fmt_value(v)}" for k, v in value.items()) or DASH
    if isinstance(value, (list, tuple)):
        return ", ".join(fmt_value(v) for v in value) or "none"
    return " ".join(str(value).split())


def _cell(value: Any) -> str:
    return fmt_value(value).replace("|", "\\|")


def _get(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _label(path: str) -> str:
    text = path.replace(".", " ").replace("_", " ")
    return text[:1].upper() + text[1:]


# -- result blocks ------------------------------------------------------------------------


def _result_lines(report: dict[str, Any], h: str) -> list[str]:
    result = report.get("result")
    calc = report.get("calculator", "")
    lines = [f"{h} Result", ""]
    if not result:
        return [*lines, "Not computed (see the checks below)."]
    paths = KEY_FIELDS.get(calc) or tuple(
        k for k, v in result.items() if isinstance(v, (str, int, Decimal, bool))
    )
    for path in paths:
        value = _get(result, path)
        if value is not None:
            lines.append(f"- {_label(path)}: {fmt_value(value)}")
    lines += _special(calc, result)
    return lines


def _special(calc: str, result: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if calc == "decision":
        for opt in result.get("options", []):
            out += [
                "",
                (f"Option **{fmt_value(opt['name'])}** - {opt['gate']['disposition']}"),
                "",
                "| Step | Criterion | Status | Detail |",
                "|---|---|---|---|",
            ]
            out += [
                f"| {c['step']} | {c['criterion']} | {c['disposition']} | {_cell(c['reason'])} |"
                for c in opt["criteria"]
            ]
    elif calc == "health_safety":
        flags = result.get("flags", [])
        out.append(f"- Cited flags: {len(flags)}")
        out += [
            f"  - {f['id']}: {fmt_value(f['message'])} ({f['source_url']}) - {fmt_value(f['action'])}"
            for f in flags
        ]
    elif calc == "habit_summary":
        out += [
            f"- {fmt_value(h['name'])}: streak {h['current_streak_days']} day(s), "
            f"{h['days_done']} of {h['days_tracked']} days done "
            f"({fmt_value(h['completion_percent'])}%)"
            for h in result.get("habits", [])
        ]
    elif calc == "meal_analysis":
        totals = result.get("totals", {})
        out += [
            f"- {totals[n]['name']}: {fmt_value(totals[n]['amount'])} {totals[n]['unit']}"
            + ("" if totals[n]["complete"] else " (lower bound)")
            for n in _NUTRIENTS
            if n in totals
        ]
    elif calc == "career_snapshot":
        out += [
            f"- {_label(k)}: {fmt_value(v['value'])} ({v['provenance']})"
            for k, v in result.get("facts", {}).items()
        ]
    elif calc == "job_offer_comparison":
        out += [f"- {_label(r['criterion'])}: {fmt_value(r['values'])}" for r in result.get("criteria", [])]
    elif calc == "negotiation_prep":
        out += [
            f"- {fmt_value(i['item'])}: {fmt_value(i['status'])} ({fmt_value(i['basis'])})"
            for i in result.get("checklist", [])
        ]
    elif calc == "exercise_plan":
        out += [
            f"- WHO check, {r['criterion'].replace('_', ' ')}: {'met' if r['met'] else 'not met'}"
            for r in result.get("guideline_check", [])
        ]
    elif calc == "bank_csv_import":
        out += [f"- {month}: {fmt_value(cats)}" for month, cats in result.get("months", {}).items()]
        out += [f"- Line {r['line']} not read: {r['reason']}" for r in result.get("rejected", [])]
    elif calc == "mf_valuation":
        out += [
            f"- {h['scheme_code']} {fmt_value(h['scheme_name'])} ({h['plan']}, "
            f"{h['option']}): {fmt_value(h['units'])} units x NAV {fmt_value(h['nav'])} "
            f"of {h['nav_date']} = {fmt_value(h['value'])}"
            for h in result.get("holdings", [])
        ]
    elif calc == "decision_history":
        items = result.get("items", [])
        if not items:
            out.append("- No decisions recorded yet.")
        else:
            out += ["", "| Date | Event | Decision id | Detail |", "|---|---|---|---|"]
            out += [
                f"| {i['date']} | {i['event']} | {i['decision_id']} | {_cell(i['detail'])} |" for i in items
            ]
    return out


# -- other blocks ---------------------------------------------------------------------------


def _questions(section: Section) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    report_qs = (section.report or {}).get("questions", []) or []
    for q in [*report_qs, *section.questions]:
        k = (str(q.get("key")), str(q.get("reason")))
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out


def _bad_reasons(report: dict[str, Any] | None, disposition: str) -> list[str]:
    if not report:
        return []
    return [
        f"{r['check']}: {r['reason']}" for r in report["gate"]["reasons"] if r["disposition"] == disposition
    ]


def section_actions(section: Section) -> list[tuple[int, str]]:
    """Prioritised actions: 0 fix BLOCK, 1 answer, 2 resolve HOLD, 3 next steps, 4 verify,
    5 optional data. Lower is more important."""
    acts: list[tuple[int, str]] = []
    acts += [(0, f"Fix: {r}") for r in _bad_reasons(section.report, BLOCK)]
    prio = 1 if section.required else 5
    acts += [(prio, f"Answer: {q['question']}") for q in _questions(section)]
    if section.report is None and not _questions(section) and section.missing:
        acts.append((prio, f"Provide: {', '.join(section.missing)}"))
    asked = [str(q.get("question")) for q in _questions(section)]
    acts += [
        (2, f"Resolve: {r}")
        for r in _bad_reasons(section.report, HOLD)
        if not any(a and a in r for a in asked)
    ]  # already asked as a question
    result = (section.report or {}).get("result") or {}
    acts += [(3, str(s)) for s in result.get("next_steps", []) if "never executes" not in str(s)]
    if any(r.get("unverified") for r in (section.report or {}).get("rules_used", [])):
        acts.append(
            (
                4,
                (
                    "Check the cited rule values marked UNVERIFIED against their sources "
                    "before acting on them."
                ),
            )
        )
    seen: set[str] = set()
    unique: list[tuple[int, str]] = []
    for p, text in sorted(acts, key=lambda a: a[0]):
        if text not in seen:
            seen.add(text)
            unique.append((p, text))
    return unique


def section_lines(section: Section, level: int = 2) -> list[str]:
    h, hh = "#" * level, "#" * (level + 1)
    lines = [f"{h} {section.title}", ""]
    disp = section.disposition
    if section.report is None:
        what = ", ".join(section.missing) or "the facts this needs"
        lines.append(
            f"Status: {disp} ({DISPOSITION_WORDS[HOLD]}). Not available: missing {what}."
            if section.required
            else f"Not provided (optional). Missing: {what}."
        )
        if section.note:
            lines.append(section.note)
    else:
        rep = section.report
        lines += [f"Status: {disp} ({DISPOSITION_WORDS[str(disp)]})", ""]
        lines += _result_lines(rep, hh)
        if rep.get("inputs"):
            lines += ["", f"{hh} Inputs", "", "| Input | Value | Provenance | Basis |", "|---|---|---|---|"]
            lines += [
                f"| {_cell(name)} | {_cell(iv.get('value'))} | {iv.get('provenance')} | "
                f"{_cell(iv.get('basis'))} |"
                for name, iv in rep["inputs"].items()
            ]
        if rep.get("rules_used"):
            lines += [
                "",
                f"{hh} Rules and sources",
                "",
                "| Rule | Source | Retrieved | Status |",
                "|---|---|---|---|",
            ]
            for r in rep["rules_used"]:
                status = f"UNVERIFIED ({r.get('source_tier')})" if r.get("unverified") else "human-verified"
                lines.append(f"| {r['id']} | {r['source_url']} | {r['retrieved_on']} | {status} |")
        if rep.get("assumptions"):
            lines += ["", f"{hh} Assumptions", "", *(f"- {fmt_value(a)}" for a in rep["assumptions"])]
        reasons = rep["gate"]["reasons"]
        bad = [r for r in reasons if r["disposition"] != SHIP]
        lines += ["", f"{hh} Checks", ""]
        lines += [f"- {r['disposition']} - {r['check']}: {fmt_value(r['reason'])}" for r in bad]
        lines.append(f"- {len(reasons) - len(bad)} check(s) passed.")
    qs = _questions(section)
    if qs:
        lines += ["", f"{hh} Questions", ""]
        lines += [f"- {q.get('key')} ({q.get('reason')}): {fmt_value(q.get('question'))}" for q in qs]
    changes = [str(c) for c in ((section.report or {}).get("result") or {}).get("change_conditions", [])]
    changes += [
        f"Resolved: {r}" for r in _bad_reasons(section.report, BLOCK) + _bad_reasons(section.report, HOLD)
    ][:5]
    if section.report is None:
        changes.append(f"Providing {', '.join(section.missing) or 'the missing facts'}.")
    lines += ["", f"{hh} What would change this", ""]
    lines += [f"- {fmt_value(c)}" for c in changes] or [
        "- Nothing here is on hold; it changes if your inputs or the cited rules change."
    ]
    acts = [t for _, t in section_actions(section)][:3]
    lines += ["", f"{hh} Suggested actions (up to 3)", ""]
    lines += [f"{i}. {fmt_value(t)}" for i, t in enumerate(acts, 1)] or ["- None."]
    return lines


def next_steps(sections: Sequence[Section], limit: int = 3) -> list[str]:
    acts = sorted((a for s in sections for a in section_actions(s)), key=lambda a: a[0])
    out: list[str] = []
    for _, text in acts:
        if text not in out:
            out.append(text)
    return out[:limit]


def finish(markdown: str) -> str:
    """Final redaction pass + LM02 scanner (raises ForbiddenFieldLeak on a leak)."""
    text = redact(markdown)
    assert_report_clean(text)
    return text


def render_report(
    kind: str, title: str, sections: Sequence[Section], *, as_of: date, generated_on: str, disclaimer: str
) -> RenderedReport:
    disposition = worst(s.disposition for s in sections) or HOLD
    lines = [
        f"# {title}",
        "",
        (
            f"As of {as_of.isoformat()}. Generated on {generated_on}. Overall status: "
            f"{disposition} ({DISPOSITION_WORDS[disposition]})."
        ),
        "",
    ]
    if disclaimer:
        lines += [f"> {disclaimer}", ""]
    if not any(s.disposition for s in sections):
        lines += ["No data has been provided for this report yet.", ""]
    for s in sections:
        lines += [*section_lines(s, 2), ""]
    steps = next_steps(sections)
    lines += ["## Next steps", ""]
    lines += [f"{i}. {fmt_value(t)}" for i, t in enumerate(steps, 1)] or ["- Nothing open."]
    return RenderedReport(kind, title, finish("\n".join(lines) + "\n"), disposition, tuple(sections))
