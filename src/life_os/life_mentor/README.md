# Life Mentor — usage

Local, stdlib-only guidance with cited, dated rules and no combined score. Data lives in
`~/.life_mentor/` (override with `--home` or `LIFE_MENTOR_HOME`); only `init` creates it.
See `docs/CHARTER.md` for the rules Life Mentor follows.

Exit codes: `0` = SHIP / done, `1` = HOLD (something missing or awaiting you), `2` = BLOCK or error.

```sh
life-mentor init
life-mentor consent grant finance --purpose "tax and budget"
life-mentor fact set gross_salary_annual 1300000 --category finance --provenance verified --basis "Form 16"
life-mentor fact set payroll_deductions_annual 21600 --category finance --provenance verified --basis payslip
life-mentor fact set assets '{"savings": 300000, "ppf": 200000}' --category finance --json
life-mentor fact list
life-mentor profile missing --for finance      # questions for what is missing
life-mentor tax --regime new                   # FY from --as-of, or --tax-year FY2026-27
life-mentor take-home --regime new
life-mentor decide options.json                # recorded; prints "Decision id: ..."
life-mentor confirm <decision-id> data_course  # records YOUR choice; never executes it
life-mentor report life-plan --out plan.md     # finance|health|lifestyle|career|decisions|life-plan
life-mentor profile export --out me.json       # 0600, refuses to overwrite without --force
life-mentor journal verify
life-mentor import bank-csv statement.csv --mapping mapping.json          # dry run
life-mentor import bank-csv statement.csv --mapping mapping.json --apply  # needs finance consent
life-mentor import bank-csv statement.csv --mapping mapping.json --apply --allow-partial  # skip bad rows
life-mentor import amfi-nav NAVAll.txt --holdings holdings.json           # a file YOU downloaded
life-mentor consent grant lifestyle --purpose habits
life-mentor habits add walk --name "20-minute walk"   # at most 3 active; ids are never reused
life-mentor habits checkin walk --day 2026-11-10      # once per day; --missed records "not done"
life-mentor habits list
life-mentor habits retire walk                        # stops it; the history is kept
life-mentor habits ics --out habits.ics               # active habits as daily all-day events
```

Money facts (`gross_salary_annual`, `payroll_deductions_annual`, `other_deductions_annual`,
`take_home_monthly`, `essential_spend_monthly`, `emergency_fund_balance`,
`discretionary_spend_monthly`, `debt_budget_monthly`, `commute_monthly`,
`other_monthly_costs`) must be a plain non-negative number when written; every amount is
limited to 10^15.

`options.json` for `decide`:

```json
{"regime": "new",
 "option": {"name": "data_course", "kind": "course", "upfront_cost": 60000,
            "recurring_monthly_cost": 0, "horizon_months": 12, "downside_loss": 60000,
            "goal_tags": ["goal_upskill"],
            "benefit_monthly": {"conservative": 0, "base": 5000, "optimistic": 10000,
                                "basis": "own estimate", "evidence_class": "HYPOTHESIS"}},
 "alternatives": []}
```

Bank CSV mapping (`mapping.json`): `date_column`, `date_format` (e.g. `%d/%m/%Y`),
`description_column`, `debit_column` (or `amount_column` + `debit_is_negative`), optional
`credit_column`, and `category_rules: [{"contains": "rent", "category": "rent"}]`.
Descriptions are only matched, never stored; unmatched debits are reported as `unmapped`.
