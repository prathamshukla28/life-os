"""Stdlib-only primitives vendored from the Asur monorepo.

- :mod:`life_os.core.gate`      fail-closed SHIP / HOLD / BLOCK dispositions
- :mod:`life_os.core.canonical` canonical JSON + SHA-256 content hashing
- :mod:`life_os.core.identity`  local (OS / git config) identity, no network
- :mod:`life_os.core.evidence`  evidence classes for honest claims
- :mod:`life_os.core.clock`     UTC ISO-8601 timestamps

Nothing in this package imports ``life_os.life_mentor`` or
``life_os.weekly_tracker`` (dependency direction is core -> life_mentor ->
weekly_tracker; enforced by ``tests/test_boundary.py``).
"""
