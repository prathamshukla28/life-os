"""Shared test configuration.

Every test runs with ``LIFE_MENTOR_HOME`` / ``WEEKLY_TRACKER_HOME`` pointed at a
fresh temporary directory, so no test can ever read or write a real
``~/.life_mentor`` or ``~/.weekly_tracker`` on the developer's machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from life_os.life_mentor.security.store import ENV_HOME as LM_ENV
from life_os.weekly_tracker.planner import ENV_HOME as WT_ENV


@pytest.fixture(autouse=True)
def _isolated_homes(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    base = tmp_path_factory.mktemp("homes")
    monkeypatch.setenv(LM_ENV, str(base / "life_mentor"))
    monkeypatch.setenv(WT_ENV, str(base / "weekly_tracker"))
    yield base
