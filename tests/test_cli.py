"""Tests for `triad_dr.cli` (the `triad-dr due` and `triad-dr inbox` commands).

Reuses the moto fixtures from `tests/conftest.py` (`aws_credentials`,
`dynamodb_resource`, `store`) — see that module for the mocked-table
pattern. `cli.main()` builds its own `boto3.resource("dynamodb", ...)`
internally rather than taking one via dependency injection (that's how a
real hook invocation works), so tests point it at the same mocked table by
setting `DR_TABLE` to the fixture's table name and running inside the
active `mock_aws()` context that `dynamodb_resource`/`store` provide.

Covers the task's required cases for `due`:
  - a past-due IDR appears in text output; after a review it does not
  - nothing due -> empty stdout, exit 0
  - no project configured -> exit 0, message on stderr, stdout empty
  - --quiet suppresses the stderr message and still exits 0
  - --format json emits parseable JSON with the expected keys
  - --all-projects spans multiple project partitions

And for `inbox`:
  - a blocking escalation appears in text output; a non-blocking one does not
  - an overdue IDR appears; blocked renders above bets when both are present
  - both sections empty -> empty stdout, exit 0
  - no project configured -> exit 0, message on stderr, stdout empty
  - --quiet suppresses the stderr message and still exits 0
  - --format json emits parseable JSON with the documented keys
"""

from __future__ import annotations

import json

import pytest

from triad_dr import cli
from triad_dr.store import Store

# Must match TABLE_NAME in tests/conftest.py's dynamodb_resource fixture.
TABLE_NAME = "triad-decision-registers-test"

PAST_REVIEW_DATE = "2020-01-01"
FUTURE_REVIEW_DATE = "2099-01-01"


@pytest.fixture(autouse=True)
def _table_name_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's own boto3 resource at the moto-mocked test table."""
    monkeypatch.setenv("DR_TABLE", TABLE_NAME)


def _make_past_due_idr(store: Store, project: str, title: str = "Ship the thing") -> str:
    """Create an IDR whose review_date is already past. Returns its ref."""
    record, _warnings = store.create(
        "IDR",
        title=title,
        fields={"review_date": PAST_REVIEW_DATE},
        project=project,
    )
    return record.ref


def _make_escalation(
    store: Store,
    project: str,
    question: str = "Switch the ranking model to a smaller variant?",
    blocking: bool = True,
) -> str:
    """File an open escalation (blocking by default). Returns its ref."""
    escalation, _warnings = store.escalate(
        question=question,
        context="Ranking pass stalled on a model choice.",
        options=[],
        recommendation="",
        cost_of_delay="",
        blocking=blocking,
        project=project,
    )
    return escalation.ref


# ----------------------------------------------------------------------
# Past-due IDR appears in text output; not after a review
# ----------------------------------------------------------------------


