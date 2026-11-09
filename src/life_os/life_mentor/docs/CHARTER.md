# Life Mentor — Charter

Life Mentor is a local-first, stdlib-only Python package (`src/life_os/life_mentor/`,
imported as `life_os.life_mentor`). It was written inside the Asur monorepo and extracted
into Life OS; the `LM..` / `LM-G..` identifiers below are its original phase and gap ids. It
helps **one person** reason about their own finances, health habits, career, and
lifestyle. It is an explainer and a checklist, not an oracle.

## 1. Purpose

- Turn the user's own facts plus cited, dated reference rules into **explainable,
  per-criterion** analyses (e.g. "can I afford this?", "what is my tax under each
  regime?").
- Ask for what is missing instead of guessing. Missing facts produce **questions** and
  a **HOLD**, never a default value.
- Keep every input, rule, and recommendation traceable to its source.

## 2. Invariants

| ID | Rule |
|----|------|
| LM-LOCAL | Stdlib only. **No network** in library code or tests. A future network refresh will be an explicit, human-run CLI, never an implicit call. |
| LM-REUSE | Build on the vendored primitives in `life_os.core` (`canonical`, `gate`, `identity`, `evidence`, `clock`); `life_os.core` never imports Life Mentor. |
| LM-PROV | Every fact carries provenance: `VERIFIED` \| `USER_ESTIMATE` \| `ASSUMPTION` \| `UNKNOWN`, plus a basis and `recorded_at`. |
| LM-RULE | Every rule value carries `source_url`, `source_tier` (primary\|secondary), `retrieved_on`, `effective_from`/`effective_to`, `verified_by_human`, `refresh_cadence_days`. Missing `source_url` → **BLOCK** on load. Stale → **HOLD**. Secondary or not-human-verified → flagged **unverified** (never silently used). |
| LM-HONEST | No single fake score. Decisions report per-criterion results plus a gate disposition with reasons. Reports may not contain a `score` field or the word "guaranteed". |
| LM-GATE | Fail closed: missing input → **HOLD**; contradictory or invalid input → **BLOCK**. A check that raises is BLOCK (`life_os.core.gate.run_gate`). |
| LM-JOURNAL | Consent changes and consequential profile changes (set, correct, delete) are written to a **hash-chained**, append-only JSONL journal (`prev_hash` → `entry_hash` via `canonical_sha256`); `verify()` reports the first broken index; a sidecar `<journal>.head` anchor (count + head hash) makes tail truncation detectable. Audit journal entries (consent, profile, decisions) never contain fact values — the LM07 tracking metric log is a separate *data* log that does (LM-G40) — and every string is PII-redacted before hashing. |
| LM-HUMAN | Human confirmation is required before any transaction, purchase, or data share. **The system never executes them.** |
| LM-MONEY | Money is `Decimal` (or integer paise) — never `float`. Facts and journals reject floats; rule packs parse JSON numbers as `Decimal`. |

## 3. Privacy model

- **Data location:** `~/.life_mentor/` by default; override with env
  `LIFE_MENTOR_HOME` or an explicit path argument (tests always pass `tmp_path`).
  Personal data is **never** written into the repository.
- **Permissions:** directories `0700`, files `0600`, enforced regardless of umask. An
  existing root that is group/world-accessible, or is a symlink, is refused
  (`InsecureStore`) rather than silently tightened.
- **Atomic writes:** temp file created with `O_CREAT|O_EXCL` at mode `0600`, fsynced,
  then `os.replace`d into place.
- **Consent:** per category — `identity`, `finance`, `health`, `career`, `lifestyle`,
  `goals`. Every category requires consent before a fact is stored or read for use.
  Export and delete work without consent (the user's data is always theirs).
- **Export / correct / delete:** `Profile.export()` returns everything (deliberately without a
  consent check — the user's right of access — and journaled as `profile_export`);
  `Profile.correct()` replaces a value with new provenance; `Profile.delete(category)`
  removes a whole category. All three are journaled (keys and counts only, no values).
- **Encryption at rest: none in Life Mentor.** Data is plaintext JSON / JSONL protected
  only by `0700`/`0600` permissions. Use full-disk encryption (macOS **FileVault**, LUKS,
  BitLocker). The standard library has no authenticated cipher, and Life OS will not ship
  home-made crypto, so encryption would need a third-party dependency; it is not built.
- **Redaction:** `life_os.life_mentor.security.redact()` (NFKC-normalised; digits joined by spaces,
  NBSP, `.`, `-` or `/`) masks PAN, Aadhaar, Indian phone numbers, email addresses, and long
  account/card numbers in any text bound for logs. A fact's `basis` is redacted when the
  fact is created, and `repr(Fact)` never shows its value or basis.

## 4. What Life Mentor does NOT do

- **No diagnosis.** Health output is general, cited reference information; anything
  clinical (symptoms, medications, drug–food interactions) is flagged for a clinician.
- **No transactions.** It never buys, sells, transfers, files, or submits anything.
- **No guaranteed returns.** Projections are scenarios (conservative / base /
  optimistic) with their basis; nothing is "guaranteed".
- **No network.** No API calls, no telemetry, no background refresh.
- **No assumed values.** A missing fact is a question, not a default.
- **No bundled restricted data.** Only CC0 food data (USDA FoodData Central) may ever
  be bundled; IFCT/INDB/GI tables are user-imported with citation (gap LM-G2).

## 5. Finance calculators (LM04)

- Every tax slab, rate, limit, and rounding multiple comes from the dated rule pack; the
  Python code holds no tax numbers. Every value is currently `verified_by_human: false`,
  so every tax report says **UNVERIFIED** until a human checks the cited sources.
- Tax scope: resident individual, salary income only, normal slab rates; no
  capital-gains/special-rate income; no HRA/80C/80D computation (old regime takes one
  user-supplied `other_deductions` total). Professional tax and employee EPF are
  user-supplied `payroll_deductions` for take-home, never computed.
- Returns used for SIP projections are assumptions, not promises; three scenarios are
  always shown side by side.
- Each report = inputs with provenance + rules used with citations + assumptions + result +
  gate disposition. No combined "financial health" number.

## 6. Decision engine (LM05)

- Every proposal is analysed in the plan's 8 steps — total cost, share of take-home,
  essentials and emergency fund, benefit scenarios, downside and opportunity cost, payback,
  versus alternatives, goal fit — next to every alternative and a **do-nothing baseline**
  (keep the cash at a cited low-risk rate).
- Output is a per-criterion table per option plus gate dispositions (worst row wins). Missing
  facts → HOLD and questions; negative surplus, an upfront cost larger than the emergency
  fund, a profile-gate BLOCK, or contradictory inputs → BLOCK; spending the emergency fund
  below its target, a base case that does not pay back within the horizon, or a base case
  worse than keeping the cash at the low-risk rate → HOLD (a human may override a HOLD).
- The recommendation: blockers first ("no option clears"); then, if anything is missing,
  "insufficient information" with the list; otherwise the option that clears every check and
  is no worse than keeping the cash, with the best goal fit (tie-break stated in the text). Benefit estimates are assumptions, not promises.
- **Life Mentor never executes.** A human's choice is recorded with `record_confirmation`
  (journal: ids and dispositions only, `executed: false`); BLOCKed options and reports that no
  longer hash to their `decision_id` cannot be confirmed.

## 7. Health & nutrition (LM06)

- Food values come only from a bundled **USDA FoodData Central SR Legacy 2018-04** subset
  (CC0; 39 foods common in Indian diets), generated by `life_mentor.health.usda` from the
  official CSV zip whose URL and sha256 are stored in the bundle. Every value carries its
  fdc_id, nutrient id/name/unit and the per-100 g basis. No IFCT, INDB or GI table is bundled.
- Reference targets come only from cited, dated rule packs: ICMR-NIN 2020 protein EAR/RDA and
  FAO 2013 amino-acid scoring patterns. A missing target → HOLD with a question.
- Amino-acid results are **uncorrected (no digestibility correction)** unless a cited
  digestibility is supplied, and are never presented as a DIAAS. Glycemic load is computed
  only from a GI the user supplies with a citation.
- Safety: any listed medicine or condition → HOLD; cited flags (NIH MedlinePlus, NIDDK)
  always say "discuss with your clinician". **No diagnosis, no dose or medicine changes.**
  Energy below 1,200 kcal/day or protein above 2.2 g/kg/day → HOLD unless a clinician note is
  recorded (owner policy thresholds, not targets).

## 8. Lifestyle, tracking & career (LM07–LM08)

- **Exercise:** plans are checked per criterion against the cited WHO 2020 adult
  recommendations (150–300 min moderate or 75–150 min vigorous aerobic activity a week;
  muscle strengthening on 2+ days). The session layout and the ≤ 10 %/week progression are
  labelled heuristics. Medical limitations or listed conditions → HOLD "clear with your
  clinician or physiotherapist"; no medical clearance is ever implied.
- **Sleep:** compared with the cited AASM/SRS 2015 consensus (≥ 7 h for adults 18–60);
  persistent short sleep or any reported symptom → HOLD with a clinician suggestion. No
  diagnosis; symptom text is never echoed.
- **Habits:** at most 3 active at once (a 4th is HOLD and not recorded); check-ins are
  append-only.
- **Tracking:** the metric log is a *data* log in the private store (hash-chained, 0600) and
  so holds values; audit journals still never do. Reviews state "correlation is not
  causation" and never credit an outcome to an action. Fewer than 3 points → HOLD.
- **Career:** offers are compared on LM04 take-home with the same tax rules; courses go
  through the LM05 decision engine with take-home uplift from user-cited salary
  expectations. Salary benchmarks come only from user-supplied dated sources — no salary
  data is bundled; without one the answer is "insufficient market evidence".

## 9. Reports, CLI & local integrations (LM09–LM10)

- **Reports** (`life_mentor.reports`): finance, health, lifestyle, career, decisions and a
  linked **life plan** in Markdown. Every section shows inputs with provenance, cited rules
  (URL, retrieved date, UNVERIFIED flag), assumptions, result, gate status with reasons,
  questions for missing facts, what would change it, and at most three suggested actions;
  the report ends with at most three next steps. Missing facts are shown as missing — never
  defaulted. Reports are read-only, deterministic (`as_of`, `generated_on`), redacted, and
  must pass the LM02 scanner. Plain-language disclaimers: not tax/investment/legal advice;
  not medical advice and no diagnosis; Life Mentor never executes a decision.
- **CLI** (`life-mentor`, see `life_mentor/README.md`): exit 0 SHIP, 1 HOLD, 2 BLOCK/error.
  Only `init` creates the store; every other command refuses (exit 2) if it is missing.
  `confirm` records your choice and never executes it.
- **Local integrations** (opt-in, no network): bank/expense CSV → monthly totals by your
  categories (dry run by default; `--apply` needs finance consent; descriptions are matched
  but never stored; bad rows are listed, not guessed); AMFI NAVAll.txt that *you* downloaded
  → holdings valuation (exact header check, stale NAV > 5 business days → HOLD). Journals
  record keys only. A one-way, local `.ics` export of active habits exists
  (`life-mentor habits ics`); wearables, health platforms, calendar sync and bank APIs are
  not built.

## 10. Layout

| Path | Phase | Contents |
|------|-------|----------|
| `life_mentor/security/` | LM02 | private store, hash-chained journal, redaction, report scanner |
| `life_mentor/profile/` | LM01 | facts with provenance, consent ledger, profile store, missing-fact gate |
| `life_mentor/research/` | LM03 | `SourceRecord`, `RulePack` loader, staleness gate |
| `life_mentor/rules/` | LM04–LM07 | dated RulePack JSON files (tax, low-risk rates, nutrition, health interactions, activity, sleep) |
| `life_mentor/finance/` | LM04 | income tax + take-home, cash flow, emergency fund, debt payoff, goal SIP, net worth |
| `life_mentor/decision_engine/` | LM05 | 8-step affordability engine, do-nothing baseline, human confirmation journal |
| `life_mentor/health/` | LM06 | USDA subset + importer, nutrition analysis, safety flags |
| `life_mentor/lifestyle/` | LM07 | exercise plan + WHO check, sleep analysis, habits |
| `life_mentor/tracking/` | LM07 | metric log + periodic reviews |
| `life_mentor/career/` | LM08 | career snapshot, job-offer comparison, course evaluation, benchmarks, negotiation checklist |
| `life_mentor/reports/` | LM09 | Markdown report builders (finance … life plan) |
| `life_mentor/cli.py` | LM09 | `life-mentor` command line |
| `life_mentor/integrations/` | LM10 | local-file importers: bank CSV, AMFI NAVAll.txt |
