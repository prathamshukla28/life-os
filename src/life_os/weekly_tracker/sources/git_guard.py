"""Refuse repositories whose LOCAL git config could make git run commands (LM11).

A repository-local ``.git/config`` with e.g. ``core.fsmonitor = <cmd>`` runs that command
on ``git rev-parse`` even with ``-c core.fsmonitor=false`` (observed with git 2.50.1), and
``GIT_CONFIG_*`` / ``GIT_INDEX_FILE`` do not stop it. So before ANY git subprocess we find
the git directory without running git and read its config files as text:

  * walk up from the given path to the first ``.git`` (directory, or a file holding
    ``gitdir: <path>``); a ``commondir`` file adds the common git dir (linked worktrees);
  * read ``config`` and ``config.worktree`` of both with a minimal parser (sections,
    ``[section "sub"]``, old ``[section.sub]``, ``key = value``, comments, quoted values,
    backslash continuations); keys are case-insensitive;
  * refuse if any key whose value can name a program, command, hook, pager, filter or
    included file is present (``is_dangerous``); declarative keys - commit signing set up
    with ``gpg.format`` / ``user.signingKey`` / ``gpg.ssh.allowedSignersFile`` included -
    are allowed. Anything unreadable or unparseable is refused (fail closed).

The user's own global/system config is trusted (it is theirs, not the repository's).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitConfigError",
    "GitLocation",
    "dangerous_keys",
    "find_git_dir",
    "is_dangerous",
    "parse_git_config",
    "repo_config_problem",
]

REFUSAL = "repository has a custom git config that could run commands"
_SECTION = re.compile(r'\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\\n]|\\.)*)")?\s*\]')
_KEY = re.compile(r"([A-Za-z][A-Za-z0-9-]*)\s*(?:=(.*))?$")

# What counts as "could run a program" follows `git help --config` (git 2.50). Only keys
# whose VALUE names a program, shell command, hook or extra config file are refused;
# purely declarative keys are allowed - in particular commit signing (gpg.format,
# user.signingKey, gpg.ssh.allowedSignersFile, gpg.ssh.revocationFile, gpg.minTrustLevel,
# commit/tag/push.gpgSign), so a repository that signs its commits is not refused.

# Every key in these sections is refused: include/includeIf pull in files this guard has
# not read, pager.<cmd> is a command, and safe.* is a trust setting git only honours from
# the user's own config - a repository that sets it is trying to widen its own trust.
_WHOLE_SECTIONS = frozenset({"include", "includeif", "pager", "safe"})
# Key-name endings git uses for "this value is run": gpg.program, gpg.<format>.program,
# gpg.ssh.defaultKeyCommand, core.sshCommand, core.alternateRefsCommand,
# diff.<driver>.command, trailer.<key>.command/.cmd, tar.<format>.command,
# difftool/mergetool/browser/man/guitool.<tool>.cmd, sendemail.toCmd/ccCmd/headerCmd/
# sendmailCmd, credential[.<url>].helper, uploadpack.packObjectsHook,
# gc.recentObjectsHook. Matching the ending also covers keys added by later git versions.
_COMMAND_SUFFIXES = ("program", "command", "cmd", "hook", "helper")
# Sections whose key NAMES are free-form or boolean flags, so a name ending in "cmd" or
# "hook" says nothing about its value (advice.ignoredHook, alias.lgcmd). An alias only
# runs when invoked by name, and Weekly Tracker runs fixed built-in commands only.
_NAME_ONLY_SECTIONS = frozenset({"advice", "alias"})
# section -> keys that run a program, with or without a <name> subsection.
_SECTION_KEYS: dict[str, frozenset[str]] = {
    "core": frozenset({"fsmonitor", "pager", "hookspath", "editor", "gitproxy", "askpass"}),
    "diff": frozenset({"external", "textconv"}),
    "merge": frozenset({"driver"}),
    "filter": frozenset({"clean", "smudge", "process"}),
    "difftool": frozenset({"path"}),
    "mergetool": frozenset({"path"}),
    "browser": frozenset({"path"}),
    "man": frozenset({"path"}),
    "remote": frozenset({"uploadpack", "receivepack", "vcs"}),
    "submodule": frozenset({"update"}),  # "!command" runs it
    "sequence": frozenset({"editor"}),
    "interactive": frozenset({"difffilter"}),
    "imap": frozenset({"tunnel"}),
    "instaweb": frozenset({"httpd"}),
    "sendemail": frozenset({"smtpserver"}),  # an absolute path is run as sendmail
}


class GitConfigError(ValueError):
    """The git dir or a config file could not be located, read or parsed."""


@dataclass(frozen=True)
class GitLocation:
    work_tree: Path
    git_dir: Path
    config_files: tuple[Path, ...]


def find_git_dir(start: Path) -> GitLocation | None:
    """Locate the repository for *start* without running git (None if there is none)."""
    here = start.resolve()
    for d in (here, *here.parents):
        cand = d / ".git"
        if cand.is_dir():
            git_dir = cand
        elif cand.is_file():
            text = cand.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                raise GitConfigError(".git file without gitdir")
            target = Path(text[len("gitdir:") :].strip())
            git_dir = (target if target.is_absolute() else d / target).resolve()
            if not git_dir.is_dir():
                raise GitConfigError("gitdir points nowhere")
        else:
            continue
        dirs = [git_dir]
        common = git_dir / "commondir"
        if common.is_file():
            ref = Path(common.read_text(encoding="utf-8").strip())
            dirs.append((ref if ref.is_absolute() else git_dir / ref).resolve())
        files = tuple(f for g in dirs for f in (g / "config", g / "config.worktree") if f.exists())
        if any(not f.is_file() for f in files):
            # A FIFO or device here would block (or never end) the read below.
            raise GitConfigError("config is not a regular file")
        if not any(f.name == "config" for f in files):
            raise GitConfigError("repository has no readable config")
        return GitLocation(d, git_dir, files)
    return None


def parse_git_config(text: str) -> list[tuple[str, str | None, str]]:
    """[(section, subsection, key)] lower-cased (subsection kept as written).

    Values are scanned the way git scans them: ``"`` toggles quoting, ``\\`` escapes the
    next character, an unquoted ``;``/``#`` starts a comment (a backslash inside a comment
    does NOT continue the line), and a backslash immediately before the newline continues
    the value onto the next physical line - with the quote state carried over.
    """
    entries: list[tuple[str, str | None, str]] = []
    section: str | None = None
    sub: str | None = None
    in_value = False
    quoted = False
    if text.startswith("\ufeff"):  # git skips a UTF-8 BOM
        text = text[1:]
    for raw in text.splitlines():
        if in_value:
            in_value, quoted = _scan_value(raw, quoted)
            continue
        # Only left-strip: trailing whitespace after a final backslash must not be
        # mistaken for a line continuation.
        line = raw.lstrip()
        if not line.strip() or line[0] in "#;":
            continue
        if line.startswith("["):
            m = _SECTION.match(line)
            if m is None:
                raise GitConfigError("bad section header")
            name, sub = m.group(1).lower(), m.group(2)
            if sub is None and "." in name:
                name, sub = name.split(".", 1)
            section = name
            line = line[m.end() :].lstrip()
            if not line.strip() or line[0] in "#;":
                continue
        if section is None:
            raise GitConfigError("key outside a section")
        k = _KEY.match(line)
        if k is None:
            raise GitConfigError("bad key line")
        entries.append((section, sub, k.group(1).lower()))
        in_value, quoted = _scan_value(k.group(2) or "", False)
    if in_value or quoted:
        raise GitConfigError("unterminated value")
    return entries


def _scan_value(value: str, quoted: bool) -> tuple[bool, bool]:
    """(continues_on_next_line, still_quoted) after scanning one physical line of a value."""
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\":
            if i + 1 == len(value):
                return True, quoted
            i += 2
            continue
        if c == '"':
            quoted = not quoted
        elif c in ";#" and not quoted:
            break  # comment to end of line; nothing after it (not even "\\") counts
        i += 1
    if quoted:
        raise GitConfigError("unterminated quoted value")
    return False, quoted


def is_dangerous(section: str, sub: str | None, key: str) -> bool:
    """True if (lower-cased) *section*.[*sub*.]*key* can make git run a program."""
    del sub  # the subsection only names a driver, tool, remote or URL
    return (
        section in _WHOLE_SECTIONS
        or key == "textconv"  # in any section: diff.<driver>.textconv is the git one
        or key in _SECTION_KEYS.get(section, frozenset())
        or (section not in _NAME_ONLY_SECTIONS and key.endswith(_COMMAND_SUFFIXES))
    )


def dangerous_keys(entries: list[tuple[str, str | None, str]]) -> list[str]:
    """Dotted names of the *entries* that can make git run a program (see ``is_dangerous``)."""
    return [
        f"{section}.{sub + '.' if sub else ''}{key}"
        for section, sub, key in entries
        if is_dangerous(section, sub, key)
    ]


def repo_config_problem(path: Path) -> tuple[GitLocation | None, str | None]:
    """(location, None) if safe; (None, reason) if not a repo or refused."""
    try:
        loc = find_git_dir(path)
        if loc is None:
            return None, "not a git repository"
        for f in loc.config_files:
            if dangerous_keys(parse_git_config(f.read_text(encoding="utf-8"))):
                return None, REFUSAL
    except (OSError, UnicodeDecodeError, GitConfigError):
        return None, REFUSAL
    return loc, None
