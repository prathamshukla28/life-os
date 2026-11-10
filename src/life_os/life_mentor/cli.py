"""``life-mentor`` command line (stdlib argparse).

Exit codes: 0 = SHIP / done, 1 = HOLD (something missing or awaiting you), 2 = BLOCK or error.

Only ``init`` creates the private store (``--home``, else $LIFE_MENTOR_HOME, else
~/.life_mentor). Every other command - read or write - refuses with exit 2 when the store
does not exist, so nothing is created by accident. Life Mentor never executes a purchase,
payment, investment or data share.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from life_os.core.gate import BLOCK, HOLD, SHIP

from .decision_engine import (
    Option,
    analyze_decision,
    benefits,
    load_rates_pack,
    record_confirmation,
    situation_from_profile,
)
from .finance import income_tax_from_profile, load_tax_pack, take_home
from .integrations import (
    apply_bank_csv,
    apply_nav_valuation,
    load_mapping,
    preview_bank_csv,
    value_holdings,
)
from .profile import CATEGORIES, ConsentRequired, FactExists, Profile
from .profile.facts import decode_value, encode_value
from .reports import (
    DISCLAIMERS,
    Section,
    career_report,
    decisions_report,
    finance_report,
    finance_sections,
    fmt_value,
    health_report,
    life_plan_report,
    lifestyle_report,
    render_report,
    requirements_for,
)
from .research import RulePackBlocked
from .security import (
    InsecureStore,
    Journal,
    JournalTampered,
    PrivateStore,
    resolve_home,
    write_file_atomic,
)

__all__ = ["EXIT_CODES", "main"]

EXIT_CODES = {SHIP: 0, HOLD: 1, BLOCK: 2}
PROVENANCE = {
    "verified": "VERIFIED",
    "estimate": "USER_ESTIMATE",
    "assumption": "ASSUMPTION",
    "unknown": "UNKNOWN",
}
REPORTS = ("finance", "health", "lifestyle", "career", "decisions", "life-plan")
_INT = re.compile(r"-?\d+")
_DEC = re.compile(r"-?\d*\.\d+")
_ID = re.compile(r"[0-9a-f]{64}")


class CliError(Exception):
    """A refusal or bad input; printed and mapped to exit 2."""


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="life-mentor",
        description="Life Mentor: local, cited, no-score guidance. Exit 0 SHIP, 1 HOLD, 2 BLOCK/error.",
    )
    p.add_argument("--home", help="private store directory")
    p.add_argument("--as-of", type=_date, help="date for rule freshness (default today)")
    p.add_argument("--generated-on", type=_date, help="date printed on reports (default as-of)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the private store (0700)")

    consent = sub.add_parser("consent").add_subparsers(dest="action", required=True)
    g = consent.add_parser("grant")
    g.add_argument("category", choices=CATEGORIES)
    g.add_argument("--purpose", required=True)
    r = consent.add_parser("revoke")
    r.add_argument("category", choices=CATEGORIES)
    r.add_argument("--reason", default="")
    consent.add_parser("list")

    fact = sub.add_parser("fact").add_subparsers(dest="action", required=True)
    for name in ("set", "correct"):
        fp = fact.add_parser(name)
        fp.add_argument("key")
        fp.add_argument("value", nargs="?")
        if name == "set":
            fp.add_argument("--category", required=True, choices=CATEGORIES)
        fp.add_argument("--provenance", choices=tuple(PROVENANCE), default="estimate")
        fp.add_argument("--basis", default="entered on the command line")
        fp.add_argument("--json", action="store_true", help="VALUE is JSON (numbers->Decimal)")
    fact.add_parser("list")
    d = fact.add_parser("delete")
    d.add_argument("category", choices=CATEGORIES)
    d.add_argument("--yes", action="store_true", help="confirm deleting the whole category")

    prof = sub.add_parser("profile").add_subparsers(dest="action", required=True)
    e = prof.add_parser("export")
    e.add_argument("--out")
    e.add_argument("--force", action="store_true")
    m = prof.add_parser("missing")
    m.add_argument(
        "--for", dest="kind", default="all", choices=("finance", "health", "lifestyle", "career", "all")
    )

    for name in ("tax", "take-home"):
        tp = sub.add_parser(name)
        tp.add_argument("--regime", choices=("new", "old"), default="new")
        tp.add_argument("--tax-year")
    dec = sub.add_parser("decide", help="analyse options from a JSON file (recorded)")
    dec.add_argument("options_file")
    conf = sub.add_parser("confirm", help="record YOUR choice (never executes it)")
    conf.add_argument("decision_id")
    conf.add_argument("option")
    rep = sub.add_parser("report")
    rep.add_argument("kind", choices=REPORTS)
    rep.add_argument("--out")
    rep.add_argument("--force", action="store_true")
    rep.add_argument("--regime", choices=("new", "old"), default="new")
    jr = sub.add_parser("journal").add_subparsers(dest="action", required=True)
    jr.add_parser("verify")

    hab = sub.add_parser(
        "habits", help="daily habits (at most 3 active; needs lifestyle consent)"
    ).add_subparsers(dest="action", required=True)
    ha = hab.add_parser("add", help="start tracking a habit")
    ha.add_argument("habit_id", help="1-40 characters of a-z, 0-9 or _ (never reused)")
    ha.add_argument("--name", required=True, help="what you will do, e.g. '20-minute walk'")
    ha.add_argument("--start", type=_date, help="first day (default: --as-of, else today)")
    hab.add_parser("list", help="list habits with their status and check-in counts")
    hr = hab.add_parser("retire", help="stop tracking a habit (its history is kept)")
    hr.add_argument("habit_id")
    hc = hab.add_parser("checkin", help="record one day as done (or --missed)")
    hc.add_argument("habit_id")
    hc.add_argument("--day", type=_date, help="the day (default: --as-of, else today)")
    hc.add_argument("--missed", action="store_true", help="record the day as not done")
    hics = hab.add_parser("ics", help="export active habits to a .ics calendar (local)")
    hics.add_argument("--out", help="output .ics file (default: stdout)")
    hics.add_argument("--force", action="store_true", help="overwrite an existing file")

    imp = sub.add_parser("import").add_subparsers(dest="action", required=True)
    b = imp.add_parser("bank-csv", help="bank/expense CSV (dry run unless --apply)")
    b.add_argument("file")
    b.add_argument("--mapping", required=True)
    b.add_argument("--apply", action="store_true")
    b.add_argument("--allow-partial", action="store_true")
    a = imp.add_parser("amfi-nav", help="value holdings from your NAVAll.txt")
    a.add_argument("file")
    a.add_argument("--holdings", required=True)
    a.add_argument("--apply", action="store_true")
    return p


# -- helpers ---------------------------------------------------------------------------


def _parse_value(raw: str | None, as_json: bool, provenance: str) -> Any:
    if provenance == "UNKNOWN":
        if raw is not None:
            raise CliError("an unknown fact takes no value")
        return None
    if raw is None:
        raise CliError("a value is required (or use --provenance unknown)")
    if as_json:
        try:
            return json.loads(raw, parse_float=Decimal)
        except ValueError as exc:
            raise CliError(f"value is not valid JSON: {exc}") from exc
    if _INT.fullmatch(raw):
        return int(raw)
    if _DEC.fullmatch(raw):
        return Decimal(raw)
    if raw.casefold() in ("true", "false"):
        return raw.casefold() == "true"
    return raw


def _write_new(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        raise CliError(f"{path} exists; use --force to overwrite")
    try:
        write_file_atomic(path, text.encode("utf-8"), overwrite=force)
    except FileExistsError as exc:
        raise CliError(f"{path} exists; use --force to overwrite") from exc
    except OSError as exc:
        raise CliError(f"cannot write {path}: {exc.strerror or exc}") from exc


def _numbers(obj: Any) -> Any:
    """JSON options/holdings: numbers and numeric strings become Decimal (never float)."""
    if isinstance(obj, dict):
        return {k: _numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_numbers(v) for v in obj]
    if isinstance(obj, (int, Decimal)) and not isinstance(obj, bool):
        return Decimal(obj)
    return obj


def _load_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read JSON {path}: {exc}") from exc


def _emit(markdown: str, disposition: str) -> int:
    sys.stdout.write(markdown)
    return EXIT_CODES[disposition]


def _single(
    title: str, report: dict[str, Any], as_of: date, generated: str, disclaimer: str
) -> tuple[str, str]:
    rendered = render_report(
        title.casefold(),
        title,
        [Section(title, report)],
        as_of=as_of,
        generated_on=generated,
        disclaimer=disclaimer,
    )
    return rendered.markdown, rendered.disposition


# -- commands ----------------------------------------------------------------------------


def _cmd_consent(args: argparse.Namespace, prof: Profile) -> int:
    if args.action == "grant":
        prof.consent.grant(args.category, purpose=args.purpose)
        print(f"{args.category}: granted")
    elif args.action == "revoke":
        prof.consent.revoke(args.category, reason=args.reason)
        print(f"{args.category}: revoked")
    else:
        for cat, ok in prof.consent.status().items():
            print(f"{cat}: {'granted' if ok else 'not granted'}")
    return 0


def _cmd_fact(args: argparse.Namespace, prof: Profile) -> int:
    if args.action in ("set", "correct"):
        prov = PROVENANCE[args.provenance]
        value = _parse_value(args.value, args.json, prov)
        if args.action == "set":
            try:
                prof.set_fact(args.key, value, args.category, prov, args.basis)
            except FactExists as exc:
                raise CliError(
                    f"fact {args.key!r} already exists; change it with: life-mentor fact correct {args.key} VALUE"
                ) from exc
        else:
            try:
                prof.correct(args.key, value, prov, args.basis)
            except KeyError as exc:
                raise CliError(
                    f"no fact {args.key!r} yet; add it with: life-mentor fact set {args.key} VALUE --category ..."
                ) from exc
        print(f"{args.key}: recorded ({prov})")
        return 0
    if args.action == "delete":
        if not args.yes:
            raise CliError(f"deleting every {args.category} fact needs --yes")
        removed = prof.delete(args.category)
        print(f"deleted {len(removed)} {args.category} fact(s)")
        return 0
    data = prof.export()
    for key, f in data["facts"].items():
        print(f"{key}\t{f['category']}\t{f['provenance']}\t{fmt_value(decode_value(f['value']))}")
    return 0


def _cmd_profile(args: argparse.Namespace, prof: Profile) -> int:
    if args.action == "export":
        text = json.dumps(prof.export(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if args.out:
            _write_new(Path(args.out), text, args.force)
            print(args.out)
        else:
            sys.stdout.write(text)
        return 0
    questions = prof.missing_facts(requirements_for(args.kind), allow_assumptions=True)
    for q in questions:
        print(f"{q['key']} ({q['reason']}): {q['question']}")
    if not questions:
        print("nothing missing")
    return 1 if questions else 0


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise CliError(f"{name} must be a JSON list")
    return value


def _cmd_decide(
    args: argparse.Namespace, prof: Profile, store: PrivateStore, as_of: date, generated: str
) -> int:
    spec = _numbers(_load_json(args.options_file))
    if not isinstance(spec, dict) or not isinstance(spec.get("option"), dict):
        raise CliError("options file needs an 'option' object")

    def whole(name: str, value: Any) -> int | None:
        if value is None:
            return None
        if not isinstance(value, Decimal) or value != value.to_integral_value():
            raise CliError(f"{name} must be a whole number, got {value}")
        return int(value)

    def option(o: Any) -> Option:
        if not isinstance(o, dict):
            raise CliError("each option must be a JSON object")
        ben = o.get("benefit_monthly")
        if ben is not None and not isinstance(ben, dict):
            raise CliError("benefit_monthly must be an object with conservative/base/optimistic")
        horizon = o.get("horizon_months")
        return Option(
            name=str(o.get("name", "")),
            kind=str(o.get("kind", "spend")),
            upfront_cost=o.get("upfront_cost"),
            recurring_monthly_cost=o.get("recurring_monthly_cost"),
            horizon_months=whole("horizon_months", horizon),
            benefit_monthly=None
            if ben is None
            else benefits(
                ben.get("conservative"),
                ben.get("base"),
                ben.get("optimistic"),
                basis=str(ben.get("basis", "")),
                evidence_class=str(ben.get("evidence_class", "")),
            ),
            downside_loss=o.get("downside_loss"),
            goal_tags=tuple(str(t) for t in o.get("goal_tags", [])),
        )

    th_report = None
    if prof.get("take_home_monthly") is None:
        th_report = take_home(
            load_tax_pack(),
            as_of=as_of,
            regime=str(spec.get("regime", "new")),
            gross_salary=prof.get("gross_salary_annual"),
            payroll_deductions=prof.get("payroll_deductions_annual"),
        )
    months = spec.get("emergency_fund_months")
    situation = situation_from_profile(
        prof, take_home_report=th_report, emergency_fund_months=whole("emergency_fund_months", months)
    )
    report = analyze_decision(
        option(spec["option"]),
        situation=situation,
        rates=load_rates_pack(),
        as_of=as_of,
        alternatives=[option(a) for a in _list(spec.get("alternatives", []), "alternatives")],
        store=store,
    )
    store.write_json(f"decisions/reports/{report['decision_id']}.json", encode_value(report))
    markdown, disp = _single("Decision analysis", report, as_of, generated, DISCLAIMERS["decisions"])
    sys.stdout.write(markdown)
    print(f"Decision id: {report['decision_id']}")
    return EXIT_CODES[disp]


def _cmd_confirm(args: argparse.Namespace, store: PrivateStore, generated: str) -> int:
    if not _ID.fullmatch(args.decision_id):
        raise CliError("decision id must be the 64-character id printed by `decide`")
    path = store.path(f"decisions/reports/{args.decision_id}.json")
    if not path.is_file():
        raise CliError("no saved decision report with that id")
    report = decode_value(json.loads(path.read_text(encoding="utf-8")))
    entry = record_confirmation(store, report, args.option, clock=lambda: f"{generated}T00:00:00Z")
    ev = entry["event"]
    print(
        f"Confirmed '{ev['option']}' for decision {args.decision_id[:12]} "
        f"(status {ev['option_disposition']}; override of hold: "
        f"{'yes' if ev['override_of_hold'] else 'no'})."
    )
    print("executed: no - Life Mentor never executes; you act yourself.")
    return 0


def _cmd_report(args: argparse.Namespace, prof: Profile, as_of: date, generated: str) -> int:
    kw: dict[str, Any] = {"as_of": as_of, "generated_on": generated}
    builders = {
        "finance": lambda: finance_report(prof, regime=args.regime, **kw),
        "health": lambda: health_report(prof, **kw),
        "lifestyle": lambda: lifestyle_report(prof, **kw),
        "career": lambda: career_report(prof, regime=args.regime, **kw),
        "decisions": lambda: decisions_report(prof, **kw),
        "life-plan": lambda: life_plan_report(prof, regime=args.regime, **kw),
    }
    rendered = builders[args.kind]()
    if args.out:
        _write_new(Path(args.out), rendered.markdown, args.force)
        print(args.out)
        return EXIT_CODES[rendered.disposition]
    return _emit(rendered.markdown, rendered.disposition)


def _cmd_journal(store: PrivateStore) -> int:
    bad = False
    paths = sorted(store.root.rglob("*.jsonl"))
    for path in paths:
        ok, index = Journal(path).verify()
        bad |= not ok
        rel = path.relative_to(store.root).as_posix()
        print(f"{rel}: {'ok' if ok else f'broken at entry {index}'}")
    if not paths:
        print("no journals yet")
    return 2 if bad else 0


def _cmd_import(args: argparse.Namespace, store: PrivateStore, as_of: date, generated: str) -> int:
    if args.action == "bank-csv":
        mapping = load_mapping(args.mapping)
        if args.apply:
            report = apply_bank_csv(store, args.file, mapping, allow_partial=args.allow_partial)
        else:
            report = preview_bank_csv(args.file, mapping)
        markdown, disp = _single("Bank CSV import", report, as_of, generated, DISCLAIMERS["finance"])
        sys.stdout.write(markdown)
        if not args.apply:
            print("Dry run: nothing written. Re-run with --apply to store the monthly totals.")
        return EXIT_CODES[disp]
    spec = _load_json(args.holdings)
    rows = spec.get("holdings") if isinstance(spec, dict) else None
    if not isinstance(rows, list):
        raise CliError("holdings file needs a 'holdings' list")
    holdings = []
    for h in rows:
        try:
            units = Decimal(str(h["units"]))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise CliError(f"bad holding entry: {exc}") from exc
        holdings.append({"scheme_code": str(h.get("scheme_code", "")), "units": units})
    report = value_holdings(args.file, holdings, as_of=as_of)
    if args.apply:
        apply_nav_valuation(store, report)
    markdown, disp = _single("Mutual fund valuation", report, as_of, generated, DISCLAIMERS["finance"])
    return _emit(markdown, disp)


def _gate_outcome(report: dict[str, Any]) -> int:
    """Print a small report's gate reasons (one per line) and map it to an exit code."""
    gate = report["gate"]
    for r in gate["reasons"]:
        prefix = "" if r["disposition"] == SHIP else f"{r['disposition']}: "
        print(f"{prefix}{r['reason']}")
    return EXIT_CODES[gate["disposition"]]


