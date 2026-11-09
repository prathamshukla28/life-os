"""LM09 `life-mentor` CLI. Exit codes: 0 SHIP/ok, 1 HOLD, 2 BLOCK or error."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from life_os.life_mentor.cli import main
from tests.lm_persona import build_persona

FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "lm"
    monkeypatch.setenv("LIFE_MENTOR_HOME", str(h))
    return h


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["--as-of", "2026-10-11", "--generated-on", "2026-10-11", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_read_commands_on_missing_store(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ("consent", "list"),
        ("fact", "list"),
        ("report", "finance"),
        ("journal", "verify"),
        ("fact", "set", "x", "1", "--category", "finance"),
    ):
        code, _, err = run(capsys, *argv)
        assert code == 2 and "life-mentor init" in err
    assert not home.exists()


def test_init_consent_fact_flow(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, "init")[0] == 0
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert run(capsys, "init")[0] == 0  # idempotent
    code, _, err = run(
        capsys, "fact", "set", "gross_salary_annual", "1300000", "--category", "finance", "--basis", "Form 16"
    )
    assert code == 2 and "consent" in err
    assert run(capsys, "consent", "grant", "finance", "--purpose", "tax")[0] == 0
    assert (
        run(
            capsys,
            "fact",
            "set",
            "gross_salary_annual",
            "1300000",
            "--category",
            "finance",
            "--provenance",
            "verified",
            "--basis",
            "Form 16",
        )[0]
        == 0
    )
    assert (
        run(
            capsys,
            "fact",
            "set",
            "payroll_deductions_annual",
            "21600.00",
            "--category",
            "finance",
            "--provenance",
            "verified",
            "--basis",
            "payslip",
        )[0]
        == 0
    )
    assert (
        run(
            capsys,
            "fact",
            "set",
            "assets",
            '{"ppf": 200000.50}',
            "--category",
            "finance",
            "--json",
            "--basis",
            "statement",
        )[0]
        == 0
    )
    code, out, _ = run(capsys, "fact", "list")
    assert code == 0 and "gross_salary_annual" in out and "VERIFIED" in out
    code, out, _ = run(capsys, "consent", "list")
    assert "finance: granted" in out and "health: not granted" in out
    code, out, _ = run(capsys, "tax", "--regime", "new")
    assert code == 0 and "26,000" in out
    code, out, _ = run(capsys, "take-home", "--regime", "new")
    assert code == 0 and "1,04,366.67" in out
    assert run(capsys, "tax", "--regime", "new", "--tax-year", "2026-27")[0] == 2
    assert (
        run(
            capsys,
            "fact",
            "correct",
            "gross_salary_annual",
            "1275000",
            "--provenance",
            "verified",
            "--basis",
            "Form 16 revised",
        )[0]
        == 0
    )
    assert "0" in run(capsys, "tax", "--regime", "new")[1]
    code, out, _ = run(capsys, "profile", "missing", "--for", "finance")
    assert code == 1 and "essential_spend_monthly" in out
    code, out, _ = run(capsys, "journal", "verify")
    assert code == 0 and "consent.jsonl: ok" in out


def test_fact_value_parsing(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "init")
    run(capsys, "consent", "grant", "identity", "--purpose", "x")
    assert run(capsys, "fact", "set", "age", "35", "--category", "identity")[0] == 0
    assert (
        run(
            capsys,
            "fact",
            "set",
            "nick",
            "--category",
            "identity",
            "--provenance",
            "unknown",
            "--basis",
            "not sure",
        )[0]
        == 0
    )
    assert run(capsys, "fact", "set", "bad", "{oops", "--category", "identity", "--json")[0] == 2
    code, out, _ = run(capsys, "profile", "export")
    assert code == 0
    data = json.loads(out)
    assert data["facts"]["age"]["value"] == 35
    assert data["facts"]["age"]["provenance"] == "USER_ESTIMATE"
    assert data["facts"]["nick"]["value"] is None


def test_export_out_file_and_delete(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "init")
    run(capsys, "consent", "grant", "finance", "--purpose", "x")
    run(capsys, "fact", "set", "rent", "20000", "--category", "finance")
    out = tmp_path / "export.json"
    assert run(capsys, "profile", "export", "--out", str(out))[0] == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert run(capsys, "profile", "export", "--out", str(out))[0] == 2  # no overwrite
    assert run(capsys, "fact", "delete", "finance")[0] == 2  # needs --yes
    assert run(capsys, "fact", "delete", "finance", "--yes")[0] == 0
    assert "rent" not in run(capsys, "fact", "list")[1]


def test_consent_revoke_and_bad_category(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "init")
    run(capsys, "consent", "grant", "health", "--purpose", "x")
    assert run(capsys, "consent", "revoke", "health")[0] == 0
    assert "health: not granted" in run(capsys, "consent", "list")[1]
    with pytest.raises(SystemExit):
        main(["consent", "grant", "wealth", "--purpose", "x"])


def test_decide_confirm_and_reports(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    build_persona(home)
    options = tmp_path / "options.json"
    options.write_text(
        json.dumps(
            {
                "regime": "new",
                "option": {
                    "name": "data_course",
                    "kind": "course",
                    "upfront_cost": 60000,
                    "recurring_monthly_cost": 0,
                    "horizon_months": 12,
                    "downside_loss": 60000,
                    "goal_tags": ["goal_upskill"],
                    "benefit_monthly": {
                        "conservative": 0,
                        "base": 5000,
                        "optimistic": 10000,
                        "basis": "own estimate",
                        "evidence_class": "HYPOTHESIS",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    code, out, _ = run(capsys, "decide", str(options))
    assert code == 1 and "4,273.47" in out
    decision_id = next(
        line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("Decision id: ")
    )
    code, out, _ = run(capsys, "confirm", decision_id, "data_course")
    assert code == 0 and "executed: no" in out
    assert run(capsys, "confirm", decision_id, "nonexistent")[0] == 2
    assert run(capsys, "confirm", "f" * 64, "data_course")[0] == 2
    code, out, _ = run(capsys, "report", "decisions")
    assert "decision_confirmed_by_human" in out
    target = tmp_path / "plan.md"
    code, out, _ = run(capsys, "report", "life-plan", "--out", str(target))
    assert code in (1, 2) and target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert run(capsys, "report", "life-plan", "--out", str(target))[0] == 2
    assert run(capsys, "report", "life-plan", "--out", str(target), "--force")[0] in (1, 2)


def test_import_commands(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "init")
    run(capsys, "consent", "grant", "finance", "--purpose", "x")
    code, out, _ = run(
        capsys,
        "import",
        "bank-csv",
        str(FIX / "lm_bank_statement.csv"),
        "--mapping",
        str(FIX / "lm_bank_mapping.json"),
    )
    assert code == 1 and "dry run" in out.casefold()
    assert not (home / "integrations").exists()
    run(
        capsys,
        "import",
        "bank-csv",
        str(FIX / "lm_bank_statement.csv"),
        "--mapping",
        str(FIX / "lm_bank_mapping.json"),
        "--apply",
        "--allow-partial",
    )
    assert (home / "integrations").is_dir()
    code, out, _ = run(
        capsys,
        "import",
        "amfi-nav",
        str(FIX / "lm_navall_sample.txt"),
        "--holdings",
        str(FIX / "lm_holdings.json"),
    )
    assert code == 0 and "12,592.20" in out
