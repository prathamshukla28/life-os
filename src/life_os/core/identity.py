# Vendored from the Asur monorepo (asur/core/identity.py).
# Copyright (c) 2026 prathamshukla28. Licensed under the MIT License (see LICENSE).
"""Local identity - ASUR-LOCAL-01.

Identity in ASUR is derived ONLY from the local machine:
  - the OS user (getpass.getuser / `whoami`)
  - git config user.name and user.email from the USER's git config (global and
    system) - never a repository's own .git/config, see ``_git_config``

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
import tempfile
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


# Environment variables that point git at a repository (and so at its untrusted
# .git/config). The user's own GIT_CONFIG_GLOBAL / GIT_CONFIG_NOSYSTEM are kept.
_REPO_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
)


def _git_config(key: str) -> str:
    """Read one value from the user's git config. Returns "" if git/value absent.

    Runs ``git config --get`` with no shell, from a fresh EMPTY temporary directory, with
    ``GIT_CEILING_DIRECTORIES`` set to that directory's parent and the repository-selecting
    variables (``GIT_DIR``, ``GIT_WORK_TREE``, ...) removed. So git finds no repository and
    reads only the system and global config - files the user owns - and never the
    ``.git/config`` of whatever repository the process happens to be started in. That file
    is not trusted (Weekly Tracker refuses repositories whose config could run commands, see
    ``weekly_tracker.sources.git_guard``); it also should not decide who "you" are. Routing
    this call through the guard instead would make identity depend on the current
    directory, and would still read a repository-local ``user.email`` set by someone else.

    No network. If git is not installed or the key is unset, we return an empty string
    rather than raising - identity recording must not block work; it is the *gate* that
    enforces policy.
    """
    git = shutil.which("git")
    if not git:
        return ""
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV}
    try:
        with tempfile.TemporaryDirectory(prefix="life-os-identity-") as tmp:
            empty = os.path.realpath(tmp)
            env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(empty)
            out = subprocess.run(
                [git, "config", "--get", key],
                cwd=empty,
                env=env,
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
