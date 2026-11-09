# Weekly Tracker

A small weekly summary: one Markdown file per ISO week (`2026-W41.md`) with a
Mon–Sun grid and a short factual summary. Local only, stdlib only, no network.

Sources (each optional, each shown as "unavailable: <reason>" rather than failing):

- **Manual planner** — tasks per day, kept in an append-only, hash-chained log.
- **Git work** — read-only `git log` for a repository: commits per day, files, +/- lines,
  test files touched.
- **Life Mentor** — metrics, habits and decisions for the week, read-only; categories you have
  not consented to are listed as "not shown: no consent for <category>".

```sh
weekly-tracker plan add --day mon --text "Write weekly report"   # prints t1
weekly-tracker plan done t1
weekly-tracker plan skip t2
weekly-tracker show --week 2026-W41 --repo .
weekly-tracker write --week 2026-W41            # -> ~/.weekly_tracker/weeks/2026-W41.md
weekly-tracker write --out ./reports --force    # overwrite only with --force
weekly-tracker plan ics --week 2026-W41 --out week.ics   # local .ics export (no sync)
```

Data lives in `~/.weekly_tracker/` (override with `WEEKLY_TRACKER_HOME` or `--home`);
directories are 0700 and files 0600. Life Mentor data is read from `LIFE_MENTOR_HOME`
(default `~/.life_mentor`) and never written. Calendar sync (Google/Outlook) is not built.
