# Life Mentor — usage

Local, stdlib-only guidance with cited, dated rules and no combined score. Data lives in
`~/.life_mentor/` (override with `--home` or `LIFE_MENTOR_HOME`); only `init` creates it.
See `docs/CHARTER.md` for the rules Life Mentor follows.

Exit codes: `0` = SHIP / done, `1` = HOLD (something missing or awaiting you), `2` = BLOCK,
refusal or error (`weekly-tracker` uses the same codes and never returns `1`).

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
life-mentor consent grant health --purpose "weight and sleep"
life-mentor metrics record weight_kg 72.4             # today; --day, --provenance, --basis
life-mentor metrics correct weight_kg 72.1            # a recorded day is corrected, never overwritten
life-mentor metrics list                              # every metric you consented to; or one metric
life-mentor metrics review sleep_hours --period weekly --target 7 --direction at_least
```

Metrics: `weight_kg`, `sleep_hours` (health), `exercise_minutes`, `habit_completion_percent`
(lifestyle), `income`, `expenses`, `savings`, `net_worth`, `debt` (finance). Each needs
consent for its category. Sleep is limited to 24 hours a day, exercise to 1440 minutes and
habit completion to 100 percent. Only `net_worth` may be negative, and a future day is refused.

Rule packs are dated. A rule value past its refresh cadence (stale), or not in effect on
the date being computed, makes `tax`, `take-home`, `decide` and the reports HOLD, by design.
The reason names the rule and says how to proceed: re-check the source and update the pack,
or put `--as-of YYYY-MM-DD` (a date the rule covers) before the command, e.g.
`life-mentor --as-of 2027-03-31 tax`.

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
