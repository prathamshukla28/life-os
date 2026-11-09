# life_mentor/rules

Reserved for dated RulePack JSON files (loaded by `life_os.life_mentor.research.load_rule_pack`).
LM04 adds `in_income_tax_fy2026_27.json`. Every value must carry `source_url`,
`source_tier`, `retrieved_on`, `effective_from`/`effective_to`, `verified_by_human`,
and `refresh_cadence_days`; a value without `source_url` BLOCKs on load.
