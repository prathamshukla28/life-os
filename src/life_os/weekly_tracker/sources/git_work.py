"""Read-only git summary of one ISO week for a local repository.

Runs only two fixed commands with list arguments (no shell, no network, 10 s timeout):

    git -C <repo> rev-parse --is-inside-work-tree
    git -C <repo> log --since=<Mon>T00:00:00 --until=<Sun>T23:59:59 --numstat ...

Before ANY git subprocess the repository's own config files are read as text and the
repository is refused if they could make git run a program (``git_guard``, LM11).
Hardening: ``core.fsmonitor`` off, no signature checks, no external diff or textconv
drivers, pager off, ``GIT_DIR``-style overrides removed from the environment. Dates are
committer dates bucketed in local time (``TZ`` may be passed for reproducibility). Any
failure becomes ``{"status": "unavailable", "reason": ...}`` - never a crash. Commit
subjects are PII-redacted; only the repository's folder name is shown, not its path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from life_os.life_mentor.security import redact

from ..week import IsoWeek
from .git_guard import repo_config_problem

__all__ = ["GIT_TIMEOUT_SECONDS", "git_section", "is_test_path", "post_rename_path"]

GIT_TIMEOUT_SECONDS = 10.0
_RS, _US = "\x1e", "\x1f"
_DROP_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_EXTERNAL_DIFF",
)


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


_BRACE_RENAME = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


def post_rename_path(path: str) -> str:
    """git --numstat rename forms -> the new path: 'a/{x => y}/b' -> 'a/y/b', 'old => new'."""
    expanded = _BRACE_RENAME.sub(lambda m: m.group(2), path)
    if " => " in expanded:
        expanded = expanded.split(" => ")[-1]
    return re.sub(r"/{2,}", "/", expanded).strip("/")


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(post_rename_path(path)).parts
    name = parts[-1] if parts else ""
    return (
        any(p in ("tests", "test") for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.ts"))
    )


def _env(tz: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    env.update(
        {
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    if tz is not None:
        env["TZ"] = tz
    return env


def _run(args: list[str], env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
        check=False,
        shell=False,
    )


def _parse(stdout: str, week: IsoWeek) -> dict[str, Any]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    files: set[str] = set()
    tests: set[str] = set()
    ins = dels = 0
    for record in stdout.split(_RS):
        record = record.strip("\n")
        if not record:
            continue
        header, *lines = record.split("\n")
        fields = header.split(_US)
        if len(fields) != 3:
            continue
        short, day, subject = fields
        try:
            if not week.contains(date.fromisoformat(day)):
                continue
        except ValueError:
            continue
        c_files = c_ins = c_dels = 0
        for line in lines:
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            added, deleted, name = cols
            c_files += 1
            name = post_rename_path(name)
            files.add(name)
            if is_test_path(name):
                tests.add(name)
            c_ins += int(added) if added.isdigit() else 0
            c_dels += int(deleted) if deleted.isdigit() else 0
        by_day.setdefault(day, []).append(
            {
                "hash": short,
                "subject": redact(subject),
                "files": c_files,
                "insertions": c_ins,
                "deletions": c_dels,
            }
        )
        ins += c_ins
        dels += c_dels
    # git log lists newest first; show each day oldest first.
    ordered = {d: list(reversed(v)) for d, v in sorted(by_day.items())}
    return {
        "by_day": ordered,
        "totals": {
            "commits": sum(len(v) for v in ordered.values()),
            "active_days": len(ordered),
            "files_changed": len(files),
            "insertions": ins,
            "deletions": dels,
            "test_files_touched": len(tests),
        },
    }


def git_section(
    repo: str | os.PathLike[str] | None,
    week: IsoWeek,
    *,
    tz: str | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Commits per day, files changed, +/- lines and test files touched in *week*."""
    if repo is None:
        return _unavailable("no repository given")
    path = Path(repo)
    if not path.exists():
        return _unavailable("repository path does not exist")
    if not path.is_dir():
        return _unavailable("repository path is not a directory")
    git = shutil.which("git")
    if git is None:
        return _unavailable("git is not installed")
    loc, problem = repo_config_problem(path)
    if loc is None:
        return _unavailable(problem or "not a git repository")
    path = loc.work_tree
    env = _env(tz)
    # core.quotePath=false: otherwise non-ASCII paths come back C-quoted ("tests/caf\303\251.py")
    # and test-file detection and file counts see the wrong names.
    base = [
        git,
        "-C",
        str(path),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "log.showSignature=false",
        "-c",
        "core.quotePath=false",
    ]
    try:
        probe = _run([*base, "rev-parse", "--is-inside-work-tree"], env, timeout)
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return _unavailable("not a git repository")
        log = _run(
            [
                *base,
                "log",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--no-show-signature",
                f"--since={week.start.isoformat()}T00:00:00",
                f"--until={week.end.isoformat()}T23:59:59",
                "--date=format-local:%Y-%m-%d",
                f"--pretty=format:{_RS}%h{_US}%cd{_US}%s",
                "--numstat",
            ],
            env,
            timeout,
        )
    except subprocess.TimeoutExpired:
        return _unavailable("git timed out")
    except OSError:
        return _unavailable("git could not be run")
    if log.returncode != 0:
        if "does not have any commits" in log.stderr:
            log = subprocess.CompletedProcess(log.args, 0, "", "")
        else:
            return _unavailable("git log failed")
    parsed = _parse(log.stdout, week)
    return {"status": "ok", "reason": None, "repo_name": redact(path.name), **parsed}
