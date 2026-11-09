"""Life Mentor career (LM08): career snapshot from consented facts, job-offer comparison via
LM04 take-home, course evaluation via the LM05 decision engine, salary benchmarks from
user-cited sources only, and a negotiation checklist. No bundled salary data, no score."""

from __future__ import annotations

from .education import UpliftScenario, evaluate_education
from .market import (
    BENCHMARK_MAX_AGE_DAYS,
    CAREER_KEYS,
    INSUFFICIENT_EVIDENCE,
    SalaryBenchmark,
    benchmark_summary,
    career_snapshot,
    negotiation_prep,
)
from .offers import (
    CRITERIA,
    JOB_PACKAGE_REQUIREMENTS,
    JobPackage,
    compare_job_offer,
    job_package_from_profile,
    job_package_questions,
)

__all__ = [
    "BENCHMARK_MAX_AGE_DAYS",
    "CAREER_KEYS",
    "CRITERIA",
    "INSUFFICIENT_EVIDENCE",
    "JOB_PACKAGE_REQUIREMENTS",
    "JobPackage",
    "SalaryBenchmark",
    "UpliftScenario",
    "benchmark_summary",
    "career_snapshot",
    "compare_job_offer",
    "evaluate_education",
    "job_package_from_profile",
    "job_package_questions",
    "negotiation_prep",
]
