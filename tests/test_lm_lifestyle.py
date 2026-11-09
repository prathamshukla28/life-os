"""LM07 lifestyle: exercise plan vs cited WHO 2020, sleep log vs AASM/SRS 2015, habits."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.lifestyle import (
    ACTIVITY_REFS_PATH,
    MAX_ACTIVE_HABITS,
    SLEEP_REFS_PATH,
    ExerciseInputs,
    HabitTracker,
    SleepEntry,
    analyze_sleep,
    check_activity,
    load_activity_refs,
    load_sleep_refs,
    plan_exercise,
    plan_exercise_from_profile,
    progression,
)
from life_os.life_mentor.profile import ConsentRequired, Profile
from life_os.life_mentor.security import PrivateStore, find_forbidden

D = Decimal
AS_OF = date(2026, 10, 11)
NOW = "2026-10-11T00:00:00Z"
WHO_PUB = "https://www.who.int/publications/i/item/9789240015128"
AASM_PDF = "https://aasm.org/resources/pdf/pressroom/adult-sleep-duration-consensus.pdf"


@pytest.fixture(scope="module")
def arefs():
    return load_activity_refs()


@pytest.fixture(scope="module")
def srefs():
    return load_sleep_refs()


def _clean(rep: dict) -> str:
    assert find_forbidden(rep) == [], find_forbidden(rep)
    assert set(rep) >= {"calculator", "inputs", "rules_used", "assumptions", "result", "gate"}
    assert "guarantee" not in json.dumps(rep, default=str).casefold()
    return rep["gate"]["disposition"]


def _ex(**over: Any) -> ExerciseInputs:
    kw: dict[str, Any] = {
        "experience": "beginner",
        "days_per_week": 5,
        "minutes_per_session": 60,
        "equipment": ("none",),
        "medical_limitations": False,
        "conditions": (),
        "age": 30,
        "goals": ("general fitness",),
    }
    kw.update(over)
    return ExerciseInputs(**kw)


# --- rule packs -------------------------------------------------------------------


def test_activity_pack_cited(arefs) -> None:
    raw = json.loads(ACTIVITY_REFS_PATH.read_text(encoding="utf-8"))
    guideline = [v for v in raw["values"] if v["id"].startswith("who_adult_")]
    assert len(guideline) == 4
    for v in raw["values"]:
        assert v["source_tier"] == "primary" and v["source_sha256"]
    for v in guideline:
        assert v["source_url"] == WHO_PUB and v["source_tier"] == "primary"
        assert v["retrieved_on"] == "2026-10-11" and v["verified_by_human"] is False
        assert v["source_sha256"] and v["document_url"].startswith("https://iris.who.int/")
    assert arefs.get("who_adult_aerobic_moderate_minutes_per_week").value == {"min": D(150), "max": D(300)}
    assert arefs.get("who_adult_aerobic_vigorous_minutes_per_week").value == {"min": D(75), "max": D(150)}
    assert arefs.get("who_adult_muscle_strengthening_days_per_week").value == D(2)
    assert arefs.get("who_adult_age_band").value == {"min": D(18), "max": D(64)}


def test_sleep_pack_cited(srefs) -> None:
    raw = json.loads(SLEEP_REFS_PATH.read_text(encoding="utf-8"))
    [v] = [x for x in raw["values"] if x["id"] == "adult_sleep_minimum_hours"]
    assert v["source_url"] == AASM_PDF and v["source_tier"] == "primary"
    assert v["verified_by_human"] is False and v["source_sha256"]
    assert srefs.get("adult_sleep_minimum_hours").value == D(7)
    assert srefs.get("adult_sleep_age_band").value == {"min": D(18), "max": D(60)}


# --- exercise -----------------------------------------------------------------------


def test_plan_meets_who(arefs) -> None:
    rep = plan_exercise(_ex(), refs=arefs, as_of=AS_OF)
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["strength_sessions"] == 2 and r["aerobic_sessions"] == 3
    assert r["mobility_minutes_per_session"] == 6
    assert r["weekly_aerobic_moderate_minutes"] == 162  # 3 x (60 - 6)
    crit = {c["criterion"]: c for c in r["guideline_check"]}
    assert crit["aerobic"]["met"] is True and crit["muscle_strengthening"]["met"] is True
    assert len(r["week"]) == 5
    assert any("heuristic" in a.casefold() for a in rep["assumptions"])
    assert {c["id"] for c in rep["rules_used"]} >= {
        "who_adult_aerobic_moderate_minutes_per_week",
        "who_adult_muscle_strengthening_days_per_week",
    }


def test_plan_reports_gap_without_inventing(arefs) -> None:
    rep = plan_exercise(_ex(days_per_week=4, minutes_per_session=45), refs=arefs, as_of=AS_OF)
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["weekly_aerobic_moderate_minutes"] == 80  # 2 x (45 - 5)
    aer = {c["criterion"]: c for c in r["guideline_check"]}["aerobic"]
    assert aer["met"] is False and aer["moderate_equivalent_gap_minutes"] == D(70)


def test_advanced_gets_three_strength_days_and_one_day_plan(arefs) -> None:
    r = plan_exercise(
        _ex(experience="advanced", days_per_week=4, equipment=("gym",)), refs=arefs, as_of=AS_OF
    )["result"]
    assert r["strength_sessions"] == 3
    one = plan_exercise(_ex(days_per_week=1), refs=arefs, as_of=AS_OF)["result"]
    assert one["strength_sessions"] == 1 and one["aerobic_sessions"] == 0


@pytest.mark.parametrize(
    ("over", "disp"),
    [
        ({"medical_limitations": True}, HOLD),
        ({"conditions": ("knee pain",)}, HOLD),
        ({"medical_limitations": None}, HOLD),
        ({"age": None}, HOLD),
        ({"age": 70}, HOLD),
        ({"days_per_week": 8}, BLOCK),
        ({"days_per_week": 0}, BLOCK),
        ({"minutes_per_session": 0}, BLOCK),
        ({"minutes_per_session": 300}, BLOCK),
        ({"experience": "elite"}, BLOCK),
        ({"equipment": ("jetpack",)}, BLOCK),
    ],
)
def test_plan_gates(arefs, over: dict, disp: str) -> None:
    rep = plan_exercise(_ex(**over), refs=arefs, as_of=AS_OF)
    assert _clean(rep) == disp
    if over.get("medical_limitations") or over.get("conditions"):
        text = json.dumps(rep, default=str).casefold()
        assert "clear with your clinician or physiotherapist" in text
        assert "knee pain" not in text


def test_plan_stale_refs_hold(arefs) -> None:
    assert _clean(plan_exercise(_ex(), refs=arefs, as_of=date(2028, 1, 1))) == HOLD


def test_check_activity_combination(arefs) -> None:
    rep = check_activity(
        moderate_minutes=D(100), vigorous_minutes=D(30), strength_days=2, refs=arefs, as_of=AS_OF
    )
    assert _clean(rep) == SHIP
    aer = {c["criterion"]: c for c in rep["result"]["guideline_check"]}["aerobic"]
    assert aer["moderate_equivalent_minutes"] == D(160) and aer["met"] is True
    assert any("2 minutes of moderate" in a for a in rep["assumptions"])
    with pytest.raises(TypeError):
        check_activity(
            moderate_minutes=100.0, vigorous_minutes=D(0), strength_days=2, refs=arefs, as_of=AS_OF
        )


def test_progression_heuristic() -> None:
    rep = progression(current_weekly_minutes=D(60), target_weekly_minutes=D(150), weeks=12)
    assert _clean(rep) == SHIP
    weeks = rep["result"]["weekly_minutes"]
    assert weeks[:4] == [D(66), D(72), D(79), D(86)]
    assert weeks[-1] == D(150) and max(weeks) == D(150)
    for a, b in zip([D(60), *weeks], weeks, strict=False):
        assert b <= a * D("1.10")
    assert rep["result"]["label"] == "heuristic, not a cited guideline"
    assert _clean(progression(current_weekly_minutes=D(0), target_weekly_minutes=D(150), weeks=4)) == HOLD
    assert (
        _clean(
            progression(
                current_weekly_minutes=D(60),
                target_weekly_minutes=D(150),
                weeks=4,
                max_increase_percent=D(50),
            )
        )
        == BLOCK
    )


def _profile(tmp_path: Path, *cats: str) -> Profile:
    prof = Profile(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    for c in cats:
        prof.consent.grant(c, purpose="lifestyle")
    return prof


def test_plan_from_profile(tmp_path: Path, arefs) -> None:
    prof = _profile(tmp_path, "health", "identity")
    rep = plan_exercise_from_profile(
        prof,
        refs=arefs,
        as_of=AS_OF,
        experience="beginner",
        days_per_week=5,
        minutes_per_session=60,
        equipment=("none",),
    )
    assert _clean(rep) == HOLD
    assert {q["key"] for q in rep["questions"]} == {"age", "medical_limitations", "conditions"}
    prof.set_fact("age", 30, "identity", "VERIFIED", "id card")
    prof.set_fact("medical_limitations", False, "health", "USER_ESTIMATE", "self report")
    prof.set_fact("conditions", [], "health", "USER_ESTIMATE", "self report")
    rep = plan_exercise_from_profile(
        prof,
        refs=arefs,
        as_of=AS_OF,
        experience="beginner",
        days_per_week=5,
        minutes_per_session=60,
        equipment=("none",),
    )
    assert _clean(rep) == SHIP


# --- sleep ---------------------------------------------------------------------------


def _nights(beds: list[str], wakes: list[str]) -> list[SleepEntry]:
    start = date(2026, 10, 1)
    return [
        SleepEntry(start + timedelta(days=i), b, w) for i, (b, w) in enumerate(zip(beds, wakes, strict=True))
    ]


BEDS = ["23:00", "23:30", "22:30", "23:00", "00:00", "23:00", "23:30"]


def test_sleep_analysis_exact(srefs) -> None:
    rep = analyze_sleep(_nights(BEDS, ["06:30"] * 7), refs=srefs, as_of=AS_OF, age=30)
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["nights"] == 7
    assert r["average_hours"] == (D(51) / 7).quantize(D("0.01"))
    rel = [D(m) for m in (660, 690, 630, 660, 720, 660, 690)]  # minutes after noon
    mean = sum(rel) / 7
    sd = (sum((x - mean) ** 2 for x in rel) / 7).sqrt()
    assert r["bedtime_sd_minutes"] == sd.quantize(D("0.1"))
    assert r["wake_sd_minutes"] == D("0.0")
    assert r["nights_below_reference"] == 1  # only the 6.5 h night; 7.0 h is not below 7
    assert r["meets_reference_on_average"] is True
    assert "consistency" in " ".join(rep["assumptions"]).casefold()


def test_sleep_short_suggests_clinician(srefs) -> None:
    rep = analyze_sleep(_nights(["01:00"] * 7, ["06:00"] * 7), refs=srefs, as_of=AS_OF, age=30)
    assert _clean(rep) == HOLD
    assert rep["result"]["average_hours"] == D("5.00")
    text = json.dumps(rep, default=str).casefold()
    assert "clinician" in text and "diagnos" not in text.replace("no diagnosis", "")


def test_sleep_symptoms_hold_without_echo(srefs) -> None:
    rep = analyze_sleep(
        _nights(BEDS, ["06:30"] * 7), refs=srefs, as_of=AS_OF, age=30, symptoms="loud snoring and gasping"
    )
    assert _clean(rep) == HOLD
    assert "snoring" not in json.dumps(rep, default=str)


@pytest.mark.parametrize(
    ("entries", "kw", "disp"),
    [
        (_nights(BEDS[:3], ["06:30"] * 3), {"age": 30}, HOLD),
        (_nights(BEDS, ["06:30"] * 7), {"age": None}, HOLD),
        (_nights(BEDS, ["06:30"] * 7), {"age": 65}, HOLD),
        (_nights(["25:00"] + BEDS[1:], ["06:30"] * 7), {"age": 30}, BLOCK),
        (_nights(["06:30"] + BEDS[1:], ["06:30"] * 7), {"age": 30}, BLOCK),
        (
            [*_nights(BEDS, ["06:30"] * 7), SleepEntry(date(2026, 10, 1), "23:00", "07:00")],
            {"age": 30},
            BLOCK,
        ),
    ],
)
def test_sleep_gates(srefs, entries: list, kw: dict, disp: str) -> None:
    assert _clean(analyze_sleep(entries, refs=srefs, as_of=AS_OF, **kw)) == disp


# --- habits ----------------------------------------------------------------------------


def _tracker(tmp_path: Path, consent: bool = True) -> HabitTracker:
    store = PrivateStore(tmp_path / "home")
    t = HabitTracker(store, clock=lambda: NOW)
    if consent:
        t.consent.grant("lifestyle", purpose="habits")
    return t


def test_habit_limit_enforced(tmp_path: Path) -> None:
    assert MAX_ACTIVE_HABITS == 3
    t = _tracker(tmp_path)
    for h in ("walk", "read", "water"):
        assert _clean(t.add_habit(h, f"daily {h}", started_on=date(2026, 10, 1))) == SHIP
    rep = t.add_habit("stretch", "daily stretch", started_on=date(2026, 10, 1))
    assert _clean(rep) == HOLD
    assert "stretch" not in t.active_habits()
    t.retire_habit("water")
    assert _clean(t.add_habit("stretch", "daily stretch", started_on=date(2026, 10, 2))) == SHIP
    assert sorted(t.active_habits()) == ["read", "stretch", "walk"]
    assert _clean(t.add_habit("walk", "again", started_on=date(2026, 10, 2))) == BLOCK


def test_habit_checkins_streak_and_rate(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.add_habit("walk", "walk 20 min", started_on=date(2026, 10, 1))
    for i in range(10):
        d = date(2026, 10, 1) + timedelta(days=i)
        assert _clean(t.check_in("walk", d, done=(i != 5), as_of=date(2026, 10, 10))) == SHIP
    rep = t.summary(as_of=date(2026, 10, 10))
    assert _clean(rep) == SHIP
    [h] = rep["result"]["habits"]
    assert h["current_streak_days"] == 4
    assert h["completion_percent"] == D("90.00")
    assert (h["days_tracked"], h["days_done"]) == (10, 9)
    assert any("missing check-in" in a for a in rep["assumptions"])


@pytest.mark.parametrize(
    ("habit", "day", "disp"),
    [
        ("ghost", date(2026, 10, 3), BLOCK),
        ("walk", date(2026, 9, 30), BLOCK),
        ("walk", date(2026, 10, 20), BLOCK),
    ],
)
def test_checkin_gates(tmp_path: Path, habit: str, day: date, disp: str) -> None:
    t = _tracker(tmp_path)
    t.add_habit("walk", "walk", started_on=date(2026, 10, 1))
    assert _clean(t.check_in(habit, day, done=True, as_of=date(2026, 10, 10))) == disp


def test_duplicate_checkin_blocks_and_journal_verifies(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.add_habit("walk", "walk", started_on=date(2026, 10, 1))
    t.check_in("walk", date(2026, 10, 2), done=True, as_of=date(2026, 10, 10))
    assert _clean(t.check_in("walk", date(2026, 10, 2), done=False, as_of=date(2026, 10, 10))) == BLOCK
    assert t.journal.verify() == (True, None)


def test_habits_need_consent(tmp_path: Path) -> None:
    t = _tracker(tmp_path, consent=False)
    with pytest.raises(ConsentRequired):
        t.add_habit("walk", "walk", started_on=date(2026, 10, 1))


def test_summary_without_habits_holds(tmp_path: Path) -> None:
    assert _clean(_tracker(tmp_path).summary(as_of=AS_OF)) == HOLD
