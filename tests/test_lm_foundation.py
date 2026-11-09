"""LM00 foundation: the Life Mentor package skeleton, charter, and wiring."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LM = REPO / "src" / "life_os" / "life_mentor"

PLACEHOLDER_SUBPACKAGES = (
    "health",
    "finance",
    "career",
    "lifestyle",
    "decision_engine",
    "tracking",
    "reports",
    "integrations",
)
BUILT_SUBPACKAGES = ("profile", "security", "research")


def test_import_life_mentor_in_fresh_interpreter() -> None:
    out = subprocess.run(
        [sys.executable, "-c", "import life_os.life_mentor as lm; print(lm.DISPLAY_NAME)"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "Life Mentor"


@pytest.mark.parametrize("name", BUILT_SUBPACKAGES + PLACEHOLDER_SUBPACKAGES)
def test_subpackage_importable_with_docstring(name: str) -> None:
    mod = importlib.import_module(f"life_os.life_mentor.{name}")
    assert mod.__doc__ and mod.__doc__.strip(), f"life_mentor.{name} lacks a docstring"


@pytest.mark.parametrize("name", PLACEHOLDER_SUBPACKAGES)
def test_placeholder_docstring_names_its_phase(name: str) -> None:
    mod = importlib.import_module(f"life_os.life_mentor.{name}")
    assert "LM0" in (mod.__doc__ or "") or "LM1" in (mod.__doc__ or "")


def test_rules_directory_reserved() -> None:
    assert (LM / "rules").is_dir()


def test_charter_states_invariants_and_exclusions() -> None:
    charter = (LM / "docs" / "CHARTER.md").read_text(encoding="utf-8")
    low = charter.casefold()
    for needle in (
        "~/.life_mentor",
        "life_mentor_home",
        "verified",
        "user_estimate",
        "assumption",
        "unknown",
        "hold",
        "block",
        "no diagnosis",
        "no transactions",
        "no guaranteed returns",
        "no network",
        "filevault",
        "hash-chained",
        "consent",
    ):
        assert needle in low, f"CHARTER.md missing {needle!r}"


def test_pyproject_packages_life_os() -> None:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = ["life_os*"]' in text
    assert 'life-mentor = "life_os.life_mentor.cli:main"' in text
    assert "dependencies = []" in text


def test_ci_runs_mypy_and_tests() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "mypy" in ci
    assert "pytest" in ci
