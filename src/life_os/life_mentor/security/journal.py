"""Hash-chained, append-only JSONL journal (LM02, ASUR-VERSION-01).

Each line is one canonical-JSON entry:

    {"index": n, "recorded_at": "...Z", "event": {...},
     "prev_hash": <entry_hash of line n-1, or 64 zeros>,
     "entry_hash": canonical_sha256(entry without "entry_hash")}

``verify()`` walks the file and returns ``(True, None)`` when every line parses,
indexes are contiguous, every ``prev_hash`` links to the previous
``entry_hash``, and every ``entry_hash`` recomputes. Otherwise it returns
``(False, i)`` with the FIRST broken index - an edited, deleted, inserted, or
garbage line is detected at the point it occurs.

``append()`` refuses (``JournalTampered``) to extend a broken chain: writing
on top of tampered history would launder it (ASUR-GATE-01, fail closed).

Tail truncation cannot be seen by a hash chain alone, so after every append a
sidecar HEAD ANCHOR ``<journal>.head`` = ``{"count": n, "head_hash": h}`` is
written atomically (through the private store when the journal comes from
``PrivateStore.journal``). ``verify()`` fails when the anchor is missing for a
non-empty journal, or its count / head hash disagree with the file. A crash
between the line append and the anchor write also fails verification - by
design (fail closed); recovery is a human decision.

Concurrency: ``append`` holds an exclusive ``flock`` on the journal file across
verify -> append -> anchor write, and ``verify``/``entries`` hold a shared one, so
two processes appending at once can no longer interleave and break the chain, and
a reader never sees a line whose anchor is not yet written. (``fcntl`` is POSIX;
on platforms without it the journal is single-writer only.)

Privacy: every string in an event is passed through ``redact()`` BEFORE it is
hashed, so raw PII never reaches the journal. Floats are rejected - money is
Decimal/paise and journals record keys and counts, never values.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

from life_os.core.canonical import canonical_sha256, canonicalize
from life_os.core.clock import utc_now_iso

from .redact import redact

_fcntl: ModuleType | None
try:  # POSIX advisory locks; absent on Windows.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX
    _fcntl = None

__all__ = ["GENESIS_HASH", "Journal", "JournalTampered"]

GENESIS_HASH = "0" * 64
_FILE_MODE = 0o600
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


@contextmanager
def _flock(fd: int, exclusive: bool) -> Iterator[None]:
    if _fcntl is None:  # pragma: no cover - non-POSIX
        yield
        return
    _fcntl.flock(fd, _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH)
    try:
        yield
    finally:
        _fcntl.flock(fd, _fcntl.LOCK_UN)


class JournalTampered(RuntimeError):
    """The chain failed verification; carries the first broken index."""

    def __init__(self, index: int) -> None:
        super().__init__(f"journal chain broken at entry {index}; refusing to append")
        self.index = index


def _sanitize(obj: Any) -> Any:
    """Redact every string and reject floats, recursively."""
    if isinstance(obj, bool) or obj is None or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        raise TypeError("journal events may not contain floats (use Decimal/str/int)")
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(f"journal event keys must be str, got {type(key).__name__}")
            out[redact(key)] = _sanitize(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [_sanitize(item) for item in obj]
    raise TypeError(f"journal events may not contain {type(obj).__name__}")


def _entry_hash(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    return canonical_sha256(body)


class Journal:
    """Append-only, hash-chained JSONL log at *path*."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        prepare: Callable[[], None] | None = None,
        clock: Callable[[], str] = utc_now_iso,
        write_anchor: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.anchor_path = self.path.with_name(self.path.name + ".head")
        self._prepare = prepare
        self._clock = clock
        self._write_anchor = write_anchor or self._write_anchor_local

    def _write_anchor_local(self, anchor: dict[str, Any]) -> None:
        tmp = self.anchor_path.with_name(f".{self.anchor_path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        try:
            _write_all(fd, canonicalize(anchor).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.anchor_path)

    def _read_anchor(self) -> dict[str, Any] | None:
        if not self.anchor_path.exists():
            return None
        try:
            data = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # -- reading ------------------------------------------------------------

    def _raw_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        return [line for line in text.split("\n") if line != ""]

    @contextmanager
    def _shared(self) -> Iterator[None]:
        """Shared lock for readers (no-op when the journal does not exist yet)."""
        try:
            fd = os.open(self.path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            yield
            return
        try:
            with _flock(fd, exclusive=False):
                yield
        finally:
            os.close(fd)

    def entries(self) -> list[dict[str, Any]]:
        """All entries in order. Raises ``JournalTampered`` if the chain is broken."""
        with self._shared():
            ok, bad = self._verify()
            if not ok:
                raise JournalTampered(bad if bad is not None else 0)
            return [json.loads(line) for line in self._raw_lines()]

    def verify(self) -> tuple[bool, int | None]:
        """Return (True, None) if intact, else (False, first_bad_index)."""
        with self._shared():
            return self._verify()

    def _verify(self) -> tuple[bool, int | None]:
        prev = GENESIS_HASH
        for i, line in enumerate(self._raw_lines()):
            try:
                entry = json.loads(line)
            except ValueError:
                return False, i
            if not isinstance(entry, dict):
                return False, i
            if entry.get("index") != i or entry.get("prev_hash") != prev:
                return False, i
            try:
                recomputed = _entry_hash(entry)
            except (TypeError, ValueError):
                return False, i
            if entry.get("entry_hash") != recomputed:
                return False, i
            prev = recomputed
        n = len(self._raw_lines())
        anchor = self._read_anchor()
        if anchor is None:
            return (True, None) if n == 0 else (False, 0)
        count, head = anchor.get("count"), anchor.get("head_hash")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return False, 0
        if count != n:
            return False, min(count, n)
        if head != prev:
            return False, max(n - 1, 0)
        return True, None

    # -- writing ------------------------------------------------------------

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """Redact, chain, and append one event. Returns the stored entry."""
        if not isinstance(event, dict):
            raise TypeError("journal event must be a dict")
        clean = _sanitize(event)
        if self._prepare is not None:
            self._prepare()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW
        fd = os.open(self.path, flags, _FILE_MODE)
        try:
            with _flock(fd, exclusive=True):
                ok, bad = self._verify()
                if not ok:
                    raise JournalTampered(bad if bad is not None else 0)
                lines = self._raw_lines()
                prev = json.loads(lines[-1])["entry_hash"] if lines else GENESIS_HASH
                entry: dict[str, Any] = {
                    "index": len(lines),
                    "recorded_at": self._clock(),
                    "event": clean,
                    "prev_hash": prev,
                }
                entry["entry_hash"] = _entry_hash(entry)
                os.fchmod(fd, _FILE_MODE)
                _write_all(fd, canonicalize(entry).encode("utf-8"))  # one trailing LF
                os.fsync(fd)
                self._write_anchor({"count": entry["index"] + 1, "head_hash": entry["entry_hash"]})
        finally:
            os.close(fd)
        return entry
