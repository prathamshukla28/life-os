"""Personalised reports built from the profile and the existing calculators (LM09).

Each builder reads consented facts (``Profile.get`` - read-only, never ``export``), runs
the LM04-LM08 calculators, and wraps every output in a ``Section``. Missing facts become a
"not available" section with questions; nothing is defaulted silently. Builders never
write to the store. ``as_of`` and ``generated_on`` are explicit, so output is deterministic.

Fact keys used (category):
  finance  gross_salary_annual, payroll_deductions_annual, take_home_monthly (optional),
           other_deductions_annual (old regime), essential_spend_monthly,
           discretionary_spend_monthly, emergency_fund_balance, emergency_fund_months,
           assets {name: amount}, liabilities {name: amount}, debts [{name, balance,
           annual_rate_percent, minimum_payment}], debt_budget_monthly, sip_goal
           {target_corpus, months, conservative, base, optimistic, current_savings}
  identity age
  health   body_weight_kg, medications, conditions, medical_limitations,
           typical_day_meals [{food, grams}]
  lifestyle exercise_experience, exercise_days_per_week, exercise_minutes_per_session,
           exercise_equipment [..], sleep_log [{night, bedtime, wake_time}]
  career   role, years_experience, skills, industry, location, ambitions,
           job_offer {name, gross_salary_annual, payroll_deductions_annual, commute_monthly,
           other_monthly_costs, probation_months, notes}, salary_benchmarks [{url, title,
           publisher, published, retrieved_on, tier, evidence_class, role, location, low,
           median, high, sample_notes}]
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from ..career import (
    JobPackage,
    SalaryBenchmark,
    benchmark_summary,
    career_snapshot,
    compare_job_offer,
    job_package_from_profile,
    job_package_questions,
    negotiation_prep,
)
from ..decision_engine import DECISION_JOURNAL
from ..finance import (
    Debt,
    cash_flow,
    debt_payoff,
    emergency_fund,
    goal_sip,
    load_tax_pack,
    net_worth,
    take_home,
)
from ..finance.report import finalize, fixed_check
from ..health import (
    MealItem,
    analyze_meal,
    health_safety_from_profile,
    load_food_table,
    load_interactions,
    load_nutrition_refs,
    protein_target_from_profile,
)
from ..lifestyle import (
    HabitTracker,
    SleepEntry,
    analyze_sleep,
    load_activity_refs,
    load_sleep_refs,
    plan_exercise_from_profile,
)
from ..profile import (
    ASSUMPTION,
    UNKNOWN,
    USER_ESTIMATE,
    VERIFIED,
    ConsentRequired,
    Fact,
    Profile,
    Requirement,
)
from ..research import RulePack, SourceRecord
from ..security import JournalTampered
from ..tracking import MetricLog, review
from .render import (
    DISCLAIMERS,
    DISPOSITION_WORDS,
    RenderedReport,
    Section,
    finish,
    fmt_value,
    next_steps,
    render_report,
    section_actions,
    section_lines,
    worst,
)

__all__ = [
    "career_report",
    "career_sections",
    "decisions_report",
    "decisions_sections",
    "finance_report",
    "finance_sections",
    "health_report",
    "health_sections",
    "life_plan_report",
    "lifestyle_report",
    "lifestyle_sections",
    "requirements_for",
]

_PROV_ORDER = (VERIFIED, USER_ESTIMATE, ASSUMPTION)
REVIEW_METRICS = ("weight_kg", "sleep_hours", "exercise_minutes")


# -- fact helpers ------------------------------------------------------------------------


def _fact(profile: Profile, key: str) -> Fact | None:
    try:
        f = profile.get(key)
    except ConsentRequired:
        return None
    return None if f is None or f.provenance == UNKNOWN else f


def _gather(
    profile: Profile, reqs: Sequence[Requirement]
) -> tuple[dict[str, Fact], list[str], tuple[dict[str, str], ...]]:
    questions = profile.missing_facts(reqs, allow_assumptions=True)
    bad = {q["key"] for q in questions}
    facts: dict[str, Fact] = {}
    for r in reqs:
        f = None if r.key in bad else _fact(profile, r.key)
        if f is not None:
            facts[r.key] = f
    missing = [r.key for r in reqs if r.key not in facts]
    return facts, missing, tuple(questions)


def _weakest(*facts: Fact) -> str:
    return max((f.provenance for f in facts), key=_PROV_ORDER.index)


def _derived(key: str, value: Decimal, category: str, basis: str, as_of: date, *sources: Fact) -> Fact:
    """A derived value carries the weakest provenance of the facts it came from."""
    return Fact(key, value, category, _weakest(*sources), f"derived: {basis}", as_of.isoformat())


def _sub(fact: Fact, name: str, value: Any) -> Fact:
    return Fact(f"{fact.key}:{name}", value, fact.category, fact.provenance, fact.basis, fact.recorded_at)


def _money_map(fact: Fact) -> dict[str, Fact] | None:
    if not isinstance(fact.value, dict) or not all(
        isinstance(v, (Decimal, int)) and not isinstance(v, bool) for v in fact.value.values()
    ):
        return None
    return {str(k): _sub(fact, str(k), v) for k, v in fact.value.items()}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return None


def _shape_problem(title: str, key: str, expected: str, required: bool = False) -> Section:
    return Section(
        title,
        None,
        missing=(key,),
        required=required,
        questions=(
            {
                "key": key,
                "category": "",
                "reason": "wrong_shape",
                "question": f"Please correct '{key}': expected {expected}.",
            },
        ),
    )


def _q(key: str, category: str, text: str) -> Requirement:
    return Requirement(key, category, text)


REQUIREMENTS: dict[str, list[Requirement]] = {
    "finance": [
        _q(
            "gross_salary_annual",
            "finance",
            "What is your gross annual salary (fixed pay, before tax), and how do you know it?",
        ),
        _q(
            "payroll_deductions_annual",
            "finance",
            "What are your yearly payroll deductions (professional tax plus employee EPF)?",
        ),
        _q(
            "essential_spend_monthly",
            "finance",
            "What do you spend each month on essentials (rent, food, utilities, EMIs)?",
        ),
        _q(
            "emergency_fund_balance",
            "finance",
            "How much do you hold as an emergency fund in liquid savings?",
        ),
    ],
    "health": [
        _q("body_weight_kg", "health", "What is your body weight in kg, and how measured?"),
        _q("medications", "health", "Which medicines do you take? (empty if none)"),
        _q("conditions", "health", "Which health conditions do you have? (empty if none)"),
    ],
    "lifestyle": [
        _q("age", "identity", "What is your age?"),
        _q("medical_limitations", "health", "Any medical limitation affecting exercise (yes/no)?"),
        _q("exercise_experience", "lifestyle", "Exercise experience: beginner, intermediate or advanced?"),
        _q("exercise_days_per_week", "lifestyle", "How many days a week can you exercise?"),
        _q("exercise_minutes_per_session", "lifestyle", "How many minutes per session?"),
        _q("exercise_equipment", "lifestyle", "Which equipment: none, resistance_bands, dumbbells, gym?"),
    ],
    "career": [
        _q(k, "career", f"Please add your {k.replace('_', ' ')}.")
        for k in ("role", "years_experience", "skills", "industry", "location", "ambitions")
    ],
}


def requirements_for(kind: str) -> list[Requirement]:
    if kind == "all":
        return [r for reqs in REQUIREMENTS.values() for r in reqs]
    return list(REQUIREMENTS[kind])


# -- finance ---------------------------------------------------------------------------------


def finance_sections(
    profile: Profile,
    *,
    as_of: date,
    tax_pack: RulePack | None = None,
    regime: str = "new",
    tax_year: str | None = None,
) -> list[Section]:
    pack = tax_pack or load_tax_pack()
    out: list[Section] = []
    reqs = REQUIREMENTS["finance"][:2] + (
        [_q("age", "identity", "What is your age?")] if regime == "old" else []
    )
    facts, missing, qs = _gather(profile, reqs)
    th_report = None
    if missing:
        out.append(Section("Take-home pay", None, missing=tuple(missing), questions=qs))
    else:
        th_report = take_home(
            pack,
            as_of=as_of,
            regime=regime,
            gross_salary=facts["gross_salary_annual"],
            payroll_deductions=facts["payroll_deductions_annual"],
            age=facts.get("age"),
            other_deductions=_fact(profile, "other_deductions_annual") if regime == "old" else None,
            tax_year=tax_year,
        )
        out.append(Section("Take-home pay", th_report, questions=qs))

    th_fact = _fact(profile, "take_home_monthly")
    if th_fact is None and th_report and th_report["gate"]["disposition"] == "SHIP":
        th_fact = _derived(
            "take_home_monthly",
            th_report["result"]["take_home_monthly"],
            "finance",
            f"LM04 take-home ({regime} regime, as of {as_of.isoformat()})",
            as_of,
            facts["gross_salary_annual"],
            facts["payroll_deductions_annual"],
        )
    ess_facts, ess_missing, ess_qs = _gather(profile, REQUIREMENTS["finance"][2:3])
    cf_report = None
    if th_fact is None or ess_missing:
        miss = tuple(ess_missing) + (() if th_fact else (("take_home_monthly (or the salary facts above)"),))
        out.append(Section("Monthly cash flow", None, missing=miss, questions=ess_qs))
    else:
        disc = _fact(profile, "discretionary_spend_monthly")
        cf_report = cash_flow(
            take_home_monthly=th_fact,
            essential_expenses={"essential spending": ess_facts["essential_spend_monthly"]},
            discretionary_expenses={"discretionary spending": disc} if disc else None,
        )
        out.append(Section("Monthly cash flow", cf_report, questions=ess_qs))

    ef_facts, ef_missing, ef_qs = _gather(profile, REQUIREMENTS["finance"][2:4])
    surplus_ok = cf_report is not None and cf_report["result"] is not None
    if ef_missing or not surplus_ok or th_fact is None:
        miss = tuple(ef_missing) + (() if surplus_ok else ("monthly surplus (cash flow above)",))
        out.append(Section("Emergency fund", None, missing=miss, questions=ef_qs))
    else:
        assert cf_report is not None and cf_report["result"] is not None
        ess = ef_facts["essential_spend_monthly"]
        surplus = _derived(
            "monthly_surplus",
            cf_report["result"]["surplus"],
            "finance",
            "cash-flow surplus",
            as_of,
            th_fact,
            ess,
        )
        months_fact = _fact(profile, "emergency_fund_months")
        months = _int(months_fact.value) if months_fact else None
        if months_fact is not None and months is None:
            out.append(
                _shape_problem(
                    "Emergency fund", "emergency_fund_months", "a whole number of months", required=True
                )
            )
        else:
            out.append(
                Section(
                    "Emergency fund",
                    emergency_fund(
                        essential_monthly=ess,
                        current_balance=ef_facts["emergency_fund_balance"],
                        monthly_surplus=surplus,
                        months=months,
                    ),
                    questions=ef_qs,
                )
            )

    out.append(_net_worth_section(profile))
    out.append(_debt_section(profile))
    out.append(_sip_section(profile, as_of))
    return out


def _net_worth_section(profile: Profile) -> Section:
    a, li = _fact(profile, "assets"), _fact(profile, "liabilities")
    if a is None or li is None:
        missing = tuple(k for k, f in (("assets", a), ("liabilities", li)) if f is None)
        return Section("Net worth", None, missing=missing, required=False)
    am, lm = _money_map(a), _money_map(li)
    if am is None or lm is None:
        return _shape_problem(
            "Net worth", "assets" if am is None else "liabilities", "an object of name: amount"
        )
    return Section("Net worth", net_worth(assets=am, liabilities=lm), required=False)


def _debt_section(profile: Profile) -> Section:
    debts, budget = _fact(profile, "debts"), _fact(profile, "debt_budget_monthly")
    if debts is None or budget is None:
        missing = tuple(k for k, f in (("debts", debts), ("debt_budget_monthly", budget)) if f is None)
        return Section("Debt payoff", None, missing=missing, required=False)
    keys = ("balance", "annual_rate_percent", "minimum_payment")
    if not isinstance(debts.value, list) or not all(
        isinstance(d, dict) and "name" in d and all(k in d for k in keys) for d in debts.value
    ):
        return _shape_problem(
            "Debt payoff", "debts", "a list of {name, balance, annual_rate_percent, minimum_payment}"
        )
    items = [
        Debt(str(d["name"]), *(_sub(debts, f"{d['name']}.{k}", d[k]) for k in keys)) for d in debts.value
    ]
    return Section("Debt payoff", debt_payoff(items, monthly_budget=budget), required=False)


def _sip_section(profile: Profile, as_of: date) -> Section:
    goal = _fact(profile, "sip_goal")
    if goal is None:
        return Section("Goal SIP", None, missing=("sip_goal",), required=False)
    v = goal.value
    need = ("target_corpus", "months", "conservative", "base", "optimistic")
    if not isinstance(v, dict) or not all(k in v for k in need) or _int(v["months"]) is None:
        return _shape_problem(
            "Goal SIP",
            "sip_goal",
            "{target_corpus, months, conservative, base, optimistic[, current_savings]}",
        )
    rates = {s: _sub(goal, f"return.{s}", v[s]) for s in ("conservative", "base", "optimistic")}
    savings = _sub(goal, "current_savings", v["current_savings"]) if "current_savings" in v else None
    return Section(
        "Goal SIP",
        goal_sip(
            target_corpus=_sub(goal, "target", v["target_corpus"]),
            months=_int(v["months"]),
            annual_return_percent=rates,
            current_savings=savings,
            as_of=as_of,
        ),
        required=False,
    )


# -- health -----------------------------------------------------------------------------------


def health_sections(profile: Profile, *, as_of: date) -> list[Section]:
    out = [
        Section(
            "Protein reference", protein_target_from_profile(profile, load_nutrition_refs(), as_of=as_of)
        ),
        Section(
            "Medicine and condition flags",
            health_safety_from_profile(profile, load_interactions(), as_of=as_of),
        ),
    ]
    meals = _fact(profile, "typical_day_meals")
    if meals is None:
        out.append(Section("Typical day meals", None, missing=("typical_day_meals",), required=False))
    elif not isinstance(meals.value, list) or not all(
        isinstance(m, dict) and {"food", "grams"} <= set(m) for m in meals.value
    ):
        out.append(_shape_problem("Typical day meals", "typical_day_meals", "a list of {food, grams}"))
    else:
        items = [
            MealItem(str(m["food"]), _sub(meals, f"meal{i}.grams", m["grams"]))
            for i, m in enumerate(meals.value)
        ]
        out.append(Section("Typical day meals", analyze_meal(items, table=load_food_table()), required=False))
    return out


# -- lifestyle -------------------------------------------------------------------------------


def lifestyle_sections(profile: Profile, *, as_of: date) -> list[Section]:
    out: list[Section] = []
    keys = (
        "exercise_experience",
        "exercise_days_per_week",
        "exercise_minutes_per_session",
        "exercise_equipment",
    )
    ex = {k: _fact(profile, k) for k in keys}
    if any(v is None for v in ex.values()):
        out.append(
            Section(
                "Exercise plan", None, required=False, missing=tuple(k for k, v in ex.items() if v is None)
            )
        )
    else:
        f = {k: v.value for k, v in ex.items() if v is not None}
        equipment = f["exercise_equipment"]
        out.append(
            Section(
                "Exercise plan",
                plan_exercise_from_profile(
                    profile,
                    refs=load_activity_refs(),
                    as_of=as_of,
                    experience=str(f["exercise_experience"]),
                    days_per_week=_int(f["exercise_days_per_week"]),
                    minutes_per_session=_int(f["exercise_minutes_per_session"]),
                    equipment=tuple(equipment) if isinstance(equipment, list) else (str(equipment),),
                ),
                required=False,
            )
        )
    log = _fact(profile, "sleep_log")
    if log is None:
        out.append(Section("Sleep", None, missing=("sleep_log",), required=False))
    else:
        try:
            entries = [
                SleepEntry(date.fromisoformat(str(e["night"])), str(e["bedtime"]), str(e["wake_time"]))
                for e in log.value
            ]
        except (TypeError, KeyError, ValueError):
            out.append(_shape_problem("Sleep", "sleep_log", "a list of {night, bedtime, wake_time}"))
        else:
            out.append(
                Section(
                    "Sleep",
                    analyze_sleep(entries, refs=load_sleep_refs(), as_of=as_of, age=_fact(profile, "age")),
                    required=False,
                )
            )
    tracker = HabitTracker(profile.store)
    try:
        has_habits = bool(tracker.habit_history())
    except ConsentRequired:
        out.append(
            Section(
                "Habits",
                None,
                missing=("lifestyle consent",),
                required=False,
                questions=(
                    {
                        "key": "lifestyle",
                        "category": "lifestyle",
                        "reason": "no_consent",
                        "question": "May Life Mentor use your habit log?",
                    },
                ),
            )
        )
        has_habits = None
    if has_habits:
        out.append(Section("Habits", tracker.summary(as_of=as_of), required=False))
    elif has_habits is False:
        out.append(Section("Habits", None, missing=("habits",), required=False))
    metrics = MetricLog(profile.store)
    for metric in REVIEW_METRICS:
        try:
            if not metrics.series(metric):
                continue
        except ConsentRequired:
            continue
        out.append(
            Section(
                f"Weekly review: {metric.replace('_', ' ')}",
                review(metrics, metric, as_of=as_of, period="weekly"),
                required=False,
            )
        )
    return out


# -- career ------------------------------------------------------------------------------------


def career_sections(
    profile: Profile, *, as_of: date, tax_pack: RulePack | None = None, regime: str = "new"
) -> list[Section]:
    out = [Section("Career profile", career_snapshot(profile))]
    offer = _fact(profile, "job_offer")
    comparison = None
    if offer is None:
        out.append(Section("Job offer comparison", None, missing=("job_offer",), required=False))
    elif not isinstance(offer.value, dict) or "gross_salary_annual" not in offer.value:
        out.append(
            _shape_problem(
                "Job offer comparison",
                "job_offer",
                "{gross_salary_annual, payroll_deductions_annual, "
                "commute_monthly, other_monthly_costs, probation_months}",
            )
        )
    else:
        v = offer.value
        pkg = JobPackage(
            name=str(v.get("name", "offer")),
            gross_salary_annual=_sub(offer, "gross", v["gross_salary_annual"]),
            payroll_deductions_annual=_sub(offer, "payroll", v.get("payroll_deductions_annual"))
            if v.get("payroll_deductions_annual") is not None
            else None,
            commute_monthly=_sub(offer, "commute", v.get("commute_monthly"))
            if v.get("commute_monthly") is not None
            else None,
            other_monthly_costs=_sub(offer, "other", v.get("other_monthly_costs"))
            if v.get("other_monthly_costs") is not None
            else None,
            probation_months=_int(v.get("probation_months", 0)),
            notes=tuple(str(n) for n in v.get("notes", [])),
        )
        comparison = compare_job_offer(
            job_package_from_profile(profile),
            pkg,
            tax_pack=tax_pack or load_tax_pack(),
            as_of=as_of,
            regime=regime,
            age=_fact(profile, "age"),
        )
        out.append(
            Section(
                "Job offer comparison",
                comparison,
                required=False,
                questions=tuple(job_package_questions(profile)),
            )
        )
    bench_fact = _fact(profile, "salary_benchmarks")
    bench = None
    if bench_fact is None:
        out.append(
            Section(
                "Salary benchmarks",
                None,
                missing=("salary_benchmarks",),
                required=False,
                note="Without a cited, dated source there is "
                "insufficient market evidence; Life Mentor bundles no salary data.",
            )
        )
    else:
        try:
            items = [
                SalaryBenchmark(
                    source=SourceRecord(
                        url=b["url"],
                        title=b["title"],
                        publisher=b["publisher"],
                        published=b.get("published"),
                        retrieved_on=b["retrieved_on"],
                        tier=b["tier"],
                        evidence_class=b["evidence_class"],
                    ),
                    role=str(b["role"]),
                    location=str(b["location"]),
                    low=_sub(bench_fact, f"b{i}.low", b["low"]),
                    median=None if b.get("median") is None else _sub(bench_fact, f"b{i}.median", b["median"]),
                    high=_sub(bench_fact, f"b{i}.high", b["high"]),
                    sample_notes=str(b["sample_notes"]),
                )
                for i, b in enumerate(bench_fact.value)
            ]
        except (TypeError, KeyError, ValueError):
            out.append(
                _shape_problem("Salary benchmarks", "salary_benchmarks", "a list of cited benchmark records")
            )
        else:
            bench = benchmark_summary(items, as_of=as_of)
            out.append(Section("Salary benchmarks", bench, required=False))
    if comparison is not None and bench is not None:
        out.append(Section("Negotiation checklist", negotiation_prep(comparison, bench), required=False))
    return out


# -- decisions -----------------------------------------------------------------------------------


def decisions_sections(
    profile: Profile, *, as_of: date, decision_reports: Sequence[dict[str, Any]] = (), limit: int = 20
) -> list[Section]:
    out: list[Section] = []
    if not profile.consent.has("finance"):
        out.append(
            Section(
                "Decision history",
                None,
                missing=("finance consent",),
                questions=(
                    {
                        "key": "finance",
                        "category": "finance",
                        "reason": "no_consent",
                        "question": "May Life Mentor show your decision history? It needs finance consent.",
                    },
                ),
            )
        )
    else:
        try:
            entries = profile.store.journal(DECISION_JOURNAL).entries()
            checks = [fixed_check("SHIP", "journal", f"decision journal verified ({len(entries)} entries)")]
        except JournalTampered as exc:
            entries, checks = [], [fixed_check("BLOCK", "journal", str(exc))]
        items: list[dict[str, Any]] = []
        for e in entries:
            day = str(e.get("recorded_at", ""))[:10]
            if day > as_of.isoformat():
                continue
            ev = e["event"]
            kind = str(ev.get("event"))
            detail = (
                f"{ev.get('recommendation_status')}: {ev.get('recommended') or '-'}"
                if kind == "decision_analyzed"
                else f"{ev.get('option')} ({ev.get('option_disposition')}; executed: no)"
            )
            items.append(
                {
                    "date": day,
                    "event": kind,
                    "decision_id": str(ev.get("decision_id", ""))[:12],
                    "detail": detail,
                }
            )
        out.append(
            Section(
                "Decision history",
                finalize(
                    "decision_history",
                    inputs=[],
                    checks=checks,
                    assumptions=[
                        (
                            "The journal records analyses and your confirmations; Life Mentor "
                            "never executes a decision."
                        )
                    ],
                    result={
                        "items": items[-limit:],
                        "analysed": sum(i["event"] == "decision_analyzed" for i in items),
                        "confirmed": sum(i["event"] == "decision_confirmed_by_human" for i in items),
                    },
                ),
            )
        )
    for rep in decision_reports:
        primary = rep["result"]["options"][0]["name"] if rep.get("result") else "decision"
        out.append(Section(f"Decision analysis: {fmt_value(primary)}", rep, required=False))
    return out


# -- report wrappers ---------------------------------------------------------------------------


def finance_report(
    profile: Profile,
    *,
    as_of: date,
    generated_on: str,
    tax_pack: RulePack | None = None,
    regime: str = "new",
    tax_year: str | None = None,
) -> RenderedReport:
    return render_report(
        "finance",
        "Finance report",
        finance_sections(profile, as_of=as_of, tax_pack=tax_pack, regime=regime, tax_year=tax_year),
        as_of=as_of,
        generated_on=generated_on,
        disclaimer=DISCLAIMERS["finance"],
    )


def health_report(profile: Profile, *, as_of: date, generated_on: str) -> RenderedReport:
    return render_report(
        "health",
        "Health report",
        health_sections(profile, as_of=as_of),
        as_of=as_of,
        generated_on=generated_on,
        disclaimer=DISCLAIMERS["health"],
    )


def lifestyle_report(profile: Profile, *, as_of: date, generated_on: str) -> RenderedReport:
    return render_report(
        "lifestyle",
        "Lifestyle report",
        lifestyle_sections(profile, as_of=as_of),
        as_of=as_of,
        generated_on=generated_on,
        disclaimer=DISCLAIMERS["health"],
    )


def career_report(
    profile: Profile, *, as_of: date, generated_on: str, tax_pack: RulePack | None = None, regime: str = "new"
) -> RenderedReport:
    return render_report(
        "career",
        "Career report",
        career_sections(profile, as_of=as_of, tax_pack=tax_pack, regime=regime),
        as_of=as_of,
        generated_on=generated_on,
        disclaimer=DISCLAIMERS["career"],
    )


def decisions_report(
    profile: Profile, *, as_of: date, generated_on: str, decision_reports: Sequence[dict[str, Any]] = ()
) -> RenderedReport:
    return render_report(
        "decisions",
        "Decisions report",
        decisions_sections(profile, as_of=as_of, decision_reports=decision_reports),
        as_of=as_of,
        generated_on=generated_on,
        disclaimer=DISCLAIMERS["decisions"],
    )


def life_plan_report(
    profile: Profile,
    *,
    as_of: date,
    generated_on: str,
    decision_reports: Sequence[dict[str, Any]] = (),
    regime: str = "new",
    tax_pack: RulePack | None = None,
) -> RenderedReport:
    """All areas linked: an at-a-glance table, every section, and at most 3 next steps."""
    areas = [
        ("Finance", finance_sections(profile, as_of=as_of, tax_pack=tax_pack, regime=regime)),
        ("Health", health_sections(profile, as_of=as_of)),
        ("Lifestyle", lifestyle_sections(profile, as_of=as_of)),
        ("Career", career_sections(profile, as_of=as_of, tax_pack=tax_pack, regime=regime)),
        ("Decisions", decisions_sections(profile, as_of=as_of, decision_reports=decision_reports)),
    ]
    all_sections = [s for _, secs in areas for s in secs]
    overall = worst(s.disposition for s in all_sections) or "HOLD"
    lines = [
        "# Life plan",
        "",
        (
            f"As of {as_of.isoformat()}. Generated on {generated_on}. Overall status: "
            f"{overall} ({DISPOSITION_WORDS[overall]})."
        ),
        "",
        *(f"> {DISCLAIMERS[k]}" for k in ("finance", "health", "decisions")),
        "",
        "## At a glance",
        "",
        "| Area | Status | First open item |",
        "|---|---|---|",
    ]
    for name, secs in areas:
        disp = worst(s.disposition for s in secs) or "not provided"
        acts = sorted((a for s in secs for a in section_actions(s)), key=lambda a: a[0])
        first = fmt_value(acts[0][1]).replace("|", "\\|") if acts else "nothing open"
        lines.append(f"| {name} | {disp} | {first} |")
    lines.append("")
    for name, secs in areas:
        lines += [f"## {name}", ""]
        for s in secs:
            lines += [*section_lines(s, 3), ""]
    lines += ["## Next steps", ""]
    lines += [f"{i}. {fmt_value(t)}" for i, t in enumerate(next_steps(all_sections), 1)] or [
        "- Nothing open."
    ]
    return RenderedReport(
        "life-plan", "Life plan", finish("\n".join(lines) + "\n"), overall, tuple(all_sections)
    )
