"""LM02 security: private store, hash-chained journal, redaction, report scanner."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from life_os.life_mentor.security import (
    DEFAULT_HOME_DIRNAME,
    ForbiddenFieldLeak,
    InsecureStore,
    Journal,
    JournalTampered,
    PrivateStore,
    assert_report_clean,
    find_forbidden,
    redact,
    resolve_home,
)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# --- home resolution -------------------------------------------------------


def test_resolve_home_precedence(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    env = {"LIFE_MENTOR_HOME": str(tmp_path / "env")}
    assert resolve_home(explicit, env=env) == explicit
    assert resolve_home(None, env=env) == tmp_path / "env"
    default = resolve_home(None, env={})
    assert default == Path.home() / DEFAULT_HOME_DIRNAME
    assert DEFAULT_HOME_DIRNAME == ".life_mentor"


def test_resolve_home_does_not_create(tmp_path: Path) -> None:
    target = tmp_path / "nothing-here"
    resolve_home(target, env={})
    assert not target.exists()


# --- private store ---------------------------------------------------------


def test_store_dirs_0700_files_0600(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "home")
    path = store.write_json("profile/facts.json", {"a": 1})
    assert _mode(tmp_path / "home") == 0o700
    assert _mode(tmp_path / "home" / "profile") == 0o700
    assert _mode(path) == 0o600
    assert store.read_json("profile/facts.json") == {"a": 1}


def test_store_files_0600_even_with_permissive_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        store = PrivateStore(tmp_path / "home")
        path = store.write_json("x.json", {"b": 2})
    finally:
        os.umask(old)
    assert _mode(path) == 0o600
    assert _mode(tmp_path / "home") == 0o700


def test_store_atomic_overwrite_leaves_no_temp(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "home")
    store.write_json("f.json", {"v": 1})
    store.write_json("f.json", {"v": 2})
    assert store.read_json("f.json") == {"v": 2}
    leftovers = [p.name for p in (tmp_path / "home").iterdir() if p.name != "f.json"]
    assert leftovers == []


def test_store_read_missing_returns_default(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path / "home")
    assert store.read_json("absent.json", default={"d": 1}) == {"d": 1}


@pytest.mark.parametrize("bad", ["../escape.json", "/etc/passwd", "a/../../b.json", ""])
def test_store_rejects_path_traversal(tmp_path: Path, bad: str) -> None:
    store = PrivateStore(tmp_path / "home")
    with pytest.raises(ValueError):
        store.path(bad)


def test_store_refuses_existing_world_readable_root(tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    with pytest.raises(InsecureStore):
        PrivateStore(loose).ensure()


def test_store_accepts_existing_private_root(tmp_path: Path) -> None:
    tight = tmp_path / "tight"
    tight.mkdir()
    os.chmod(tight, 0o700)
    store = PrivateStore(tight)
    path = store.write_json("ok.json", {})
    assert path == tight / "ok.json" and path.exists()
    assert _mode(path) == 0o600 and _mode(tight) == 0o700
    assert store.read_json("ok.json") == {}


def test_store_refuses_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    os.chmod(real, 0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(InsecureStore):
        PrivateStore(link).ensure()


# --- journal ---------------------------------------------------------------


def _journal(tmp_path: Path) -> Journal:
    return PrivateStore(tmp_path / "home").journal("consent.jsonl")


def test_journal_chain_links_and_verifies(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    e0 = j.append({"event": "grant", "category": "finance"})
    e1 = j.append({"event": "revoke", "category": "finance"})
    assert e0["index"] == 0 and e1["index"] == 1
    assert e0["prev_hash"] == "0" * 64
    assert e1["prev_hash"] == e0["entry_hash"]
    assert j.verify() == (True, None)
    assert [e["event"]["event"] for e in j.entries()] == ["grant", "revoke"]
    assert _mode(j.path) == 0o600


def test_empty_journal_verifies(tmp_path: Path) -> None:
    assert _journal(tmp_path).verify() == (True, None)


def test_tampering_one_line_breaks_verify_at_that_index(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    for i in range(4):
        j.append({"event": "note", "n": i})
    lines = j.path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[2])
    rec["event"]["n"] = 99
    lines[2] = json.dumps(rec)
    j.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert j.verify() == (False, 2)


def test_deleting_a_line_breaks_verify(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    for i in range(3):
        j.append({"event": "note", "n": i})
    lines = j.path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    j.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, idx = j.verify()
    assert ok is False and idx == 1


def test_garbage_line_breaks_verify(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    j.append({"event": "note"})
    with j.path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    assert j.verify() == (False, 1)


def test_append_refuses_on_broken_chain(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    j.append({"event": "a"})
    j.append({"event": "b"})
    lines = j.path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["event"]["event"] = "z"
    lines[0] = json.dumps(rec)
    j.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalTampered):
        j.append({"event": "c"})


def test_journal_redacts_pii_before_hashing(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    j.append({"event": "note", "text": "mail me at someone@example.com"})
    raw = j.path.read_text(encoding="utf-8")
    assert "someone@example.com" not in raw
    assert j.verify() == (True, None)


def test_journal_rejects_float(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        _journal(tmp_path).append({"event": "x", "amount": 1.5})


# --- redaction -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("PAN is ABCDE1234F ok", "ABCDE1234F"),
        ("aadhaar 1234 5678 9012", "1234 5678 9012"),
        ("aadhaar 123456789012", "123456789012"),
        ("call +91 98765 43210", "98765 43210"),
        ("call +919876543210", "9876543210"),
        ("call 9876543210 now", "9876543210"),
        ("email a.b+c@example.co.in", "a.b+c@example.co.in"),
        ("acct 001234567890123", "001234567890123"),
        ("card 4111 1111 1111 1111", "4111 1111 1111 1111"),
        ("card 4111-1111-1111-1111", "4111-1111-1111-1111"),
    ],
)
def test_redact_masks_pii(text: str, secret: str) -> None:
    out = redact(text)
    assert secret not in out
    assert "[" in out


def test_redact_leaves_ordinary_text_and_dates() -> None:
    text = "Spent 4500 on 2026-10-11; goal year 2030; age 34."
    assert redact(text) == text


def test_redact_is_idempotent() -> None:
    text = "PAN ABCDE1234F, phone 9876543210, x@y.com"
    once = redact(text)
    assert redact(once) == once


# --- forbidden-field scanner -----------------------------------------------


def test_scanner_clean_report_passes() -> None:
    report = {"criteria": [{"name": "cost", "result": "within budget"}], "disposition": "HOLD"}
    assert find_forbidden(report) == []
    assert_report_clean(report)


@pytest.mark.parametrize(
    "report",
    [
        {"score": 87},
        {"rows": [{"overall_score": 1}]},
        {"summary": "This plan has guaranteed returns"},
        {"note": "PAN ABCDE1234F"},
        {"account_number": "x"},
    ],
)
def test_scanner_fails_closed(report: dict) -> None:
    assert find_forbidden(report)
    with pytest.raises(ForbiddenFieldLeak):
        assert_report_clean(report)


def test_scanner_does_not_flag_score_inside_words() -> None:
    assert find_forbidden({"underscored": "the scoreboard is underscored"}) == []