def _cmd_habits(args: argparse.Namespace, store: PrivateStore, as_of: date) -> int:
    """Habits on the journaled ``HabitTracker`` (LM07), and the .ics export (LM-G54).

    Every action needs ``lifestyle`` consent (enforced by the tracker). History is
    append-only: ``retire`` stops a habit without deleting it, and a check-in is never
    overwritten. The export makes no network call and links no account: it writes a
    file you import yourself (ASUR-LOCAL-01).
    """
    from .lifestyle.habit_ics import habits_to_calendar
    from .lifestyle.habits import HabitTracker

    tracker = HabitTracker(store)
    if args.action == "add":
        return _gate_outcome(tracker.add_habit(args.habit_id, args.name, started_on=args.start or as_of))
    if args.action == "retire":
        return _gate_outcome(tracker.retire_habit(args.habit_id))
    if args.action == "checkin":
        day = args.day or as_of
        return _gate_outcome(tracker.check_in(args.habit_id, day, done=not args.missed, as_of=as_of))
    if args.action == "list":
        history = tracker.habit_history()
        for h in history:
            done = sum(1 for ok in h["checkins"].values() if ok)
            print(
                f"{h['habit_id']}\t{h['name']}\tsince {h['started_on'].isoformat()}\t"
                f"{'active' if h['active'] else 'retired'}\t{done}/{len(h['checkins'])} check-ins done"
            )
        if not history:
            print("no habits yet; add one with: life-mentor habits add ID --name NAME")
        return 0
    text = habits_to_calendar(tracker.habit_history())
    if args.out:
        _write_new(Path(args.out), text, args.force)
        print(args.out)
    else:
        # Bytes, so the CRLF line endings RFC 5545 requires survive on every platform.
        sys.stdout.flush()
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    return 0


