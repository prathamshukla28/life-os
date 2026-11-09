"""Synthetic persona for LM09 report / CLI tests (no real personal data).

Reuses earlier worked numbers: salary Rs 13,00,000 new regime -> tax 26,000; payroll
deductions 21,600 -> take-home 1,04,366.67/month; the Rs 60,000 course decision.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from life_os.life_mentor.decision_engine import (
    DECISION_JOURNAL,
    Option,
    analyze_decision,
    benefits,
    load_rates_pack,
    situation_from_profile,
)
from life_os.life_mentor.finance import load_tax_pack, take_home
from life_os.life_mentor.lifestyle import HabitTracker
from life_os.life_mentor.profile import Profile
from life_os.life_mentor.security import PrivateStore
from life_os.life_mentor.tracking import MetricLog

D = Decimal
AS_OF = date(2026, 10, 11)
NOW = "2026-10-11T00:00:00Z"
CATEGORIES = ("identity", "finance", "health", "career", "lifestyle", "goals")


def clock() -> str:
    return NOW


def build_persona(home: Path) -> Profile:
    store = PrivateStore(home)
    prof = Profile(store, clock=clock)
    for c in CATEGORIES:
        prof.consent.grant(c, purpose="synthetic persona")
    facts: list[tuple[str, Any, str, str, str]] = [
        ("gross_salary_annual", D(1300000), "finance", "VERIFIED", "Form 16 (synthetic)"),
        ("payroll_deductions_annual", D(21600), "finance", "VERIFIED", "payslip (synthetic)"),
        ("essential_spend_monthly", D(55000), "finance", "USER_ESTIMATE", "bank app"),
        ("discretionary_spend_monthly", D(10000), "finance", "USER_ESTIMATE", "bank app"),
        ("emergency_fund_balance", D(300000), "finance", "VERIFIED", "statement"),
        ("emergency_fund_months", 6, "finance", "USER_ESTIMATE", "chosen"),
        ("assets", {"savings": D(300000), "ppf": D(200000)}, "finance", "VERIFIED", "statements"),
        ("liabilities", {"car_loan": D(150000)}, "finance", "VERIFIED", "loan statement"),
        ("age", 35, "identity", "VERIFIED", "id card"),
        ("body_weight_kg", D(65), "health", "USER_ESTIMATE", "home scale"),
        ("medications", [], "health", "USER_ESTIMATE", "self report"),
        ("conditions", [], "health", "USER_ESTIMATE", "self report"),
        ("medical_limitations", False, "health", "USER_ESTIMATE", "self report"),
        ("goal_upskill", "learn data science", "goals", "USER_ESTIMATE", "stated"),
        ("role", "data analyst", "career", "VERIFIED", "offer letter"),
        ("years_experience", 4, "career", "USER_ESTIMATE", "self"),
        ("skills", ["sql", "python"], "career", "USER_ESTIMATE", "self"),
        ("industry", "fintech", "career", "USER_ESTIMATE", "self"),
        ("location", "Pune", "career", "USER_ESTIMATE", "self"),
        ("exercise_experience", "beginner", "lifestyle", "USER_ESTIMATE", "self"),
        ("exercise_days_per_week", 5, "lifestyle", "USER_ESTIMATE", "self"),
        ("exercise_minutes_per_session", 60, "lifestyle", "USER_ESTIMATE", "self"),
        ("exercise_equipment", ["none"], "lifestyle", "USER_ESTIMATE", "self"),
    ]
    for key, value, cat, prov, basis in facts:
        prof.set_fact(key, value, cat, prov, basis)
    habits = HabitTracker(store, clock=clock)
    habits.add_habit("walk", "walk 20 min", started_on=AS_OF - timedelta(days=6))
    for i in range(7):
        habits.check_in("walk", AS_OF - timedelta(days=6 - i), done=(i != 3), as_of=AS_OF)
    log = MetricLog(store, clock=clock)
    for i, kg in enumerate(("70.4", "70.2", "70.1")):
        log.record(
            "weight_kg",
            D(kg),
            AS_OF - timedelta(days=4 - 2 * i),
            provenance="USER_ESTIMATE",
            basis="home scale",
        )
    store.journal(DECISION_JOURNAL, clock=clock).append(
        {
            "event": "decision_analyzed",
            "decision_id": "a" * 64,
            "options": ["data_course", "do_nothing_keep_cash_low_risk"],
            "dispositions": {"data_course": "HOLD", "do_nothing_keep_cash_low_risk": "SHIP"},
            "overall_disposition": "HOLD",
            "recommendation_status": "recommend",
            "recommended": "do_nothing_keep_cash_low_risk",
        }
    )
    return prof


def course_decision(prof: Profile) -> dict[str, Any]:
    th = take_home(
        load_tax_pack(),
        as_of=AS_OF,
        regime="new",
        gross_salary=prof.get("gross_salary_annual"),
        payroll_deductions=prof.get("payroll_deductions_annual"),
    )
    course = Option(
        name="data_course",
        kind="course",
        upfront_cost=D(60000),
        recurring_monthly_cost=D(0),
        horizon_months=12,
        benefit_monthly=benefits(
            D(0), D(5000), D(10000), basis="salary uplift (own estimate)", evidence_class="HYPOTHESIS"
        ),
        downside_loss=D(60000),
        goal_tags=("goal_upskill",),
    )
    return analyze_decision(
        course,
        situation=situation_from_profile(prof, take_home_report=th),
        rates=load_rates_pack(),
        as_of=AS_OF,
    )
