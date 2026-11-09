"""Private on-disk store for personal data (LM02).

All Life Mentor personal data lives under ONE root directory:

    explicit argument  >  $LIFE_MENTOR_HOME  >  ~/.life_mentor

Guarantees (enforced here, not merely documented):
  - directories are 0700 and files 0600, regardless of the process umask;
  - an existing root that is group/world accessible, or is a symlink, is
    REFUSED (``InsecureStore``) rather than silently tightened - we never
    chmod a directory we did not create (it might be ``$HOME``);
  - writes are atomic: a temp file is created with ``O_CREAT|O_EXCL`` at mode
    0600, fsynced, then ``os.replace``-d over the target;
  - relative paths are confined to the root (no ``..``, no absolute paths).

Encryption at rest is delegated to macOS FileVault (SPEC-GAP LM-G1). Stdlib
only; no network.
"""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from life_os.core.canonical import canonicalize, parse_json

if TYPE_CHECKING:
    from .journal import Journal

__all__ = [
    "DEFAULT_HOME_DIRNAME",
    "DIR_MODE",
    "ENV_HOME",
    "FILE_MODE",
    "InsecureStore",
    "PrivateStore",
    "resolve_home",
    "write_file_atomic",
]

DEFAULT_HOME_DIRNAME = ".life_mentor"
ENV_HOME = "LIFE_MENTOR_HOME"
DIR_MODE = 0o700
FILE_MODE = 0o600


class InsecureStore(PermissionError):
    """The store root exists but is not private (or is a symlink). Fail closed."""


def resolve_home(
    explicit: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Return the Life Mentor home directory WITHOUT creating it.

    Precedence: *explicit* > ``$LIFE_MENTOR_HOME`` > ``~/.life_mentor``.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    environ = os.environ if env is None else env
    from_env = environ.get(ENV_HOME, "").strip()
    if from_env:
        return Path(from_env).expanduser()
    return Path.home() / DEFAULT_HOME_DIRNAME


def _is_private(mode: int) -> bool:
    return (stat.S_IMODE(mode) & 0o077) == 0


def _mkdir_private(path: Path) -> None:
    """mkdir at 0700, then re-lstat: refuse a symlink or a wrong mode (fail closed)."""
    os.mkdir(path, DIR_MODE)
    os.chmod(path, DIR_MODE)  # umask may have narrowed or (with umask 0) not
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise InsecureStore(f"{path} is not a real directory after creation; refusing")
    if stat.S_IMODE(st.st_mode) != DIR_MODE:
        raise InsecureStore(f"{path} has mode {stat.S_IMODE(st.st_mode):o} after creation; expected 700")


def _write_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
    os.fsync(fd)


def write_file_atomic(target: Path, data: bytes, *, overwrite: bool) -> None:
    """Write *data* to *target* at mode 0600: temp file in the same dir, fsync, rename.

    With ``overwrite=False`` the final step is ``os.link``, which fails with
    ``FileExistsError`` if *target* exists, so nothing is ever clobbered (no
    check-then-write race). A failure at any step removes the temp file, so no
    half-written export (which may hold personal data) is left behind. Used for
    user-chosen output paths (``--out``) outside the private store.
    """
    target.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        try:
            os.fchmod(fd, FILE_MODE)
            _write_fd(fd, data)
        finally:
            os.close(fd)
        if overwrite:
            os.replace(tmp, target)
        else:
            try:
                os.link(tmp, target)
            except FileExistsError:
                raise
            except OSError:
                # File systems without hard links: an exclusive create still never clobbers.
                xfd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
                try:
                    _write_fd(xfd, data)
                finally:
                    os.close(xfd)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    PrivateStore._fsync_dir(target.parent)


class PrivateStore:
    """A permission-checked, atomically-written directory of JSON/JSONL files."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = resolve_home(root)

    # -- directory handling -------------------------------------------------

    def ensure(self) -> PrivateStore:
        """Create the root at 0700, or verify an existing root is private."""
        root = self.root
        if root.is_symlink():
            raise InsecureStore(f"store root {root} is a symlink; refusing")
        if root.exists():
            st = root.stat()
            if not stat.S_ISDIR(st.st_mode):
                raise InsecureStore(f"store root {root} is not a directory")
            if not _is_private(st.st_mode):
                raise InsecureStore(
                    f"store root {root} has mode {stat.S_IMODE(st.st_mode):o}; "
                    f"expected 0700 (chmod 700 it yourself if this is intended)"
                )
            return self
        root.parent.mkdir(parents=True, exist_ok=True)
        _mkdir_private(root)
        return self

    def path(self, relpath: str) -> Path:
        """Resolve *relpath* inside the root; reject traversal and absolute paths."""
        if not relpath or not relpath.strip():
            raise ValueError("empty store path")
        pure = PurePosixPath(relpath)
        if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
            raise ValueError(f"store path {relpath!r} escapes the store root")
        if any(part.startswith(".") and part.endswith(".tmp") for part in pure.parts):
            raise ValueError(f"store path {relpath!r} uses a reserved temp name")
        target = self.root.joinpath(*pure.parts)
        root_real = os.path.realpath(self.root)
        target_real = os.path.realpath(target)
        if target_real != root_real and not target_real.startswith(root_real + os.sep):
            raise ValueError(f"store path {relpath!r} resolves outside the store root")
        return target

    def _ensure_parent(self, target: Path) -> None:
        self.ensure()
        rel = target.parent.relative_to(self.root)
        cur = self.root
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                raise InsecureStore(f"{cur} is a symlink inside the store; refusing")
            if not cur.exists():
                _mkdir_private(cur)
            elif not _is_private(cur.stat().st_mode):
                raise InsecureStore(f"{cur} is not private (expected 0700)")

    # -- file I/O -----------------------------------------------------------

    def write_bytes(self, relpath: str, data: bytes) -> Path:
        """Atomically write *data* to *relpath* with mode 0600."""
        target = self.path(relpath)
        self._ensure_parent(target)
        if target.is_symlink():
            raise InsecureStore(f"{target} is a symlink; refusing to write through it")
        tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        try:
            try:
                os.fchmod(fd, FILE_MODE)
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, target)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise
        self._fsync_dir(target.parent)
        return target

    def write_json(self, relpath: str, obj: Any) -> Path:
        """Atomically write *obj* as canonical JSON (floats/NaN rejected upstream)."""
        return self.write_bytes(relpath, canonicalize(obj).encode("utf-8"))

    def read_json(self, relpath: str, default: Any = None) -> Any:
        target = self.path(relpath)
        if not target.exists():
            return default
        return parse_json(target.read_text(encoding="utf-8"))

    def journal(self, relpath: str, *, clock: Callable[[], str] | None = None) -> Journal:
        """Return the hash-chained journal stored at *relpath*."""
        from .journal import Journal

        target = self.path(relpath)

        def prepare() -> None:
            self._ensure_parent(target)

        anchor_rel = relpath + ".head"
        self.path(anchor_rel)

        def write_anchor(anchor: dict[str, Any]) -> None:
            self.write_json(anchor_rel, anchor)

        if clock is None:
            return Journal(target, prepare=prepare, write_anchor=write_anchor)
        return Journal(target, prepare=prepare, clock=clock, write_anchor=write_anchor)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            dfd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)