def test_past_due_idr_appears_in_text_output(store: Store, capsys: pytest.CaptureFixture) -> None:
    """A past-due IDR shows up in the default text report."""
    ref = _make_past_due_idr(store, "acme-app", title="Ship saved-search to 10 beta users")
    capsys.readouterr()  # drain structlog noise from the seed call above

    exit_code = cli.main(["due", "--project", "acme-app"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert ref in captured.out
    assert "saved-search" in captured.out
    assert "past review" in captured.out
    assert captured.err == ""


def test_reviewed_idr_no_longer_appears(store: Store, capsys: pytest.CaptureFixture) -> None:
    """After a dr_review dated after review_date, the IDR drops off the report."""
    ref = _make_past_due_idr(store, "acme-app")
    store.review(ref, verdict="met", evidence="Talked to 10 users.", project="acme-app")
    capsys.readouterr()  # drain structlog noise from the seed calls above

    exit_code = cli.main(["due", "--project", "acme-app"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


# ----------------------------------------------------------------------
# Nothing due
# ----------------------------------------------------------------------


def test_nothing_due_is_silent(store: Store, capsys: pytest.CaptureFixture) -> None:
    """No due IDRs -> empty stdout, exit 0, no stderr noise."""
    store.create(
        "IDR",
        title="Not due yet",
        fields={"review_date": FUTURE_REVIEW_DATE},
        project="acme-app",
    )
    capsys.readouterr()  # drain structlog noise from the seed call above

    exit_code = cli.main(["due", "--project", "acme-app"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


# ----------------------------------------------------------------------
# No project configured
# ----------------------------------------------------------------------


def test_no_project_configured_degrades_silently(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """No DR_PROJECT and no --project -> exit 0, one stderr line, empty stdout."""
    exit_code = cli.main(["due"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err.strip() != ""
    assert captured.err.count("\n") <= 1
    assert "DR_PROJECT" in captured.err


def test_quiet_suppresses_no_project_message(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """--quiet suppresses the stderr message on the no-project soft error."""
    exit_code = cli.main(["due", "--quiet"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


# ----------------------------------------------------------------------
# --format json
# ----------------------------------------------------------------------


def test_format_json_emits_parseable_json_with_expected_keys(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """--format json emits a JSON array with ref/project/title/review_date/days_overdue."""
    ref = _make_past_due_idr(store, "acme-app", title="Ship saved-search")
    capsys.readouterr()  # drain structlog noise from the seed call above

    exit_code = cli.main(["due", "--project", "acme-app", "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert row["ref"] == ref
    assert row["project"] == "acme-app"
    assert row["title"] == "Ship saved-search"
    assert row["review_date"] == PAST_REVIEW_DATE
    assert isinstance(row["days_overdue"], int)
    assert row["days_overdue"] > 0


# ----------------------------------------------------------------------
# --all-projects
# ----------------------------------------------------------------------


def test_all_projects_spans_multiple_project_partitions(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """--all-projects reports due IDRs from every project in the registry."""
    ref_a = _make_past_due_idr(store, "acme-app", title="Ship saved-search to 10 beta users")
    ref_b = _make_past_due_idr(store, "example-project", title="Reach 500 subscribers")
    capsys.readouterr()  # drain structlog noise from the seed calls above

    exit_code = cli.main(["due", "--all-projects"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert ref_a in captured.out
    assert ref_b in captured.out
    assert "acme-app" in captured.out
    assert "example-project" in captured.out


# ----------------------------------------------------------------------
# inbox: blocking escalation appears, non-blocking one does not
# ----------------------------------------------------------------------


def test_inbox_blocking_escalation_appears_non_blocking_does_not(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """A blocking escalation shows up in inbox text; a non-blocking one does not."""
    blocking_ref = _make_escalation(
        store, "acme-app", question="Switch the ranking model to a smaller variant?", blocking=True
    )
    _make_escalation(
        store, "acme-app", question="Consider renaming the ranking schema later?", blocking=False
    )
    capsys.readouterr()  # drain structlog noise from the seed calls above

    exit_code = cli.main(["inbox", "--project", "acme-app"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert blocking_ref in captured.out
    assert "Switch the ranking model to a smaller variant?" in captured.out
    assert "Consider renaming the ranking schema later?" not in captured.out
    assert "blocked" in captured.out
    assert captured.err == ""


# ----------------------------------------------------------------------
# inbox: overdue IDR appears; blocked renders above bets when both present
# ----------------------------------------------------------------------


def test_inbox_blocked_renders_above_overdue_bets(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """Both sections present: the blocked section appears before the bets section."""
    idr_ref = _make_past_due_idr(store, "acme-app", title="Ship saved-search to 10 beta users")
    esc_ref = _make_escalation(store, "acme-app", blocking=True)
    capsys.readouterr()  # drain structlog noise from the seed calls above

    exit_code = cli.main(["inbox", "--project", "acme-app"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert idr_ref in captured.out
    assert esc_ref in captured.out
    assert "past review" in captured.out
    assert "blocked" in captured.out
    assert captured.out.index(esc_ref) < captured.out.index(idr_ref)
    assert captured.err == ""


# ----------------------------------------------------------------------
# inbox: both sections empty
# ----------------------------------------------------------------------


def test_inbox_both_empty_is_silent(store: Store, capsys: pytest.CaptureFixture) -> None:
    """No blocking escalations and nothing due -> empty stdout, exit 0, no stderr noise."""
    exit_code = cli.main(["inbox", "--project", "acme-app"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


# ----------------------------------------------------------------------
# inbox: no project configured
# ----------------------------------------------------------------------


def test_inbox_no_project_configured_degrades_silently(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """No DR_PROJECT and no --project -> exit 0, one stderr line, empty stdout."""
    exit_code = cli.main(["inbox"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err.strip() != ""
    assert captured.err.count("\n") <= 1
    assert "DR_PROJECT" in captured.err


def test_inbox_quiet_suppresses_no_project_message(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """--quiet suppresses the stderr message on the no-project soft error."""
    exit_code = cli.main(["inbox", "--quiet"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


# ----------------------------------------------------------------------
# inbox: --format json
# ----------------------------------------------------------------------


def test_inbox_format_json_emits_documented_keys(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """--format json emits {"blocked": [...], "overdue": [...]} and parses."""
    idr_ref = _make_past_due_idr(store, "acme-app", title="Ship saved-search")
    esc_ref = _make_escalation(store, "acme-app", blocking=True)
    capsys.readouterr()  # drain structlog noise from the seed calls above

    exit_code = cli.main(["inbox", "--project", "acme-app", "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert set(payload.keys()) == {"blocked", "overdue"}
    assert len(payload["blocked"]) == 1
    assert payload["blocked"][0]["ref"] == esc_ref
    assert len(payload["overdue"]) == 1
    assert payload["overdue"][0]["ref"] == idr_ref


# ----------------------------------------------------------------------
# due still behaves exactly as before (no regression from adding inbox)
# ----------------------------------------------------------------------


def test_due_subcommand_unaffected_by_inbox_addition(
    store: Store, capsys: pytest.CaptureFixture
) -> None:
    """`due` still reports only bets past review, ignoring open escalations."""
    idr_ref = _make_past_due_idr(store, "acme-app", title="Ship saved-search to 10 beta users")
    esc_ref = _make_escalation(store, "acme-app", blocking=True)
    capsys.readouterr()  # drain structlog noise from the seed calls above

    exit_code = cli.main(["due", "--project", "acme-app"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert idr_ref in captured.out
    assert esc_ref not in captured.out
    assert "past review" in captured.out
    assert captured.err == ""
