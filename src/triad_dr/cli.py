"""Command-line interface for triad_dr.

Primary consumer: a Claude Code SessionStart hook (`triad-dr inbox`), which
makes graceful degradation the dominant constraint — this CLI must never
break a session start. Every error path exits 0; the three expected store
errors (no project, expired credentials, missing table) print one line to
stderr, suppressable with `--quiet`. See `decision-registers-mcp-spec.md`
section 5 item 7 for the `dr_due` semantics and section 1 ("The escalation
queue") for why blocked workers are surfaced first. `due` wraps `Store.due`
unchanged; `inbox` is the union of `Store.escalations` (open, blocking) and
`Store.due`, blocked workers first because someone is stopped right now,
whereas an overdue review is merely late.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Any

import boto3
from botocore.config import Config

from .errors import CredentialsError, NoProjectError, TableMissingError
from .models import Escalation, Record
from .store import Store

DEFAULT_TABLE_NAME = "triad-decision-registers"
_MAX_LINE_WIDTH = 100
_SOFT_ERRORS = (NoProjectError, CredentialsError, TableMissingError)


def _build_store() -> Store:
    """Build a `Store` on `DR_TABLE` (default triad-decision-registers).

    Returns:
        A `Store` with connect/read timeouts and capped retries, so a
        network stall never hangs a session start.
    """
    table_name = os.environ.get("DR_TABLE", DEFAULT_TABLE_NAME)
    config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2})
    return Store(table_name=table_name, resource=boto3.resource("dynamodb", config=config))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse CLI arguments (argv defaults to `sys.argv[1:]`)."""
    parser = argparse.ArgumentParser(prog="triad-dr", description="Decision registers CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    due = subparsers.add_parser("due", help="List IDRs past their review_date.")
    scope = due.add_mutually_exclusive_group()
    scope.add_argument("--project", help="Project slug (overrides DR_PROJECT).")
    scope.add_argument("--all-projects", action="store_true", help="Check every project.")
    due.add_argument("--format", choices=["text", "json"], default="text")
    due.add_argument("--quiet", action="store_true", help="Suppress stderr on soft errors.")

    inbox = subparsers.add_parser(
        "inbox", help="List open blocking escalations, then IDRs past their review_date."
    )
    inbox.add_argument("--project", help="Project slug (overrides DR_PROJECT).")
    inbox.add_argument("--format", choices=["text", "json"], default="text")
    inbox.add_argument("--quiet", action="store_true", help="Suppress stderr on soft errors.")
    return parser.parse_args(argv)


def _collect_due(store: Store, args: argparse.Namespace) -> list[Record]:
    """Gather due IDRs for `args`' scope (one project or `--all-projects`).

    Returns:
        Due IDR records, most overdue first.

    Raises:
        NoProjectError: No project resolved and `--all-projects` not set.
        CredentialsError: AWS credentials are missing or expired.
        TableMissingError: The DynamoDB table does not exist.
    """
    if not args.all_projects:
        return store.due(project=args.project)
    due: list[Record] = []
    for entry in store.projects():
        due.extend(store.due(project=entry["project"]))
    due.sort(key=lambda r: str(r.fields.get("review_date", "")))
    return due


def _truncate(text: str, max_len: int) -> str:
    """Truncate `text` to `max_len` chars (incl. ellipsis) for line width."""
    return text if len(text) <= max_len else text[: max(max_len - 1, 0)] + "…"


def _row(record: Record, today: date) -> dict[str, Any]:
    """Build the display dict for one due IDR.

    Returns:
        A dict with ref, project, title, review_date, days_overdue.
    """
    review_date = str(record.fields.get("review_date", ""))
    days_overdue = (today - Store._parse_dt(review_date).date()).days
    return {
        "ref": record.ref,
        "project": record.project,
        "title": record.title,
        "review_date": review_date,
        "days_overdue": days_overdue,
    }


def _format_text(due: list[Record], show_project: bool) -> str:
    """Render due IDRs as a compact report, sized to stay under ~100 cols.

    Args:
        due: Due IDR records, most overdue first.
        show_project: Include a project column (set for `--all-projects`).
    """
    today = datetime.now(timezone.utc).date()
    rows = [_row(r, today) for r in due]
    due_strs = [f"due {r['review_date']} ({r['days_overdue']}d)" for r in rows]

    ref_w = max(len(r["ref"]) for r in rows)
    proj_w = max((len(r["project"]) for r in rows), default=0)
    due_w = max(len(s) for s in due_strs)
    prefix_w = 2 + ref_w + 2 + (proj_w + 1 if show_project else 0) + due_w + 2
    title_budget = max(20, _MAX_LINE_WIDTH - prefix_w)

    count = len(rows)
    header = f"{count} bet{'' if count == 1 else 's'} {'is' if count == 1 else 'are'} past review:"
    lines = [header]
    for row, due_str in zip(rows, due_strs):
        line = f"  {row['ref'].ljust(ref_w)}  "
        if show_project:
            line += f"{row['project'].ljust(proj_w)} "
        line += f"{due_str.ljust(due_w)}  {_truncate(row['title'], title_budget)}"
        lines.append(line)
    return "\n".join(lines)


def _run_due(args: argparse.Namespace) -> int:
    """Execute `due`: print nothing when nothing is due. Always returns 0."""
    store = _build_store()
    due = _collect_due(store, args)
    if not due:
        return 0
    if args.format == "json":
        today = datetime.now(timezone.utc).date()
        print(json.dumps([_row(r, today) for r in due], indent=2))
    else:
        print(_format_text(due, show_project=args.all_projects))
    return 0


def _blocked_row(escalation: Escalation, today: date) -> dict[str, Any]:
    """Build the display dict for one open blocking escalation.

    Args:
        escalation: The escalation to summarize.
        today: The date to measure age against.

    Returns:
        A dict with ref, project, question, raised_at, days_ago.
    """
    days_ago = (today - Store._parse_dt(escalation.raised_at).date()).days
    return {
        "ref": escalation.ref,
        "project": escalation.project,
        "question": escalation.question,
        "raised_at": escalation.raised_at,
        "days_ago": days_ago,
    }


def _format_blocked_text(blocked: list[Escalation]) -> str:
    """Render open blocking escalations as a compact report.

    Args:
        blocked: Open, blocking escalations, in the order to display
            (see `Store.escalations` for ordering).

    Returns:
        A header line plus one line per escalation, sized to stay under
        ~100 columns (see `_MAX_LINE_WIDTH`).
    """
    today = datetime.now(timezone.utc).date()
    rows = [_blocked_row(e, today) for e in blocked]
    age_strs = [f"(raised {r['days_ago']}d ago)" for r in rows]

    ref_w = max(len(r["ref"]) for r in rows)
    age_w = max(len(s) for s in age_strs)
    prefix_w = 2 + ref_w + 2 + age_w + 2
    question_budget = max(20, _MAX_LINE_WIDTH - prefix_w)

    count = len(rows)
    header = f"{count} worker{'' if count == 1 else 's'} blocked:"
    lines = [header]
    for row, age_str in zip(rows, age_strs):
        question = _truncate(row["question"], question_budget)
        lines.append(f"  {row['ref'].ljust(ref_w)}  {question}  {age_str}")
    return "\n".join(lines)


def _inbox_payload(blocked: list[Escalation], due: list[Record]) -> dict[str, Any]:
    """Build the `--format json` payload for `inbox`.

    Args:
        blocked: Open, blocking escalations.
        due: Past-due IDRs.

    Returns:
        ``{"blocked": [...], "overdue": [...]}``, using the same row shapes
        as the text renderers.
    """
    today = datetime.now(timezone.utc).date()
    return {
        "blocked": [_blocked_row(e, today) for e in blocked],
        "overdue": [_row(r, today) for r in due],
    }


def _run_inbox(args: argparse.Namespace) -> int:
    """Execute `inbox`: open blocking escalations, then bets past review.

    Resolving the project is fatal to the whole command on failure — there
    is nothing left to scope either query to, so that error is left to
    propagate to `main`'s handler exactly like `due`'s. Once the project is
    resolved, the two sections are fetched independently: a soft error in
    one does not suppress output from the other, so a stale escalations
    read (say) still prints whatever bets are due.

    Args:
        args: Parsed `inbox` arguments.

    Returns:
        Always 0.

    Raises:
        NoProjectError: No project could be resolved.
        CredentialsError: AWS credentials are missing or expired while
            resolving the project (only reachable when the project must be
            looked up from the registry).
        TableMissingError: The DynamoDB table does not exist while
            resolving the project.
    """
    store = _build_store()
    resolved_project = store.resolve_project(args.project)

    blocked: list[Escalation] = []
    due: list[Record] = []
    errors: list[str] = []
    try:
        blocked = store.escalations(
            status="open", blocking_only=True, project=resolved_project
        )
    except _SOFT_ERRORS as exc:
        errors.append(str(exc))
    try:
        due = store.due(project=resolved_project)
    except _SOFT_ERRORS as exc:
        message = str(exc)
        if message not in errors:
            errors.append(message)

    if not args.quiet:
        for message in errors:
            print(message, file=sys.stderr)

    if not blocked and not due:
        return 0
    if args.format == "json":
        print(json.dumps(_inbox_payload(blocked, due), indent=2))
    else:
        sections = []
        if blocked:
            sections.append(_format_blocked_text(blocked))
        if due:
            sections.append(_format_text(due, show_project=False))
        print("\n\n".join(sections))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list, or None to use `sys.argv[1:]`.

    Returns:
        Always 0 — this CLI backs a session-start hook and must never
        propagate a nonzero exit or a stack trace. The three expected
        store errors (see module docstring) print one line to stderr;
        any other unexpected error is swallowed here too, deliberately,
        for the same reason.
    """
    args = _parse_args(argv)
    try:
        if args.command == "inbox":
            return _run_inbox(args)
        return _run_due(args)
    except _SOFT_ERRORS as exc:
        if not args.quiet:
            print(str(exc), file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 - deliberate: never break a session start.
        if not args.quiet:
            print(f"triad-dr: unexpected error: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
