"""Life Mentor lifestyle (LM07): exercise plans checked against cited WHO 2020 adult
guidelines, sleep-log analysis against the cited AASM/SRS 2015 consensus, and habits
(at most 3 active). No diagnosis and no medical clearance."""

from __future__ import annotations

from .exercise import (
    ACTIVITY_REFS_PATH,
    CLINICIAN_PHYSIO_LINE,
    EQUIPMENT,
    EXPERIENCE_LEVELS,
    ExerciseInputs,
    check_activity,
    load_activity_refs,
    plan_exercise,
    plan_exercise_from_profile,
    progression,
)
from .habits import MAX_ACTIVE_HABITS, HabitTracker
from .sleep import (
    MIN_NIGHTS,
    SHORT_SLEEP_CLINICIAN_HOURS,
    SLEEP_REFS_PATH,
    SleepEntry,
    analyze_sleep,
    load_sleep_refs,
)

__all__ = [
    "ACTIVITY_REFS_PATH",
    "CLINICIAN_PHYSIO_LINE",
    "EQUIPMENT",
    "EXPERIENCE_LEVELS",
    "MAX_ACTIVE_HABITS",
    "MIN_NIGHTS",
    "SHORT_SLEEP_CLINICIAN_HOURS",
    "SLEEP_REFS_PATH",
    "ExerciseInputs",
    "HabitTracker",
    "SleepEntry",
    "analyze_sleep",
    "check_activity",
    "load_activity_refs",
    "load_sleep_refs",
    "plan_exercise",
    "plan_exercise_from_profile",
    "progression",
]
