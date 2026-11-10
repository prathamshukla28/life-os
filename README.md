# Life OS

Two small, local-first command-line tools in one stdlib-only Python package:

- **Life Mentor** (`life-mentor`): a personal mentor for finance, health, career and
  lifestyle decisions. It works from facts you enter (each tagged with where it came
  from) and from dated, cited rule files. It asks for what is missing instead of
  guessing, and it never collapses a decision into a single score: every analysis is a
  table of checks plus a fail-closed **SHIP / HOLD / BLOCK** status with reasons. It reads
  and writes nothing outside its data directory without being asked, and it never makes
  payments, purchases or investments.
- **Weekly Tracker** (`weekly-tracker`): an ISO-week planner and weekly review. It writes
  one Markdown file per week (`2026-W46.md`) that combines your planner, a read-only
  summary of a git repository and, with your consent, Life Mentor data. It can export the
  planner to an `.ics` file you import into any calendar.

Everything runs offline. The package has **no runtime dependencies**, makes **no network
calls**, and needs Python 3.10 or newer. It has been run on macOS. The CI matrix runs
it on Linux. Windows is untested: the file-permission and file-locking code assumes POSIX.

Life OS was written inside the Asur monorepo (as `products/life_mentor` and
`products/weekly_tracker`) and extracted into this standalone repository. The handful of
Asur primitives it relies on are vendored in `life_os.core` with their original MIT
attribution. The `LM..` and `LM-G..` ids that appear in docstrings are its original phase
and gap ids.

> **Not advice.** Tax, finance and health outputs are general information computed
> from your own figures and cited rules. The bundled rule values (Indian income tax for
> FY2026-27, low-risk rates, nutrition, activity and sleep references) are marked
> **UNVERIFIED** until a person checks them against the cited sources. Health output never
> diagnoses.

## Quickstart

```sh
git clone <this repository> life-os && cd life-os
python3 -m venv .venv && . .venv/bin/activate
pip install .            # zero dependencies; use `pip install -e ".[dev]"` to develop
life-mentor --help
weekly-tracker --help
```

### Life Mentor

Only `init` creates the data directory. Every other command refuses to run without one.
Exit codes: `0` SHIP or done, `1` HOLD (something is missing or waiting for you), `2`
BLOCK or error.

```console
$ life-mentor init
created: /home/you/.life_mentor
$ life-mentor consent grant finance --purpose "tax estimate"
finance: granted
$ life-mentor fact set gross_salary_annual 1300000 --category finance --provenance verified --basis "Form 16"
gross_salary_annual: recorded (VERIFIED)
$ life-mentor fact set payroll_deductions_annual 21600 --category finance --provenance verified --basis payslip
payroll_deductions_annual: recorded (VERIFIED)
$ life-mentor --as-of 2026-11-10 tax --regime new
# Income tax

As of 2026-11-10. Generated on 2026-11-10. Overall status: SHIP (clear: every check passed).
...
### Result

- Regime: new
- Tax year: FY2026-27
- Gross salary: 13,00,000
- Taxable income: 12,25,000
- Slab tax: 63,750.00
- Total tax: 26,000
- Tax as percent of taxable income: 2.12
...
```

The full report also lists the inputs with their provenance, every rule used (source URL,
retrieval date, UNVERIFIED flag), assumptions, and what would change the result. Money
facts such as `gross_salary_annual` must be a plain, non-negative number when you enter
them (`1300000` or `85000.50`, not `13,00,000`), and every amount, whether a fact, a
decision input or an imported row, is limited to 10^15. Other
commands: `take-home`, `decide` / `confirm` (a decision analysis, and a record of *your*
choice, which Life Mentor never carries out), `report finance|health|lifestyle|career|decisions|life-plan`,
`fact set|correct|list|delete`, `profile export|missing`, `journal verify`, `habits`, and the local file importers
`import bank-csv` (dry run unless `--apply`; `--allow-partial` to skip unreadable or
unmapped rows) and `import amfi-nav`.

Habits need `lifestyle` consent. At most three are active at once, and history is never
rewritten: `retire` stops a habit but keeps its check-ins, and a day is checked in once.

```console
$ life-mentor consent grant lifestyle --purpose habits
lifestyle: granted
$ life-mentor habits add walk --name "20-minute walk"
habit walk added
$ life-mentor habits checkin walk            # today; --day YYYY-MM-DD, --missed
walk 2026-11-10
$ life-mentor habits list
walk	20-minute walk	since 2026-11-10	active	1/1 check-ins done
$ life-mentor habits ics --out habits.ics    # a daily all-day event per active habit
habits.ics
$ life-mentor habits retire walk
habit walk retired
```

See
[`src/life_os/life_mentor/README.md`](src/life_os/life_mentor/README.md) and the
[charter](src/life_os/life_mentor/docs/CHARTER.md).

### Weekly Tracker

```console
$ weekly-tracker plan add --week 2026-W46 --day mon --text "Draft the quarterly budget"
t1
$ weekly-tracker plan add --week 2026-W46 --day wed --text "Book a dentist appointment"
t2
$ weekly-tracker plan done t1
t1 done
$ weekly-tracker --no-life-mentor show --week 2026-W46 --no-git --generated-on 2026-11-10
# Week 2026-W46 (2026-11-09 to 2026-11-15)

Generated on 2026-11-10. Sources: manual planner. Facts and counts only; no ratings.

| Day | Date | Tasks done/total | Skipped | Commits | Key metrics |
|---|---|---|---|---|---|
| Mon | 2026-11-09 | 1/1 | 0 | — | — |
| Tue | 2026-11-10 | 0/0 | 0 | — | — |
| Wed | 2026-11-11 | 0/1 | 0 | — | — |
...

## Planner

- Mon 2026-11-09: [x] t1 Draft the quarterly budget
- Wed 2026-11-11: [ ] t2 Book a dentist appointment

Carry-over (still planned; listed only, not moved): t2 Book a dentist appointment (Wed).
...
```

