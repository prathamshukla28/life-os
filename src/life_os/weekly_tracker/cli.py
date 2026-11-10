"""``weekly-tracker`` command line (stdlib argparse).

    weekly-tracker show  [--week 2026-W41] [--repo PATH | --no-git]
    weekly-tracker write [--week ...] [--out DIR] [--force]
    weekly-tracker plan add --day mon --text "..." [--week ...]
    weekly-tracker plan done ID | plan skip ID | plan move ID --day tue [--week ...]
    weekly-tracker plan list [--week ...]

Global: ``--home`` (planner + default output; else $WEEKLY_TRACKER_HOME or
~/.weekly_tracker), ``--life-mentor-home`` (else $LIFE_MENTOR_HOME or ~/.life_mentor),
``--no-life-mentor``. ``show`` only reads; ``write`` refuses to overwrite without --force.
Exit codes: 0 ok, 1 refused/error, 2 usage.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path

from life_os.life_mentor.security import InsecureStore, JournalTampered, PrivateStore, write_file_atomic
from life_os.life_mentor.security import resolve_home as resolve_lm_home

from .planner import Planner, resolve_home
from .render import collect, render
from .week import DAY_NAMES, IsoWeek, day_index

__all__ = ["main"]


def _day(text: str) -> str:
    try:
        return DAY_NAMES[day_index(text)]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _iso_date(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="weekly-tracker", description="Weekly summary: planner, git work, Life Mentor."
    )
    p.add_argument("--home", help="Weekly Tracker data directory")
    p.add_argument("--life-mentor-home", help="Life Mentor data directory (read-only)")
    p.add_argument("--no-life-mentor", action="store_true", help="leave out Life Mentor data")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("show", "write"):
        sp = sub.add_parser(name, help=f"{name} the Markdown summary for a week")
        sp.add_argument("--week", help="ISO week, e.g. 2026-W41 (default: this week)")
        sp.add_argument("--repo", help="git repository (default: current directory)")
        sp.add_argument("--no-git", action="store_true", help="leave out git work")
        sp.add_argument("--tz", help="time zone for git dates, e.g. Asia/Kolkata")
        sp.add_argument("--generated-on", type=_iso_date, help="date printed in the header (YYYY-MM-DD)")
        if name == "write":
            sp.add_argument("--out", help="output directory (default: <home>/weeks)")
            sp.add_argument("--force", action="store_true", help="overwrite an existing file")
    plan = sub.add_parser("plan", help="manual planner")
    psub = plan.add_subparsers(dest="plan_command", required=True)
    add = psub.add_parser("add", help="add a task")
    add.add_argument("--day", required=True, type=_day)
    add.add_argument("--text", required=True)
    add.add_argument("--week")
    for name in ("done", "skip"):
        psub.add_parser(name, help=f"mark a task {name}").add_argument("task_id")
    move = psub.add_parser("move", help="move a planned task")
    move.add_argument("task_id")
    move.add_argument("--day", required=True, type=_day)
    move.add_argument("--week")
    psub.add_parser("list", help="list tasks of a week").add_argument("--week")
    ics = psub.add_parser("ics", help="export a week's tasks to a .ics calendar file")
    ics.add_argument("--week")
    ics.add_argument("--out", help="output .ics file (default: stdout)")
    ics.add_argument("--force", action="store_true", help="overwrite an existing file")
    return p


def _week(text: str | None, today: date) -> IsoWeek:
    return IsoWeek.parse(text) if text else IsoWeek.containing(today)


def _write_out(target: Path, data: bytes, force: bool) -> None:
    """Atomic 0600 write that never clobbers without *force* (see ``write_file_atomic``).

    A failure is reported against *target*, never the hidden temporary file.
    """
    try:
        write_file_atomic(target, data, overwrite=force)
    except FileExistsError:
        raise
    except OSError as exc:
        raise OSError(exc.errno, exc.strerror or str(exc), str(target)) from exc


def _refusal(target: Path, force: bool) -> str | None:
    """Why *target* (an ``--out`` path) cannot be written, or None."""
    if target.is_dir():
        return f"{target} is a directory; give a file path"
    if target.exists() and not force:
        return f"{target} exists; use --force to overwrite"
    return None


def _report(args: argparse.Namespace, planner: Planner, week: IsoWeek, today: date) -> str:
    lm_home = None if args.no_life_mentor else resolve_lm_home(args.life_mentor_home)
    model = collect(
        week,
        generated_on=args.generated_on or today.isoformat(),
        planner=planner,
        life_mentor_home=lm_home,
        repo=args.repo or Path.cwd(),
        git=not args.no_git,
        git_tz=args.tz,
    )
    return render(model)


def _run(args: argparse.Namespace, today: date) -> int:
    planner = Planner(resolve_home(args.home))
    if args.command == "plan":
        cmd = args.plan_command
        if cmd == "add":
            print(planner.add(_week(args.week, today), args.day, args.text).task_id)
        elif cmd in ("done", "skip"):
            task = getattr(planner, cmd)(args.task_id)
            print(f"{task.task_id} {task.status}")
        elif cmd == "move":
            task = planner.move(args.task_id, _week(args.week, today), args.day)
            print(f"{task.task_id} moved to {task.week} {task.day}")
        elif cmd == "ics":
            from .ics import planner_to_calendar

            text = planner_to_calendar(planner.tasks(_week(args.week, today)))
            if args.out:
                target = Path(args.out).expanduser()
                refusal = _refusal(target, args.force)
                if refusal:
                    print(f"error: {refusal}", file=sys.stderr)
                    return 1
                _write_out(target, text.encode("utf-8"), args.force)
                print(target)
            else:
                # Bytes, so the CRLF line endings RFC 5545 requires survive on every platform.
                sys.stdout.flush()
                sys.stdout.buffer.write(text.encode("utf-8"))
                sys.stdout.buffer.flush()
        else:
            for t in planner.tasks(_week(args.week, today)):
                print(f"{t.task_id}\t{t.day}\t{t.status}\t{t.text}")
        return 0
    week = _week(args.week, today)
    text = _report(args, planner, week, today)
    if args.command == "show":
        sys.stdout.write(text)
        return 0
    name = f"{week}.md"
    if args.out:
        target = Path(args.out).expanduser() / name
        refusal = _refusal(target, args.force)
        if refusal:
            print(f"error: {refusal}", file=sys.stderr)
            return 1
        _write_out(target, text.encode("utf-8"), args.force)
    else:
        store = PrivateStore(planner.home)
        target = store.path(f"weeks/{name}")
        if target.exists() and not args.force:
            print(f"error: {target} exists; use --force to overwrite", file=sys.stderr)
            return 1
        store.write_bytes(f"weeks/{name}", text.encode("utf-8"))
    print(target)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    today = datetime.now(timezone.utc).astimezone().date()  # local calendar date
    if getattr(args, "week", None):
        try:
            IsoWeek.parse(args.week)
        except ValueError as exc:
            print(f"error: invalid --week: {exc}", file=sys.stderr)
            return 2
    try:
        return _run(args, today)
    except KeyError as exc:
        # The planner's KeyError already reads "unknown task 't9'".
        print(f"error: {exc.args[0] if exc.args else 'unknown task'}", file=sys.stderr)
        return 1
    except JournalTampered as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except InsecureStore as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileExistsError as exc:
        print(f"error: {exc.filename2 or exc.filename} exists; use --force to overwrite", file=sys.stderr)
        return 1
    except OSError as exc:
        # Unwritable --out directory, full disk, ...: a clean message, not a traceback.
        print(f"error: {exc.strerror or exc} ({exc.filename or 'file system'})", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
