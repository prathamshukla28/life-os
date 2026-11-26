"""Regression tests (part 4): git guard precision, facts integrity, identity, metrics CLI,
exit codes and rule-expiry messages.

Each test names the bug it pins. See CHANGELOG.md (0.1.0, "Found in the post-extraction
code read").
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from life_os.core.identity import get_identity
from life_os.life_mentor.cli import main as lm_main
from life_os.life_mentor.finance import load_tax_pack
from life_os.life_mentor.profile import FACTS_FILE, PROFILE_JOURNAL, ConsentLedger, Profile
from life_os.life_mentor.research import rules_gate
from life_os.life_mentor.security import JournalTampered, PrivateStore
from life_os.weekly_tracker.cli import main as wt_main
from life_os.weekly_tracker.sources.git_guard import dangerous_keys, parse_git_config
from life_os.weekly_tracker.sources.git_work import git_section
from life_os.weekly_tracker.week import IsoWeek

NOW = "2026-11-10T00:00:00Z"
W41 = IsoWeek(2026, 41)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _lm(tmp_path: Path, *args: str) -> int:
    return lm_main(["--home", str(tmp_path / "lm"), *args])


def _lm_with(tmp_path: Path, *categories: str) -> None:
    assert _lm(tmp_path, "init") == 0
    for cat in categories:
        assert _lm(tmp_path, "consent", "grant", cat, "--purpose", "test") == 0


def _git_env(**extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "F",
        "GIT_AUTHOR_EMAIL": "f@example.invalid",
        "GIT_COMMITTER_NAME": "F",
        "GIT_COMMITTER_EMAIL": "f@example.invalid",
        "GIT_AUTHOR_DATE": "2026-10-06T10:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-10-06T10:00:00+00:00",
        **extra,
    }


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
            env=_git_env(),
            check=True,
            capture_output=True,
            timeout=30,
        )
    return repo


# --- git_guard.py: every repository that signs commits was refused (any gpg.* key) ------------

# The signing setup of life-os itself (SSH signing), plus other declarative signing keys.
SIGNING = """[user]
\tname = someone
\tsigningkey = /home/someone/.ssh/id_ed25519_signing.pub
[gpg]
\tformat = ssh
\tminTrustLevel = marginal
[commit]
\tgpgsign = true
[tag]
\tgpgSign = true
[push]
\tgpgSign = if-asked
[gpg "ssh"]
\tallowedSignersFile = /home/someone/.ssh/allowed_signers
\trevocationFile = /home/someone/.ssh/revoked
[gpg.ssh]
\tallowedSignersFile = /home/someone/.ssh/allowed_signers
[advice]
\tignoredHook = false
"""


def test_git_guard_allows_commit_signing_config() -> None:
    assert dangerous_keys(parse_git_config(SIGNING)) == []


@pytest.mark.parametrize(
    ("section", "key", "name"),
    [
        ("[gpg]", "program", "gpg.program"),
        ('[gpg "openpgp"]', "program", "gpg.openpgp.program"),
        ('[gpg "x509"]', "program", "gpg.x509.program"),
        ('[gpg "ssh"]', "program", "gpg.ssh.program"),
        ('[gpg "ssh"]', "defaultKeyCommand", "gpg.ssh.defaultkeycommand"),
        ("[gpg.ssh]", "defaultKeyCommand", "gpg.ssh.defaultkeycommand"),
    ],
)
def test_git_guard_refuses_only_the_gpg_keys_that_run_programs(section: str, key: str, name: str) -> None:
    text = SIGNING + f"{section}\n\t{key} = touch /tmp/pwned\n"
    assert dangerous_keys(parse_git_config(text)) == [name]


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("[gc]", "recentObjectsHook"),
        ('[guitool "x"]', "cmd"),
        ('[browser "x"]', "cmd"),
        ('[browser "x"]', "path"),
        ('[man "x"]', "cmd"),
        ('[man "x"]', "path"),
        ('[tar "tar.xz"]', "command"),
        ("[sendemail]", "toCmd"),
        ("[sendemail]", "ccCmd"),
        ("[sendemail]", "headerCmd"),
        ("[sendemail]", "sendmailCmd"),
        ("[sendemail]", "smtpServer"),
        ('[sendemail "work"]', "smtpServer"),
        ("[imap]", "tunnel"),
        ("[instaweb]", "httpd"),
        ("[interactive]", "diffFilter"),
    ],
)
def test_git_guard_refuses_more_documented_executors(section: str, key: str) -> None:
    assert dangerous_keys(parse_git_config(f"{section}\n\t{key} = touch /tmp/pwned\n"))


def _documented_keys() -> list[str]:
    out = subprocess.run(["git", "help", "--config"], capture_output=True, text=True, check=False, timeout=30)
    if out.returncode != 0:
        pytest.skip("git help --config is not available")
    return [k.strip() for k in out.stdout.splitlines() if "." in k and " " not in k.strip()]


def _entry(name: str) -> tuple[str, str | None, str]:
    parts = name.lower().split(".")
    return parts[0], ".".join(parts[1:-1]) or None, parts[-1]


@needs_git
def test_git_guard_against_gits_own_list_of_config_keys() -> None:
    keys = _documented_keys()
    # Every documented key whose name says "program/command/hook/helper" is refused ...
    runs = [k for k in keys if k.lower().endswith(("program", "command", "cmd", "hook", "helper"))]
    # advice.* names are boolean flags. alias.* (incl. alias.<name>.command, git >= 2.56) only
    # runs when the alias is invoked by name, git ignores aliases that hide built-in commands,
    # and Weekly Tracker runs built-ins only (rev-parse, log) - see _NAME_ONLY_SECTIONS.
    runs = [k for k in runs if not k.lower().startswith(("advice.", "alias."))]
    assert runs
    assert [k for k in runs if not dangerous_keys([_entry(k)])] == []
    # ... and the documented declarative signing keys are not.
    signing = [
        "gpg.format",
        "gpg.minTrustLevel",
        "gpg.ssh.allowedSignersFile",
        "gpg.ssh.revocationFile",
        "user.signingKey",
        "commit.gpgSign",
    ]
    assert set(signing) <= set(keys)
    assert [k for k in signing if dangerous_keys([_entry(k)])] == []


@needs_git
def test_git_guard_reads_a_signing_repo_and_still_refuses_a_key_command(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(SIGNING)
    sec = git_section(repo, W41, tz="UTC")
    assert sec["status"] == "ok", sec
    assert sec["reason"] is None
    pwned = tmp_path / "pwned"
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(f'[gpg "ssh"]\n\tdefaultKeyCommand = touch {pwned}\n')
    assert git_section(repo, W41, tz="UTC") == {
        "status": "unavailable",
        "reason": "repository has a custom git config that could run commands",
    }
    assert not pwned.exists()


# --- core/identity.py: `git config` read the CURRENT repository's untrusted config ------------


def _identity_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = Me\n\temail = me@home.invalid\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_CONFIG_GLOBAL", "GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    repo = _repo(tmp_path)
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write("[user]\n\tname = Intruder\n\temail = intruder@repo.invalid\n")
    return repo


@needs_git
def test_identity_ignores_the_current_repositorys_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _identity_env(tmp_path, monkeypatch)
    monkeypatch.chdir(repo)
    who = get_identity()
    assert (who.git_name, who.git_email) == ("Me", "me@home.invalid")


@needs_git
def test_identity_ignores_a_repository_named_by_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _identity_env(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    who = get_identity()
    assert (who.git_name, who.git_email) == ("Me", "me@home.invalid")


# --- profile/store.py: a hand edit of facts.json was not detected ------------------------------


def _facts_path(tmp_path: Path) -> Path:
    return tmp_path / "lm" / "profile" / "facts.json"


def _edit_facts(tmp_path: Path, edit: object) -> None:
    path = _facts_path(tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    edit(doc)  # type: ignore[operator]
    path.write_text(json.dumps(doc), encoding="utf-8")


def _set_salary(tmp_path: Path) -> None:
    _lm_with(tmp_path, "finance")
    assert _lm(tmp_path, "fact", "set", "gross_salary_annual", "1300000", "--category", "finance") == 0
    assert _lm(tmp_path, "fact", "set", "payroll_deductions_annual", "0", "--category", "finance") == 0


def test_hand_edited_fact_value_is_refused_and_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_salary(tmp_path)
    assert _lm(tmp_path, "journal", "verify") == 0
    assert "profile/facts.json: ok" in capsys.readouterr().out

    def bump(doc: dict) -> None:  # type: ignore[type-arg]
        doc["facts"]["gross_salary_annual"]["value"] = 9900000

    _edit_facts(tmp_path, bump)
    assert _lm(tmp_path, "journal", "verify") == 2
    out = capsys.readouterr().out
    assert "profile/facts.json: does not match profile/journal.jsonl" in out and "digest" in out
    assert _lm(tmp_path, "--as-of", "2026-11-10", "tax") == 2
    err = capsys.readouterr().err
    assert "profile/facts.json does not match profile/journal.jsonl" in err
    assert "restore it from a backup" in err
    assert _lm(tmp_path, "fact", "list") == 2
    assert "99,00,000" not in capsys.readouterr().out
    assert _lm(tmp_path, "fact", "correct", "gross_salary_annual", "1") == 2  # no laundering


@pytest.mark.parametrize(
    ("edit", "words"),
    [
        (lambda d: d["facts"].pop("payroll_deductions_annual"), "journaled facts missing: payroll"),
        (
            lambda d: d["facts"].__setitem__("bonus", {**d["facts"]["gross_salary_annual"], "key": "bonus"}),
            "facts not in the journal: bonus",
        ),
        (
            lambda d: d["facts"]["gross_salary_annual"].__setitem__("provenance", "VERIFIED"),
            "category or provenance differs for: gross_salary_annual",
        ),
    ],
)
def test_hand_added_removed_or_relabelled_fact_is_detected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], edit: object, words: str
) -> None:
    _set_salary(tmp_path)
    _edit_facts(tmp_path, edit)
    capsys.readouterr()
    assert _lm(tmp_path, "journal", "verify") == 2
    assert words in capsys.readouterr().out


def test_deleted_facts_file_is_detected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _set_salary(tmp_path)
    _facts_path(tmp_path).unlink()
    capsys.readouterr()
    assert _lm(tmp_path, "journal", "verify") == 2
    assert "journaled facts missing" in capsys.readouterr().out
    assert _lm(tmp_path, "fact", "list") == 2


def test_export_still_works_on_a_mismatch_but_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_salary(tmp_path)

    def bump(doc: dict) -> None:  # type: ignore[type-arg]
        doc["facts"]["gross_salary_annual"]["value"] = 1

    _edit_facts(tmp_path, bump)
    capsys.readouterr()
    assert _lm(tmp_path, "profile", "export") == 2
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert data["journal_ok"] is False and data["facts"]["gross_salary_annual"]["value"] == 1
    assert "journal_ok: false" in err
    store = PrivateStore(tmp_path / "lm")
    events = [e["event"]["event"] for e in store.journal(PROFILE_JOURNAL).entries()]
    assert events[-1] != "profile_export"  # not journaled on top of a mismatch


def test_journal_verify_names_why_facts_were_not_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_salary(tmp_path)
    head = tmp_path / "lm" / "profile" / "journal.jsonl.head"
    saved = head.read_bytes()
    head.unlink()
    capsys.readouterr()
    assert _lm(tmp_path, "journal", "verify") == 2
    assert "profile/facts.json: not checked: profile/journal.jsonl is broken" in capsys.readouterr().out
    head.write_bytes(saved)
    _facts_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert _lm(tmp_path, "journal", "verify") == 2
    assert "profile/facts.json: unreadable (" in capsys.readouterr().out


def test_facts_are_not_used_while_the_profile_journal_is_broken(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_salary(tmp_path)
    (tmp_path / "lm" / "profile" / "journal.jsonl.head").unlink()
    capsys.readouterr()
    assert _lm(tmp_path, "--as-of", "2026-11-10", "tax") == 2  # used to compute from unvouched facts
    assert "does not match profile/journal.jsonl: the journal is broken at entry" in capsys.readouterr().err


def test_facts_file_that_is_not_a_facts_document_is_refused_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_salary(tmp_path)
    _facts_path(tmp_path).write_text("[1, 2]", encoding="utf-8")
    capsys.readouterr()
    assert _lm(tmp_path, "fact", "list") == 2  # used to be an AttributeError traceback
    assert "it is not a facts document" in capsys.readouterr().err


def _legacy_store(tmp_path: Path) -> tuple[PrivateStore, Profile]:
    """A store written before facts digests existed: fact events without facts_digest."""
    store = PrivateStore(tmp_path / "lm")
    ConsentLedger(store, clock=lambda: NOW).grant("finance", purpose="test")
    store.journal(PROFILE_JOURNAL).append(
        {"event": "fact_set", "key": "rent", "category": "finance", "provenance": "VERIFIED"}
    )
    fact = {
        "key": "rent",
        "value": 20000,
        "category": "finance",
        "provenance": "VERIFIED",
        "basis": "lease",
        "recorded_at": NOW,
    }
    store.write_json(FACTS_FILE, {"facts": {"rent": fact}})
    return store, Profile(store, clock=lambda: NOW)


def test_legacy_journal_without_digests_stays_loadable_and_migrates(tmp_path: Path) -> None:
    store, prof = _legacy_store(tmp_path)
    assert prof.get("rent").value == 20000  # type: ignore[union-attr]
    prof.set_fact("food", 9000, "finance", "VERIFIED", "receipts")
    doc = json.loads(store.path(FACTS_FILE).read_text(encoding="utf-8"))
    assert isinstance(doc.get("salt"), str) and len(doc["salt"]) == 32
    last = store.journal(PROFILE_JOURNAL).entries()[-1]["event"]
    assert isinstance(last.get("facts_digest"), str) and len(last["facts_digest"]) == 64
    assert "9000" not in json.dumps(last)  # still no values in the journal
    # From now on a value edit is caught by the digest.
    doc["facts"]["rent"]["value"] = 1
    store.write_json(FACTS_FILE, doc)
    with pytest.raises(JournalTampered, match="digest mismatch"):
        prof.get("rent")


def test_legacy_journal_still_catches_a_hand_added_fact(tmp_path: Path) -> None:
    store, prof = _legacy_store(tmp_path)
    doc = json.loads(store.path(FACTS_FILE).read_text(encoding="utf-8"))
    doc["facts"]["loan"] = {**doc["facts"]["rent"], "key": "loan"}
    store.write_json(FACTS_FILE, doc)
    with pytest.raises(JournalTampered, match="facts not in the journal: loan"):
        prof.get("rent")
    with pytest.raises(JournalTampered):
        prof.delete("finance")


def _set_many(root: str, worker: int, n: int) -> None:
    prof = Profile(PrivateStore(Path(root)))
    for i in range(n):
        prof.set_fact(f"k{worker}_{i}", i, "finance", "VERIFIED", "test")


def test_concurrent_fact_writers_lose_nothing(tmp_path: Path) -> None:
    root = tmp_path / "lm"
    ConsentLedger(PrivateStore(root)).grant("finance", purpose="test")
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_set_many, args=(str(root), w, 8)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    doc = json.loads((root / "profile" / "facts.json").read_text(encoding="utf-8"))
    assert len(doc["facts"]) == 32
    assert lm_main(["--home", str(root), "journal", "verify"]) == 0


def test_store_tolerates_a_directory_created_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from life_os.life_mentor.security import store as store_mod

    real = store_mod._mkdir_private

    def racing(path: Path) -> None:  # another process wins the mkdir
        real(path)
        raise FileExistsError(17, "File exists", str(path))

    monkeypatch.setattr(store_mod, "_mkdir_private", racing)
    store = PrivateStore(tmp_path / "lm")
    store.write_json("profile/facts.json", {"facts": {}})  # root and profile/ both "raced"
    assert store.read_json("profile/facts.json") == {"facts": {}}


# --- cli.py: no command could record a metric -------------------------------------------------


def test_metrics_record_list_correct_review(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _lm_with(tmp_path, "health")
    capsys.readouterr()
    for day, kg in (("2026-11-04", "72.4"), ("2026-11-05", "72.1"), ("2026-11-06", "71.9")):
        assert _lm(tmp_path, "--as-of", "2026-11-06", "metrics", "record", "weight_kg", kg, "--day", day) == 0
    assert capsys.readouterr().out.splitlines()[0] == "weight_kg 2026-11-04: 72.4 kg (USER_ESTIMATE)"
    assert (
        _lm(tmp_path, "--as-of", "2026-11-06", "metrics", "record", "weight_kg", "70", "--day", "2026-11-05")
        == 2
    )
    assert "life-mentor metrics correct weight_kg VALUE --day 2026-11-05" in capsys.readouterr().err
    args = ["metrics", "correct", "weight_kg", "72.0", "--day", "2026-11-05", "--provenance", "verified"]
    assert _lm(tmp_path, "--as-of", "2026-11-06", *args) == 0
    capsys.readouterr()
    assert _lm(tmp_path, "metrics", "list") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [
        "weight_kg\t2026-11-04\t72.4\tkg",
        "weight_kg\t2026-11-05\t72.0\tkg",
        "weight_kg\t2026-11-06\t71.9\tkg",
    ]
    assert out[3] == "not shown: no consent for finance, lifestyle"
    review = ["metrics", "review", "weight_kg", "--target", "72", "--direction", "at_most"]
    assert _lm(tmp_path, "--as-of", "2026-11-06", *review) == 0
    assert "Overall status: SHIP" in capsys.readouterr().out
    assert _lm(tmp_path, "--as-of", "2026-11-30", "metrics", "review", "weight_kg") == 1  # too few points
    assert "insufficient data" in capsys.readouterr().out
    assert _lm(tmp_path, "journal", "verify") == 0
    assert "tracking/metrics.jsonl: ok" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["sleep_hours", "25"], "cannot exceed 24"),
        (["exercise_minutes", "1441"], "cannot exceed 1440"),
        (["weight_kg", "-1"], "cannot be negative"),
        (["weight_kg", "72,4"], "not a plain number"),
        (["weight_kg", "1e3"], "not a plain number"),
        (["weight_kg", "NaN"], "not a plain number"),
        (["weight_kg", "1" + "0" * 16], "out of range"),
        (["weight_kg", "72", "--day", "2026-11-11"], "a future day cannot be recorded"),
    ],
)
def test_metrics_bounds_and_typos_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], args: list[str], message: str
) -> None:
    _lm_with(tmp_path, "health", "lifestyle")
    capsys.readouterr()
    assert _lm(tmp_path, "--as-of", "2026-11-10", "metrics", "record", *args) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "lm" / "tracking" / "metrics.jsonl").exists()


def test_metrics_need_consent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _lm_with(tmp_path)
    capsys.readouterr()
    assert _lm(tmp_path, "metrics", "record", "weight_kg", "72") == 2
    assert "life-mentor consent grant health" in capsys.readouterr().err
    assert _lm(tmp_path, "metrics", "list", "savings") == 2
    assert _lm(tmp_path, "metrics", "list") == 0
    out = capsys.readouterr().out
    assert "not shown: no consent for finance, health, lifestyle" in out and "no metric values yet" in out


# --- weekly_tracker/cli.py: exit codes differed from life-mentor (1 for refusals/errors) -------


@pytest.mark.parametrize(
    "args",
    [
        ["plan", "done", "t9"],  # unknown task
        ["plan", "add", "--day", "mon", "--text", "   "],  # invalid input
        ["plan", "move", "t9", "--day", "tue"],  # unknown task
    ],
)
def test_wt_errors_exit_2_like_life_mentor(tmp_path: Path, args: list[str]) -> None:
    assert wt_main(["--home", str(tmp_path / "wt"), "--no-life-mentor", *args]) == 2


def test_wt_refusing_to_overwrite_exits_2(tmp_path: Path) -> None:
    args = ["--home", str(tmp_path / "wt"), "--no-life-mentor", "write", "--no-git", "--week", "2026-W41"]
    assert wt_main(args) == 0
    assert wt_main(args) == 2  # exists; --force needed (life-mentor: 2 as well)
    out = tmp_path / "w.md"
    out.write_text("mine", encoding="utf-8")
    assert lm_main(["--home", str(tmp_path / "lm"), "init"]) == 0
    assert lm_main(["--home", str(tmp_path / "lm"), "profile", "export", "--out", str(out)]) == 2


def test_both_helps_state_the_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    for main in (lm_main, wt_main):
        with pytest.raises(SystemExit):
            main(["--help"])
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps the description
    assert "Exit 0 SHIP, 1 HOLD, 2 BLOCK/error." in out
    assert "Exit 0 done, 2 refused/error." in out


# --- research/rules.py: a rule-expiry HOLD did not say when the rule expired or what to do ------


def test_stale_rule_hold_names_the_rule_its_expiry_and_as_of() -> None:
    pack = load_tax_pack()
    gate = rules_gate(pack, as_of=date(2027, 9, 1), ids=["new_regime_slabs"])
    (reason,) = [r["reason"] for r in gate.reasons]
    assert gate.disposition == "HOLD"
    assert reason.startswith("new_regime_slabs: stale: expired after 2027-04-09")
    assert "pass --as-of 2027-04-09 or earlier" in reason
    assert "not_in_effect" in reason and "pass --as-of a date from 2026-04-01 to 2027-03-31" in reason
    tax_year = rules_gate(
        pack, as_of=date(2026, 11, 1), ids=["new_regime_slabs"], effective_on=date(2027, 4, 1)
    )
    assert "pass --tax-year (or --as-of) for a tax year starting from 2026-04-01 to 2027-03-31" in str(
        tax_year.reasons
    )


def test_cli_tax_after_expiry_holds_with_the_how_to(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_salary(tmp_path)
    capsys.readouterr()
    assert _lm(tmp_path, "--as-of", "2027-09-01", "tax", "--tax-year", "FY2026-27") == 1
    out = capsys.readouterr().out
    assert "HOLD - rule:new_regime_slabs: new_regime_slabs: stale: expired after 2027-04-09" in out
    assert "pass --as-of 2027-04-09 or earlier" in out
    assert _lm(tmp_path, "--as-of", "2027-03-31", "tax") == 0
