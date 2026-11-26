# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - unreleased

First standalone release. Life Mentor and Weekly Tracker were developed inside the
Asur monorepo (as `products/life_mentor` and `products/weekly_tracker`) and are
extracted here into one installable package, `life-os`.

### Changed (extraction)

- New `src/` layout: `life_os.core` (vendored Asur primitives), `life_os.life_mentor`,
  `life_os.weekly_tracker`. All `asur.*`, `life_mentor.*` and `weekly_tracker.*` imports
  were rewritten.
- `life_os.core` vendors `gate`, `canonical`, `identity` and `evidence` from Asur, plus
  `clock` (Asur's `envelope.utc_now_iso` only), with the original MIT attribution.
  Asur's unused `approval_gate` and artifact envelope were not vendored.
- iCalendar `PRODID` is now `-//life-os//...` and generated UIDs end in `@life-os.local`
  (previously `@asur.local`). Calendars imported from earlier exports get new UIDs once.
- `tests/test_boundary.py` now enforces stdlib-only, no-network imports and the one-way
  dependency direction `core -> life_mentor -> weekly_tracker` across `src/life_os`.
- Strict mypy, ruff (lint + format), pytest with branch coverage, Hypothesis property
  tests, and a CI matrix for Python 3.10-3.13.
- Tests run with `LIFE_MENTOR_HOME` / `WEEKLY_TRACKER_HOME` pointed at temporary
  directories (`tests/conftest.py`), so they never touch a real home directory.

### Fixed

Security and data integrity:

- `weekly_tracker/sources/git_guard.py`: a backslash at the end of a config *comment*
  (`x = y ;\`) made the guard skip the next line, so a following `core.fsmonitor`,
  `core.pager`, ... was not seen (git itself does not continue comments). Values are now
  scanned like git does (quotes, escapes, comments, continuations); unterminated quotes
  fail closed. Also refused: `gpg.*`, `difftool/mergetool.*.cmd`,
  `remote.*.uploadpack/receivepack/vcs`, `submodule.*.update`, `trailer.*.command/cmd`,
  and config files that are not regular files (a FIFO would hang the read).
- `security/journal.py`: concurrent appends from two processes could interleave and
  permanently break the hash chain (every later append, including consent changes, was
  then refused). Appends now hold an exclusive `flock` across verify, append and anchor
  write; readers hold a shared lock. Writes loop until complete.
- `profile/store.py`, `integrations/bank_csv.py`, `integrations/amfi_nav.py`: data was
  written *before* the journal append that verifies the chain, so a write into a store
  with a tampered journal landed unjournaled. The journal is now written first.
- `decision_engine/confirm.py`: an option could be confirmed even when the report as a
  whole was BLOCKed (for example duplicate option names). Now refused; a report-level
  HOLD also counts as an override of HOLD.
- `life_mentor/cli.py` and `weekly_tracker/cli.py`: output files are written atomically
  with a shared helper (`write_file_atomic`): temp file, fsync, rename or no-clobber
  hard link. Failures no longer leave `.tmp` files holding exported personal data, and no
  longer print tracebacks (unwritable paths, a directory as `--out`, `decimal`
  overflows). `--generated-on` is validated as a date.
- `security/redact.py`: now catches lowercase PANs, phone numbers written with a spaced
  dash (`98765 - 43210`), numbers split by a space at the end of a URL query,
  `wa.me/<number>` links, apostrophes and internationalised domains in e-mail addresses.
  Runs of ISO dates (`2026-10-11/2026-10-12`) are no longer masked as account numbers,
  and digits on separate lines are no longer joined.

iCalendar:

- `life_mentor/ics.py`: line folding cut UTF-8 byte strings at fixed offsets and decoded
  with `errors="ignore"`, silently dropping any multi-byte character (Devanagari, emoji,
  accented letters) that straddled a fold. Folding is now on character boundaries.
- A lone CR and other control characters were emitted raw inside TEXT values, breaking
  the content-line structure; they are now normalised or removed.
- `DTEND <= DTSTART` and control characters in `UID`/`RRULE` are rejected; dates are
  always four-digit years.
- `weekly_tracker/ics.py`: the event UID included the task's week and day, so after
  `plan move` a re-import created a duplicate event instead of updating it. UIDs are now
  stable per task.
- `.ics` written to stdout is written as bytes so the CRLF line endings survive.

Money and finance:

- `finance/money.py`: `format_inr` rounded the paise separately and dropped the carry
  (`12.999` printed as `Rs 12.00`; `-0.001` as `-Rs 0.00`).
- `finance/debt.py`: a payoff plan was declared "never" as soon as the total balance did
  not fall in one month, although under avalanche it can rise and then clear. It now
  simulates to `max_months` and stops early only on a runaway balance.
- `finance/sip.py`: paise were quantized outside the high-precision context, raising
  `decimal.InvalidOperation` for long horizons or high rates. Inputs above 1200 months or
  100% a year now BLOCK, and any remaining decimal error BLOCKs instead of raising.
- `finance/budget.py`: an emergency fund with zero essential expenses SHIPped as complete;
  it now HOLDs.
- `finance/inputs.py`: a `NaN`/`Infinity` input raised `ValueError` out of every
  calculator; it now BLOCKs.
- `decision_engine/engine.py`: `horizon_months` above 1200 BLOCKs (it raised
  `decimal.Overflow`).
- `integrations/bank_csv.py`: a decimal-comma amount such as `12,50` was read as `1250`;
  a row with more cells than the header (an unquoted `25,000.00`) was silently read as
  `25`. Both are now rejected rows.
- `integrations/amfi_nav.py`: duplicate scheme codes were last-wins (an old NAV could
  replace the current one) and NAVs like `1_0.5` or `1e2` were accepted. Both now leave the
  scheme unpriced (HOLD).

Health, lifestyle, tracking:

- `health/safety.py`: medicine and condition names were compared as raw text, so
  `ACE-inhibitor` or a doubled space missed a cited interaction caution. Matching is now
  on normalised text.
- `lifestyle/exercise.py`: a progression that rounded down to no change after the first
  week (or started from a non-integer value) SHIPped; any stalled week now HOLDs.
- `lifestyle/sleep.py`: the "last 14 days" window held 15 nights.
- `health/usda.py`: a short CSV row raised `AttributeError` instead of `UsdaImportError`;
  a UTF-8 BOM is tolerated.
- `tracking/metrics.py`: `review(min_points=0)` crashed; daily values now have physical
  upper bounds (24 h sleep, 1440 exercise minutes, 100% habit completion).

Weekly Tracker:

- `sources/git_work.py`: with git's default `core.quotePath`, non-ASCII paths came back
  C-quoted (`"tests/caf\303\251.py"`), so they were miscounted and test files were not
  recognised. git now runs with `core.quotePath=false`.
- `week.py`: `9999-W52` was accepted although its Sunday is outside the date range
  (`.end` crashed later); week strings must use ASCII digits.
- `core/gate.py`: the "no checks" result carried a bare string reason while every other
  reason is a dict, so a renderer reading `r["check"]` would crash.

Command line (found in the release verification):

- `weekly-tracker --generated-on` was printed into the report header unvalidated; it must
  now be a real `YYYY-MM-DD` date.
- `weekly-tracker plan done t9` printed "unknown task unknown task 't9'".
- `weekly-tracker plan ics --out DIR` (and `write --out` onto a directory) asked for
  `--force` and then failed naming the hidden temporary file. It now refuses with
  "DIR is a directory", and any write failure names the path you gave.
- `life-mentor` errors named Python calls (`ConsentLedger.grant(...)`, `correct()`).
  They now give the command to run: `life-mentor consent grant <category> --purpose ...`,
  `life-mentor fact correct <key> VALUE`, `life-mentor fact set <key> VALUE ...`. The bank
  CSV HOLD reasons name `--allow-partial`.
- A 29-digit salary made `tax` print `error: [<class 'decimal.InvalidOperation'>]`. Every
  money input now has one bound, 10^15 either way (`MAX_AMOUNT`): `to_money` refuses
  larger amounts, a calculator input beyond it BLOCKs with "out of range", a bank CSV
  row beyond it is rejected, and a NAV beyond it leaves the scheme unpriced (HOLD).
- `life-mentor fact set gross_salary_annual abc` was stored and failed only later. Money
  facts are now checked when written (`fact set` and `fact correct`): a plain,
  non-negative number within the bound.
- `habits ics` always exported an empty calendar, because no command could add a habit.
  New `habits add`, `habits list`, `habits checkin` and `habits retire`, on the existing
  journaled habit tracker.
- CI: the pinned `hypothesis` release needs Python 3.11; Python 3.10 now installs 6.168.5.

Found in the post-extraction code read:

- The git guard refused every repository with any `gpg.*` key, so it refused every
  repository that signs commits (life-os itself: `gpg.format=ssh`,
  `gpg.ssh.allowedSignersFile`). It now refuses only keys whose value can run something:
  `gpg.program`, `gpg.<format>.program`, `gpg.ssh.defaultKeyCommand`, any other key named
  `*program`, `*command`, `*cmd`, `*hook` or `*helper` (outside `advice.*` and `alias.*`),
  and the documented executors that do not follow that pattern (`core.fsmonitor`,
  `core.editor`, `filter.*.clean`, `merge.*.driver`, `*.path` of difftool/mergetool/
  browser/man tools, `interactive.diffFilter`, `imap.tunnel`, `instaweb.httpd`,
  `sendemail.smtpServer`, ...). Newly refused: `gc.recentObjectsHook`, `guitool.*.cmd`,
  `browser/man.*.cmd/path`, `tar.*.command`, `sendemail.*Cmd`. Declarative signing keys
  (`gpg.format`, `user.signingKey`, `gpg.ssh.allowedSignersFile`, `commit.gpgSign`, ...)
  are allowed.
- A hand edit of `profile/facts.json` went unnoticed. Each fact event in the profile
  journal now records a salted digest of the facts file it wrote, and facts.json must
  match both the journal's replayed keys/categories/provenance and the last digest;
  otherwise every read and write fails with "profile/facts.json does not match
  profile/journal.jsonl", `fact list` refuses, `profile export` still exports but exits 2
  with `journal_ok: false`, and `journal verify` reports it. Older journals (no digest)
  stay loadable: only the replay check applies until the next write. Fact writes now hold
  a lock across read, journal and write, so two concurrent writers cannot lose a change,
  and the private store no longer fails with `FileExistsError` when two processes create
  the same directory at once.
- `core/identity.py` ran `git config --get` in the current directory, so a repository's
  own `.git/config` (untrusted, never passed through the git guard) chose the recorded
  identity. It now runs from an empty temporary directory with `GIT_CEILING_DIRECTORIES`
  and without `GIT_DIR`/`GIT_WORK_TREE`, so only the user's global and system config are
  read.
- No command could record a metric although the metric log existed. New `life-mentor
  metrics record|correct|list|review` on the existing `MetricLog` (same bounds and
  consent gates; a future day is refused).
- The two CLIs used different exit codes. `weekly-tracker` now matches `life-mentor`:
  0 done, 2 refused, error or bad usage (it used to return 1 for refusals and errors).
  1 stays life-mentor's HOLD; weekly-tracker never returns it.
- A rule-pack HOLD (stale or not in effect) now says the date the rule expired, or the
  range it is in effect for, and how to proceed (`--as-of`, `--tax-year`, or re-checking
  the source). The behaviour itself is unchanged and by design.

### Known limitations

- Personal data is stored as plaintext JSON/JSONL, protected by `0700`/`0600`
  permissions only. Use full-disk encryption. See "Privacy and data storage" in the
  README.
- `decision_id` is an unkeyed content hash: it detects accidental edits of a saved
  report, not deliberate ones by someone with write access to the store.
- A crash between a journal line and its head anchor leaves the journal failing
  verification (fail closed); recovery is manual. The same holds for a crash between a
  profile journal line and the facts.json write.
