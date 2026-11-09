"""Life Mentor security (LM02): private store, hash-chained journal, redaction, scanner."""

from __future__ import annotations

from .journal import GENESIS_HASH, Journal, JournalTampered
from .redact import redact
from .scanner import (
    REPORT_FORBIDDEN_KEYS,
    REPORT_FORBIDDEN_PHRASES,
    ForbiddenFieldLeak,
    assert_report_clean,
    find_forbidden,
)
from .store import (
    DEFAULT_HOME_DIRNAME,
    DIR_MODE,
    ENV_HOME,
    FILE_MODE,
    InsecureStore,
    PrivateStore,
    resolve_home,
    write_file_atomic,
)

__all__ = [
    "DEFAULT_HOME_DIRNAME",
    "DIR_MODE",
    "ENV_HOME",
    "FILE_MODE",
    "GENESIS_HASH",
    "REPORT_FORBIDDEN_KEYS",
    "REPORT_FORBIDDEN_PHRASES",
    "ForbiddenFieldLeak",
    "InsecureStore",
    "Journal",
    "JournalTampered",
    "PrivateStore",
    "assert_report_clean",
    "find_forbidden",
    "redact",
    "resolve_home",
    "write_file_atomic",
]