`weekly-tracker write` saves the same Markdown to `~/.weekly_tracker/weeks/<week>.md`, or
to `--out DIR`. It never overwrites an existing file unless you pass `--force`.
`weekly-tracker plan ics --week 2026-W46 [--out FILE]` exports the week's tasks as all-day
events. Omit `--no-git` to include commits from `--repo PATH` (default: the current
directory). Omit `--no-life-mentor` to include Life Mentor metrics, habits and decisions
for the categories you have consented to.

## Architecture

```
life_os.core  <--  life_os.life_mentor  <--  life_os.weekly_tracker
```

Dependencies point one way only, and `tests/test_boundary.py` enforces it: `core` imports
neither product, and `life_mentor` never imports `weekly_tracker`. The same test fails if
any module imports a non-stdlib package or a network module (`socket`, `http`,
`urllib.request`, `asyncio`, ...).

| Package | Contents |
|---|---|
| `life_os.core` | Vendored from Asur: `gate` (fail-closed SHIP/HOLD/BLOCK), `canonical` (canonical JSON + SHA-256), `identity` (OS user and git config, no network), `evidence` (evidence classes), `clock` (UTC timestamps). |
| `life_os.life_mentor` | `profile` (facts with provenance, per-category consent), `security` (private store, hash-chained journal, PII redaction, report scanner), `research` (dated, cited rule packs with staleness gates), `finance`, `decision_engine`, `health`, `lifestyle`, `career`, `tracking`, `reports`, `integrations` (bank CSV, AMFI NAV file), `ics`, `cli`. |
| `life_os.weekly_tracker` | `week` (ISO 8601 weeks, including week 53 and year boundaries), `planner` (append-only event log), `sources` (git and Life Mentor readers), `render` (deterministic Markdown), `ics`, `cli`. |

Some design rules apply throughout:

- **Money is `Decimal`.** Floats are rejected as inputs. Indian tax rounding follows the
  s.288A/288B convention, and everything else rounds half-up to the paisa.
- **Fail closed.** A missing input gives HOLD. Contradictory or invalid input gives BLOCK.
  So does a check that raises.
- **Rules are data.** Tax slabs, rates and health references live in dated JSON rule packs
  under `life_mentor/rules/`, each value with a source URL and retrieval date. A value with
  no source BLOCKs on load. A stale value HOLDs.
- **Git is read with care.** Before Weekly Tracker runs any `git` command, it reads the
  repository's own config files as text. It refuses the repository if they set anything
  that could make git run a program (`core.fsmonitor`, `core.pager`, diff/filter drivers,
  `include`, `gpg.*`, ...). It then runs two fixed read-only commands with no shell and a
  10-second timeout.

## Privacy and data storage

- **Location.** Life Mentor keeps its data in `~/.life_mentor/`. Override it with
  `LIFE_MENTOR_HOME` or `--home`. Weekly Tracker uses `~/.weekly_tracker/`
  (`WEEKLY_TRACKER_HOME`, `--home`). Nothing is stored anywhere else unless you pass
  `--out`.
- **Plaintext.** Facts, metrics, journals and the planner are stored as **plaintext JSON
  and JSONL**. Life OS does **not** encrypt data at rest. The Python standard library has no
  authenticated cipher, and this project will not ship home-made cryptography. Protect the
  data with full-disk encryption (FileVault, LUKS, BitLocker) and keep it out of
  synced or backed-up folders you do not trust.
- **Permissions.** Directories are created `0700` and files `0600`, whatever your umask.
  An existing data directory that is group- or world-accessible, or is a symlink, is
  refused rather than silently changed.
- **Atomic writes.** Data files are written to a temporary file in the same directory,
  fsynced, and renamed into place. Journals are append-only, fsynced after each entry, and
  locked with `flock` against concurrent writers. Exports to `--out` never overwrite
  without `--force`.
- **Consent.** Every category (`identity`, `finance`, `health`, `career`, `lifestyle`,
  `goals`) needs explicit consent before a fact in it is stored or used. Weekly Tracker
  shows "not shown: no consent for <category>" instead of reading that data. Export and
  delete work without consent, because the data is yours.
- **Journals.** Consent changes, profile changes, decisions and imports are recorded in
  append-only journals. Each entry is hash-chained to the previous one, and a head anchor
  makes truncation detectable. The audit journals hold keys and counts, never fact values.
  The metric log is different: it is the data store for tracked metrics, so it holds their
  values. A tampered journal blocks further writes. This detects accidental damage and naive edits.
  It is not a defence against someone who can rewrite the whole directory.
- **Redaction.** Text bound for journals and reports passes through a redactor that masks
  e-mail addresses, Indian PAN and Aadhaar numbers, Indian phone numbers and long
  account or card numbers. It is regex-based and deliberately aggressive, but it is not a
  guarantee.

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # pinned ruff, mypy, pytest, pytest-cov, hypothesis
pytest                           # full suite, including Hypothesis property tests
pytest --cov                     # with branch coverage
ruff check . && ruff format --check .
mypy                             # strict, configured in pyproject.toml
```

Tests never use the network. They point `LIFE_MENTOR_HOME` and `WEEKLY_TRACKER_HOME` at
temporary directories (`tests/conftest.py`). Tests that need `git` are skipped when it is
not installed. CI (`.github/workflows/ci.yml`) runs lint, format, strict mypy and the
tests with a coverage floor on Python 3.10 to 3.13, plus a zero-dependency install smoke
test. CodeQL runs weekly. The changes made during extraction are listed in
[CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
