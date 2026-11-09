# Vendored from the Asur monorepo (asur/core/identity.py).
# Copyright (c) 2026 prathamshukla28. Licensed under the MIT License (see LICENSE).
"""Local identity - ASUR-LOCAL-01.

Identity in ASUR is derived ONLY from the local machine:
  - the OS user (getpass.getuser / `whoami`)
  - git config user.name and user.email (local or global)

There is NO remote auth, NO GitHub login, NO network call, and NO
cryptographic key. This replaces Trinity's SSH-signature authorship proof
with "trust the local git/OS identity". Any local user can clear gates;
the point is not to prove *who you are to a server* but to record, honestly,
*who did what on this machine* so that producer != approver can be checked
(see core/gate.py).

Everything here is best-effort and fail-soft on the identity *fields*
(a missing git name is recorded as empty, not an error) - but the OS user
is always available. Gates fail CLOSED elsewhere; identity itself never
blocks, it only records.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass

__all__ = ["Identity", "get_identity"]


@dataclass(frozen=True)
class Identity:
    """Who performed an action, as seen by this local machine."""

    local_user: str
    git_name: str
    git_email: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    def principal(self) -> str:
        """A single stable string used for producer != approver comparison.

        We prefer git_email (most specific and stable), then git_name, then
        fall back to the OS user. Never empty: local_user is always present.
        """
        return self.git_email or self.git_name or self.local_user


def _os_user() -> str:
    # getpass.getuser consults env (LOGNAME/USER/...) then the password db.
    # It never hits the network. Guard against the rare empty/raising case.
    try:
        user = getpass.getuser()
    except Exception:  # guard the rare empty/raising case, stay key-free
        user = ""
    return user or os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def _git_config(key: str) -> str:
    """Read one git config value locally. Returns "" if git/value absent.

    Uses `git config --get`, which reads local then global config files on
    disk. No network. If git is not installed or the key is unset, we return
    an empty string rather than raising - identity recording must not block
    work; it is the *gate* that enforces policy.
    """
    git = shutil.which("git")
    if not git:
        return ""
    try:
        out = subprocess.run(
            [git, "config", "--get", key],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def get_identity() -> Identity:
    """Resolve the current local identity. Pure of network; safe to call often."""
    return Identity(
        local_user=_os_user(),
        git_name=_git_config("user.name"),
        git_email=_git_config("user.email"),
    )
