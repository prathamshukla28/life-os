"""LM07 tracking: append-only, hash-chained metric log; periodic review vs target."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from life_os.core.gate import BLOCK, HOLD, SHIP
from life_os.life_mentor.profile import ConsentRequired
from life_os.life_mentor.security import PrivateStore, find_forbidden
from life_os.life_mentor.tracking import METRICS, MIN_POINTS, MetricLog, review

D = Decimal
NOW = "2026-10-11T00:00:00Z"
START = date(2026, 9, 28)


def _log(tmp_path: Path, *cats: str) -> MetricLog:
    log = MetricLog(PrivateStore(tmp_path / "home"), clock=lambda: NOW)
    for c in cats:
        log.consent.grant(c, purpose="tracking")
    return log


def _clean(rep: dict) -> str:
    assert find_forbidden(rep) == [], find_forbidden(rep)
    assert set(rep) >= {"calculator", "inputs", "rules_used", "assumptions", "result", "gate"}
    return rep["gate"]["disposition"]


def _weights(log: MetricLog) -> None:
    for i in range(14):
        log.record(
            "weight_kg",
            D("80.0") - D("0.1") * i,
            START + timedelta(days=i),
            provenance="USER_ESTIMATE",
            basis="home scale",
        )


def test_metric_vocabulary() -> None:
    assert set(METRICS) == {
        "weight_kg",
        "sleep_hours",
        "exercise_minutes",
        "income",
        "expenses",
        "savings",
        "net_worth",
        "debt",
        "habit_completion_percent",
    }


def test_record_and_series_roundtrip(tmp_path: Path) -> None:
    log = _log(tmp_path, "health", "finance")
    _weights(log)
    series = log.series("weight_kg")
    assert len(series) == 14
    assert series[0] == (START, D("80.0")) and series[-1][1] == D("78.7")
    log.record("net_worth", D("-250000000.50"), START, provenance="VERIFIED", basis="statement")
    assert log.series("net_worth") == [(START, D("-250000000.50"))]
    assert log.journal.verify() == (True, None)
    raw = log.journal.path.read_text(encoding="utf-8")
    assert "[ACCOUNT]" not in raw


def test_weekly_review_trend_and_target(tmp_path: Path) -> None:
    log = _log(tmp_path, "health")
    _weights(log)
    rep = review(
        log,
        "weight_kg",
        period="weekly",
        as_of=START + timedelta(days=13),
        target=D(78),
        target_direction="at_most",
    )
    assert _clean(rep) == SHIP
    r = rep["result"]
    assert r["current"]["points"] == 7 and r["current"]["average"] == D("79.00")
    assert r["previous"]["average"] == D("79.70")
    assert r["change_in_average"] == D("-0.70")
    assert r["first_to_last_in_current"] == D("-0.60")
    assert r["target"] == {"value": D(78), "direction": "at_most", "met": False, "difference": D("1.00")}
    text = json.dumps(rep, default=str).casefold()
    assert "correlation is not causation" in text
    for bad in ("because of", "caused by", "thanks to", "due to your"):
        assert bad not in text


def test_review_insufficient_data_holds(tmp_path: Path) -> None:
    log = _log(tmp_path, "health")
    log.record("weight_kg", D(80), START, provenance="VERIFIED", basis="clinic")
    log.record("weight_kg", D(79), START + timedelta(days=1), provenance="VERIFIED", basis="clinic")
    rep = review(log, "weight_kg", period="weekly", as_of=START + timedelta(days=2))
    assert MIN_POINTS == 3
    assert _clean(rep) == HOLD
    assert "insufficient data" in json.dumps(rep).casefold()


def test_review_previous_window_missing_is_noted(tmp_path: Path) -> None:
    log = _log(tmp_path, "health")
    for i in range(4):
        log.record("sleep_hours", D(7), START + timedelta(days=i), provenance="USER_ESTIMATE", basis="diary")
    rep = review(log, "sleep_hours", period="weekly", as_of=START + timedelta(days=3))
    assert _clean(rep) == SHIP
    assert rep["result"]["change_in_average"] is None


@pytest.mark.parametrize(
    ("kw", "disp"),
    [
        ({"period": "yearly"}, BLOCK),
        ({"period": "weekly", "target": D(1)}, BLOCK),
        ({"period": "weekly", "target": D(1), "target_direction": "sideways"}, BLOCK),
    ],
)
def test_review_gates(tmp_path: Path, kw: dict, disp: str) -> None:
    log = _log(tmp_path, "health")
    _weights(log)
    assert _clean(review(log, "weight_kg", as_of=START + timedelta(days=13), **kw)) == disp


def test_no_silent_overwrite_and_correction_kept(tmp_path: Path) -> None:
    log = _log(tmp_path, "finance")
    log.record("savings", D(1000), START, provenance="VERIFIED", basis="bank")
    with pytest.raises(ValueError):
        log.record("savings", D(2000), START, provenance="VERIFIED", basis="bank")
    log.correct("savings", D(1500), START, provenance="VERIFIED", basis="bank, corrected")
    assert log.series("savings") == [(START, D(1500))]
    assert len(log.journal.entries()) == 2


@pytest.mark.parametrize(
    ("metric", "value", "exc"),
    [
        ("height_cm", D(170), ValueError),
        ("weight_kg", 80.5, TypeError),
        ("weight_kg", D(-1), ValueError),
    ],
)
def test_record_validation(tmp_path: Path, metric: str, value: object, exc: type) -> None:
    log = _log(tmp_path, "health", "finance")
    with pytest.raises(exc):
        log.record(metric, value, START, provenance="VERIFIED", basis="x")  # type: ignore[arg-type]


def test_record_rejects_unknown_provenance(tmp_path: Path) -> None:
    log = _log(tmp_path, "health")
    with pytest.raises(ValueError):
        log.record("weight_kg", D(80), START, provenance="UNKNOWN", basis="x")


def test_consent_required_for_record_and_read(tmp_path: Path) -> None:
    log = _log(tmp_path, "health")
    with pytest.raises(ConsentRequired):
        log.record("income", D(100000), START, provenance="VERIFIED", basis="payslip")
    _weights(log)
    log.consent.revoke("health", reason="pause")
    with pytest.raises(ConsentRequired):
        log.series("weight_kg")
    rep = review(log, "weight_kg", period="weekly", as_of=START + timedelta(days=13))
    assert _clean(rep) == HOLD