def _run(args: argparse.Namespace) -> int:
    home = resolve_home(args.home)
    as_of = args.as_of or datetime.now(timezone.utc).astimezone().date()
    generated = (args.generated_on or as_of).isoformat()
    if args.cmd == "init":
        existed = home.is_dir()
        PrivateStore(home).ensure()
        print(f"{'already initialised' if existed else 'created'}: {home}")
        return 0
    if not home.is_dir():
        raise CliError(f"no Life Mentor store at {home}; run `life-mentor init` first")
    store = PrivateStore(home).ensure()
    prof = Profile(store)
    if args.cmd == "consent":
        return _cmd_consent(args, prof)
    if args.cmd == "fact":
        return _cmd_fact(args, prof)
    if args.cmd == "profile":
        return _cmd_profile(args, prof)
    if args.cmd == "tax":
        rep = income_tax_from_profile(
            prof, load_tax_pack(), as_of=as_of, regime=args.regime, tax_year=args.tax_year
        )
        return _emit(*_single("Income tax", rep, as_of, generated, DISCLAIMERS["finance"]))
    if args.cmd == "take-home":
        section = finance_sections(prof, as_of=as_of, regime=args.regime, tax_year=args.tax_year)[0]
        rendered = render_report(
            "take-home",
            "Take-home pay",
            [section],
            as_of=as_of,
            generated_on=generated,
            disclaimer=DISCLAIMERS["finance"],
        )
        return _emit(rendered.markdown, rendered.disposition)
    if args.cmd == "decide":
        return _cmd_decide(args, prof, store, as_of, generated)
    if args.cmd == "confirm":
        return _cmd_confirm(args, store, generated)
    if args.cmd == "report":
        return _cmd_report(args, prof, as_of, generated)
    if args.cmd == "journal":
        return _cmd_journal(store)
    if args.cmd == "habits":
        return _cmd_habits(args, store, as_of)
    return _cmd_import(args, store, as_of, generated)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except ConsentRequired as exc:
        print(
            f"error: no consent for {exc.category} data; grant it with: "
            f'life-mentor consent grant {exc.category} --purpose "..."',
            file=sys.stderr,
        )
        return 2
    except (
        CliError,
        InsecureStore,
        JournalTampered,
        RulePackBlocked,
        KeyError,
        ValueError,
        TypeError,
        csv.Error,
        OSError,  # unreadable/unwritable paths: a message, not a traceback
        ArithmeticError,  # decimal.Overflow / InvalidOperation on absurd inputs
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
