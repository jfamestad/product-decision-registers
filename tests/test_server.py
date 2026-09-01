"""Tests for `triad_dr.server` — the MCP tool layer built on top of `Store`.

Reuses the moto fixtures from `tests/conftest.py` (`aws_credentials`,
`dynamodb_resource`, `store`). `Store`'s own business logic (sequencing,
status-lifecycle validation, governance-warning computation, the kill
cascade, ...) is already covered by `tests/test_store.py`; this file covers
only what `server.py` itself adds:

  - each tool's response shape (always `ref`/`title`/`status` where a
    record or summary row is returned)
  - warnings computed by `Store` reach the tool response and are not
    swallowed
  - `Store`'s typed errors arrive at the caller as actionable `ToolError`
    messages, never tracebacks
  - `dr_list` has no `persona` filter axis (spec section 5 item 3)
  - `dr_export`'s file-rendering (frontmatter + "## Status history" +
    "## Reviews")
  - `dr_template`'s per-type field skeleton
  - the tool-layer-only response shaping in `dr_review` (echoing an IDR's
    success/kill criteria, the cause-routing message) and `dr_set_status`
    (the `orphaned_refs` field)
  - the three new governance checks reach the tool layer unmuted: the
    placeholder-lint and requires-attestation commitment-gate warnings from
    `dr_set_status(..., "accepted")`, and `dr_list(stale=True)`'s documented
    response shape (`stale_reasons` per row)

Every call goes through the real `MCPServer.call_tool()` path (not the
underlying Python functions directly), so a failure surfaces exactly the
way spec section 4 says it must: `ToolError` with a human-readable message
for known/typed failures, never a raw traceback.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from triad_dr.events_queue import EventQueue
from triad_dr.server import _TEMPLATE_FIELDS, build_server
from triad_dr.store import Store

PROJECT = "acme-app"


# ----------------------------------------------------------------------
# Fixtures / helpers
# ----------------------------------------------------------------------


@pytest.fixture
def server(store: Store) -> MCPServer:
    """Build an `MCPServer` wired to the moto-mocked `store` fixture."""
    return build_server(store)


@pytest.fixture
def event_queue(dynamodb_resource: Any) -> EventQueue:
    """Build an `EventQueue` against a moto-mocked SQS queue.

    Depends on `dynamodb_resource`, not because it needs DynamoDB, but
    because that fixture is what has `mock_aws()` active for the
    duration of the test -- moto mocks every AWS service reached while
    that context is open, SQS included, so no separate `mock_aws()`
    context is needed here.

    Args:
        dynamodb_resource: The moto-mocked DynamoDB resource fixture
            (from `conftest.py`), depended on solely to keep `mock_aws()`
            active.
    """
    sqs_client = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs_client.create_queue(
        QueueName="triad-decision-registers-events-queue-test"
    )["QueueUrl"]
    return EventQueue(queue_url=queue_url, client=sqs_client)


@pytest.fixture
def events_server(store: Store, event_queue: EventQueue) -> MCPServer:
    """Build an `MCPServer` wired to both the record store and the events queue."""
    return build_server(store, event_queue=event_queue)


def _call(server: MCPServer, name: str, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool through the real MCP call path and parse its JSON.

    Args:
        server: The server under test.
        name: Tool name, e.g. "dr_create".
        **arguments: Tool arguments.

    Returns:
        The parsed JSON response body.
    """
    result = asyncio.run(server.call_tool(name, arguments))
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def _call_expecting_error(server: MCPServer, name: str, **arguments: Any) -> str:
    """Invoke a tool expecting it to fail, and return the error message.

    Args:
        server: The server under test.
        name: Tool name.
        **arguments: Tool arguments.

    Returns:
        `str(exc)` from the raised `ToolError` — the message a real MCP
        client would show the calling model.
    """
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(server.call_tool(name, arguments))
    return str(exc_info.value)


# ----------------------------------------------------------------------
# Response shape: ref/title/status everywhere, per spec section 5 preamble
# ----------------------------------------------------------------------


def test_dr_create_returns_full_record_with_ref_title_status(server: MCPServer) -> None:
    """dr_create returns the full record, including ref/title/status."""
    payload = _call(server, "dr_create", rec_type="ADR", title="Use single-table design", project=PROJECT)

    assert payload["ref"] == "ADR-0001"
    assert payload["title"] == "Use single-table design"
    assert payload["status"] == "proposed"
    assert payload["persona"] == "builder"
    assert payload["warnings"] == [] or isinstance(payload["warnings"], list)


def test_dr_list_summary_rows_include_ref_title_status(server: MCPServer) -> None:
    """dr_list summary rows always carry ref/title/status (plus persona/unfunded)."""
    _call(server, "dr_create", rec_type="ADR", title="Builder decision", project=PROJECT)
    _call(server, "dr_create", rec_type="IDR", title="Investor bet", project=PROJECT)

    payload = _call(server, "dr_list", project=PROJECT)

    assert len(payload["records"]) == 2
    for row in payload["records"]:
        assert set(row) >= {"ref", "title", "status", "persona", "rec_type"}
    adr_row = next(r for r in payload["records"] if r["rec_type"] == "ADR")
    idr_row = next(r for r in payload["records"] if r["rec_type"] == "IDR")
    assert adr_row["unfunded"] is True  # no implements-bet link
    assert "unfunded" not in idr_row  # concept doesn't apply to IDRs


def test_dr_search_and_dr_due_summary_rows_include_ref_title_status(server: MCPServer) -> None:
    """dr_search and dr_due summary rows also carry ref/title/status."""
    _call(
        server,
        "dr_create",
        rec_type="IDR",
        title="Ship saved-search to beta users",
        fields={"review_date": "2020-01-01"},
        project=PROJECT,
    )

    search_payload = _call(server, "dr_search", query="saved-search", project=PROJECT)
    assert len(search_payload["results"]) == 1
    row = search_payload["results"][0]
    assert row["ref"] == "IDR-0001"
    assert row["title"] == "Ship saved-search to beta users"
    assert row["status"] == "proposed"

    due_payload = _call(server, "dr_due", project=PROJECT)
    assert len(due_payload["due"]) == 1
    due_row = due_payload["due"][0]
    assert due_row["ref"] == "IDR-0001"
    assert due_row["title"] == "Ship saved-search to beta users"
    assert due_row["status"] == "proposed"
    assert due_row["review_date"] == "2020-01-01"


# ----------------------------------------------------------------------
# Warnings propagate — the product, per the task brief
# ----------------------------------------------------------------------


def test_adr_with_no_implements_bet_link_warns_and_still_succeeds(server: MCPServer) -> None:
    """An ADR created with no implements-bet link returns a warning AND succeeds."""
    payload = _call(server, "dr_create", rec_type="ADR", title="Unfunded work", project=PROJECT)

    assert payload["status"] == "proposed"  # the write succeeded, not blocked
    assert any("implements-bet" in w for w in payload["warnings"])


def test_mdr_with_neither_user_evidence_nor_evidence_plan_warns(server: MCPServer) -> None:
    """An MDR with neither user_evidence nor evidence_plan warns but still succeeds."""
    payload = _call(server, "dr_create", rec_type="MDR", title="Positioning experiment", project=PROJECT)

    assert payload["status"] == "proposed"
    assert any("user_evidence" in w and "evidence_plan" in w for w in payload["warnings"])


def test_dr_set_status_surfaces_orphaned_refs_and_warning_without_cascading(server: MCPServer) -> None:
    """Deprecating a governing IDR reports orphaned refs and warns, without changing their status."""
    idr = _call(server, "dr_create", rec_type="IDR", title="Standing ops bet", status="accepted", project=PROJECT)
    adr = _call(
        server,
        "dr_create",
        rec_type="ADR",
        title="Builder work under the bet",
        links=[{"ref": idr["ref"], "relation": "implements-bet"}],
        project=PROJECT,
    )

    payload = _call(server, "dr_set_status", ref=idr["ref"], status="deprecated", project=PROJECT)

    assert payload["orphaned_refs"] == [adr["ref"]]
    assert any("orphaned" in w for w in payload["warnings"])

    refetched_adr = _call(server, "dr_get", ref=adr["ref"], project=PROJECT)
    assert refetched_adr["status"] == "proposed"  # unchanged, not cascaded


# ----------------------------------------------------------------------
# Typed errors -> actionable ToolError messages, never tracebacks
# ----------------------------------------------------------------------


def test_no_project_error_names_remediation_and_known_projects(server: MCPServer) -> None:
    """No DR_PROJECT, no project= -> actionable message with known projects."""
    _call(server, "dr_create", rec_type="ADR", title="Seed a known project", project="known-project")

    message = _call_expecting_error(server, "dr_list")

    assert "DR_PROJECT" in message
    assert 'project="' in message
    assert "known-project" in message


def test_missing_table_error_names_make_deploy(dynamodb_resource: Any) -> None:
    """A tool call against a nonexistent table surfaces the make-deploy remediation."""
    missing_store = Store(table_name="does-not-exist", resource=dynamodb_resource)
    missing_server = build_server(missing_store)

    message = _call_expecting_error(missing_server, "dr_get", ref="ADR-0001", project=PROJECT)

    assert "does-not-exist" in message
    assert "make deploy" in message


def test_illegal_status_transition_names_allowed_next_states(server: MCPServer) -> None:
    """An illegal transition (accepted -> proposed) is rejected with the allowed states named."""
    record = _call(server, "dr_create", rec_type="ADR", title="Decision", project=PROJECT)
    _call(server, "dr_set_status", ref=record["ref"], status="accepted", project=PROJECT)

    message = _call_expecting_error(server, "dr_set_status", ref=record["ref"], status="proposed", project=PROJECT)

    assert 'from "accepted" to "proposed"' in message
    assert "deprecated" in message
    assert "superseded" in message


def test_malformed_ref_is_rejected_with_actionable_message(server: MCPServer) -> None:
    """A malformed ref (dr_get) is rejected, naming the expected format."""
    message = _call_expecting_error(server, "dr_get", ref="not-a-ref", project=PROJECT)

    assert "not-a-ref" in message
    assert "ADR-0007" in message  # example of the expected format


def test_missing_cause_on_missed_idr_names_all_three_options(server: MCPServer) -> None:
    """A missed verdict on an IDR without cause errors, naming all three options."""
    idr = _call(server, "dr_create", rec_type="IDR", title="Bet on X", project=PROJECT)

    message = _call_expecting_error(
        server, "dr_review", ref=idr["ref"], verdict="missed", evidence="Nobody used it.", project=PROJECT
    )

    assert "market" in message
    assert "delivery" in message
    assert "both" in message


def test_protected_attribute_patch_names_the_right_tool(server: MCPServer) -> None:
    """dr_update rejects a protected key and names the dedicated tool to use instead."""
    record = _call(server, "dr_create", rec_type="ADR", title="Decision", project=PROJECT)

    message = _call_expecting_error(
        server, "dr_update", ref=record["ref"], patch={"status": "accepted"}, project=PROJECT
    )

    assert "dr_set_status" in message


# ----------------------------------------------------------------------
# dr_list has no persona filter axis (spec section 5 item 3)
# ----------------------------------------------------------------------


def test_dr_list_ignores_persona_argument(server: MCPServer) -> None:
    """Passing persona= to dr_list has no filtering effect; it is not wired to Store."""
    _call(server, "dr_create", rec_type="ADR", title="Builder decision", project=PROJECT)
    _call(server, "dr_create", rec_type="IDR", title="Investor bet", project=PROJECT)
    _call(server, "dr_create", rec_type="MDR", title="Seller motion", project=PROJECT)

    payload = _call(server, "dr_list", persona="builder", project=PROJECT)

    # If persona filtered, only the ADR (builder) would come back. It doesn't filter.
    rec_types = {row["rec_type"] for row in payload["records"]}
    assert rec_types == {"ADR", "IDR", "MDR"}
    # persona is still present on every row, per spec section 5: it's a display
    # attribute, just not a filter.
    assert all("persona" in row for row in payload["records"])


# ----------------------------------------------------------------------
# dr_review: tool-layer response shaping (success/kill criteria, routing)
# ----------------------------------------------------------------------


def test_review_on_idr_echoes_success_and_kill_criteria(server: MCPServer) -> None:
    """dr_review on an IDR restates its own success_criteria/kill_criteria."""
    idr = _call(
        server,
        "dr_create",
        rec_type="IDR",
        title="Bet on Y",
        fields={"success_criteria": "500 signups", "kill_criteria": "under 50 in 60 days"},
        project=PROJECT,
    )

    payload = _call(
        server, "dr_review", ref=idr["ref"], verdict="met", evidence="612 signups per dashboard.", project=PROJECT
    )

    assert payload["success_criteria"] == "500 signups"
    assert payload["kill_criteria"] == "under 50 in 60 days"
    assert payload["status"] == idr["status"]  # dr_review never changes status


def test_review_missed_idr_with_delivery_cause_says_untested_not_disproven(server: MCPServer) -> None:
    """A delivery-caused miss says plainly that killing the bet would discard an untested thesis."""
    idr = _call(server, "dr_create", rec_type="IDR", title="Bet on Z", project=PROJECT)

    payload = _call(
        server,
        "dr_review",
        ref=idr["ref"],
        verdict="missed",
        evidence="Feature was never actually built as specified.",
        cause="delivery",
        project=PROJECT,
    )

    assert payload["reviews"][-1]["cause"] == "delivery"
    assert "untested" in payload["cause_routing"].lower()
    assert "not disproven" in payload["cause_routing"].lower()
    assert "ADR" in payload["cause_routing"]


def test_review_missed_idr_with_market_cause_routes_to_mdr(server: MCPServer) -> None:
    """A market-caused miss routes to a new MDR and says the thesis is disproven."""
    idr = _call(server, "dr_create", rec_type="IDR", title="Bet on W", project=PROJECT)

    payload = _call(
        server,
        "dr_review",
        ref=idr["ref"],
        verdict="missed",
        evidence="We shipped five variants and nobody found the export button.",
        cause="market",
        project=PROJECT,
    )

    assert "disproven" in payload["cause_routing"].lower()
    assert "MDR" in payload["cause_routing"]


# ----------------------------------------------------------------------
# dr_export round-trip (spec section 7 item 3)
# ----------------------------------------------------------------------


def test_dr_export_round_trip_renders_frontmatter_and_sections(server: MCPServer, tmp_path: Path) -> None:
    """create -> export -> the file has frontmatter (ref/status/persona) and both sections."""
    record = _call(
        server,
        "dr_create",
        rec_type="ADR",
        title="Use DynamoDB single-table design",
        body_md="We evaluated a few options and landed here.",
        project=PROJECT,
    )
    _call(server, "dr_set_status", ref=record["ref"], status="accepted", note="looks good", project=PROJECT)

    out_dir = str(tmp_path / "decisions")
    payload = _call(server, "dr_export", ref=record["ref"], out_dir=out_dir, project=PROJECT)

    assert len(payload["written"]) == 1
    file_path = Path(payload["written"][0])
    assert file_path.exists()
    content = file_path.read_text(encoding="utf-8")

    assert f"ref: {record['ref']}" in content
    assert 'status: accepted' in content
    assert "persona: builder" in content
    assert "We evaluated a few options and landed here." in content
    assert "## Status history" in content
    assert "## Reviews" in content
    assert "accepted" in content  # status_history entry rendered


def test_dr_export_with_no_ref_exports_every_record_of_a_type(server: MCPServer, tmp_path: Path) -> None:
    """dr_export(rec_type=...) with no ref exports every matching record."""
    _call(server, "dr_create", rec_type="MDR", title="Launch experiment one", project=PROJECT)
    _call(server, "dr_create", rec_type="MDR", title="Launch experiment two", project=PROJECT)
    _call(server, "dr_create", rec_type="ADR", title="Unrelated ADR", project=PROJECT)

    out_dir = str(tmp_path / "decisions")
    payload = _call(server, "dr_export", rec_type="MDR", out_dir=out_dir, project=PROJECT)

    assert len(payload["written"]) == 2
    for path_str in payload["written"]:
        assert Path(path_str).exists()


# ----------------------------------------------------------------------
# dr_template: skeleton contains every field named in spec section 4
# ----------------------------------------------------------------------


@pytest.mark.parametrize("rec_type", ["ADR", "IDR", "MDR"])
def test_dr_template_contains_every_spec_field_for_type(server: MCPServer, rec_type: str) -> None:
    """dr_template(rec_type) returns a skeleton naming every field from spec section 4."""
    payload = _call(server, "dr_template", rec_type=rec_type)

    assert payload["rec_type"] == rec_type
    expected_fields = _TEMPLATE_FIELDS[rec_type]
    assert payload["fields"] == expected_fields
    for field_name in expected_fields:
        assert field_name in payload["template_md"]


def test_dr_template_mdr_includes_both_evidence_plan_and_user_evidence(server: MCPServer) -> None:
    """MDR's template explicitly carries both evidence_plan and user_evidence."""
    payload = _call(server, "dr_template", rec_type="MDR")

    assert "evidence_plan" in payload["fields"]
    assert "user_evidence" in payload["fields"]
    assert "evidence_plan" in payload["template_md"]
    assert "user_evidence" in payload["template_md"]


def test_dr_template_does_not_require_a_project(server: MCPServer) -> None:
    """dr_template works with no DR_PROJECT set — it's not project-scoped."""
    payload = _call(server, "dr_template", rec_type="ADR")  # no project= passed
    assert payload["rec_type"] == "ADR"


# ----------------------------------------------------------------------
# Misc tool-layer coverage: dr_get by id, dr_projects, dr_supersede
# ----------------------------------------------------------------------


def test_dr_get_by_id(server: MCPServer) -> None:
    """dr_get(id=...) resolves the same record as dr_get(ref=...)."""
    created = _call(server, "dr_create", rec_type="ADR", title="Decision", project=PROJECT)

    by_id = _call(server, "dr_get", id=created["id"], project=PROJECT)

    assert by_id["ref"] == created["ref"]


def test_dr_get_requires_ref_or_id(server: MCPServer) -> None:
    """dr_get with neither ref nor id is a rejected call, not a crash."""
    message = _call_expecting_error(server, "dr_get", project=PROJECT)
    assert "ref" in message
    assert "id" in message


def test_dr_projects_lists_registry_without_project_scoping(server: MCPServer) -> None:
    """dr_projects works without DR_PROJECT and reports per-type counts."""
    _call(server, "dr_create", rec_type="ADR", title="Decision", project="acme-app")
    _call(server, "dr_create", rec_type="IDR", title="Bet", project="example-project")

    payload = _call(server, "dr_projects")

    slugs = {p["project"] for p in payload["projects"]}
    assert {"acme-app", "example-project"} <= slugs
    acme_app_entry = next(p for p in payload["projects"] if p["project"] == "acme-app")
    assert acme_app_entry["record_counts"]["ADR"] == 1


def test_dr_supersede_links_now_and_flips_on_acceptance(server: MCPServer) -> None:
    """dr_supersede atomically creates the replacement and supersedes the old record."""
    old = _call(server, "dr_create", rec_type="ADR", title="Use DynamoDB", project=PROJECT)
    _call(server, "dr_set_status", ref=old["ref"], status="accepted", project=PROJECT)

    payload = _call(
        server,
        "dr_supersede",
        old_ref=old["ref"],
        new_record={"title": "Use DynamoDB with single-table design"},
        project=PROJECT,
    )

    assert payload["supersedes"] == old["ref"]
    assert payload["status"] == "proposed"

    refetched_old = _call(server, "dr_get", ref=old["ref"], project=PROJECT)
    # Deferred: link now, status flip when the replacement is accepted.
    assert refetched_old["superseded_by"] == payload["ref"]
    assert refetched_old["status"] == "accepted"

    _call(server, "dr_set_status", ref=payload["ref"], status="accepted", project=PROJECT)
    after = _call(server, "dr_get", ref=old["ref"], project=PROJECT)
    assert after["status"] == "superseded"
    assert after["superseded_by"] == payload["ref"]


# ----------------------------------------------------------------------
# dr_escalate / dr_escalations / dr_answer (spec section 5 items 7a/7b/7c)
# ----------------------------------------------------------------------


def _valid_escalation_options() -> list[dict[str, Any]]:
    """Two well-formed options -- enough to avoid the <2-options warning."""
    return [
        {
            "option": "Proceed with plan A",
            "consequence": "Ships today; hard to undo once live.",
            "reversible": False,
        },
        {
            "option": "Wait for operator input",
            "consequence": "Blocks the worker until answered.",
            "reversible": True,
        },
    ]


def _escalate(
    server: MCPServer,
    question: str = "What now?",
    blocking: bool = False,
    project: str = PROJECT,
) -> dict[str, Any]:
    """Shorthand for filing a well-formed escalation through dr_escalate."""
    return _call(
        server,
        "dr_escalate",
        question=question,
        context="Context for the test.",
        options=_valid_escalation_options(),
        recommendation="Proceed with plan A.",
        cost_of_delay="Nothing until Friday.",
        blocking=blocking,
        project=project,
    )


def _stamp_escalation_raised_at(
    dynamodb_resource: Any, table_name: str, ref: str, raised_at: str, project: str = PROJECT
) -> None:
    """Directly overwrite an escalation's raised_at in the mocked table.

    Bypasses the tool layer on purpose: dr_escalate always stamps "now", so
    the ordering test needs a way to force distinct, controllable
    raised_at values.
    """
    seq_str = ref.split("-")[1]
    table = dynamodb_resource.Table(table_name)
    table.update_item(
        Key={"pk": f"PROJ#{project}", "sk": f"ESC#{seq_str}"},
        UpdateExpression="SET #raised_at = :raised_at",
        ExpressionAttributeNames={"#raised_at": "raised_at"},
        ExpressionAttributeValues={":raised_at": raised_at},
    )


def test_dr_escalate_returns_full_escalation_with_ref_and_warnings(server: MCPServer) -> None:
    """dr_escalate returns the full escalation, including ref/status, plus warnings."""
    payload = _escalate(server, question="Deploy to prod now?")

    assert payload["ref"] == "ESC-0001"
    assert payload["question"] == "Deploy to prod now?"
    assert payload["status"] == "open"
    assert payload["project"] == PROJECT
    assert isinstance(payload["warnings"], list)
    # options/recommendation/cost_of_delay are all well-formed; the only
    # warning left is the omitted governing_ref, which _escalate() doesn't pass.
    assert payload["warnings"] == [
        f'{payload["ref"]} has no governing_ref — the escalated work has no bet behind it.'
    ]


def test_dr_escalate_with_fewer_than_two_options_warns_and_still_succeeds(server: MCPServer) -> None:
    """A thin escalation -- no options, no recommendation -- warns but never blocks.

    A blocked worker must always be able to raise a hand.
    """
    payload = _call(
        server,
        "dr_escalate",
        question="Should I proceed?",
        context="Hit an authorization edge with no obvious call.",
        options=[],
        recommendation="",
        cost_of_delay="",
        project=PROJECT,
    )

    assert payload["status"] == "open"  # succeeded, not blocked
    assert any("option" in w for w in payload["warnings"])
    assert any("recommendation" in w for w in payload["warnings"])
    assert any("cost_of_delay" in w for w in payload["warnings"])


def test_dr_escalate_malformed_governing_ref_is_actionable_not_a_traceback(server: MCPServer) -> None:
    """A malformed governing_ref is rejected with an actionable message, not a crash."""
    message = _call_expecting_error(
        server,
        "dr_escalate",
        question="q",
        context="c",
        options=_valid_escalation_options(),
        recommendation="r",
        cost_of_delay="d",
        governing_ref="not-a-ref",
        project=PROJECT,
    )

    assert "not-a-ref" in message
    assert "IDR-0001" in message  # example of the expected format


def test_dr_escalations_returns_blocking_items_first(
    server: MCPServer, dynamodb_resource: Any, store: Store
) -> None:
    """dr_escalations orders blocking DESC, then raised_at DESC within each group."""
    new_blocking = _escalate(server, question="New blocking?", blocking=True)
    old_nonblocking = _escalate(server, question="Old flag?", blocking=False)
    old_blocking = _escalate(server, question="Old blocking?", blocking=True)
    new_nonblocking = _escalate(server, question="New flag?", blocking=False)

    _stamp_escalation_raised_at(
        dynamodb_resource, store.table_name, new_blocking["ref"], "2026-06-01T00:00:00+00:00"
    )
    _stamp_escalation_raised_at(
        dynamodb_resource, store.table_name, old_nonblocking["ref"], "2026-01-01T00:00:00+00:00"
    )
    _stamp_escalation_raised_at(
        dynamodb_resource, store.table_name, old_blocking["ref"], "2026-02-01T00:00:00+00:00"
    )
    _stamp_escalation_raised_at(
        dynamodb_resource, store.table_name, new_nonblocking["ref"], "2026-05-01T00:00:00+00:00"
    )

    payload = _call(server, "dr_escalations", project=PROJECT)
    refs_in_order = [e["ref"] for e in payload["escalations"]]

    assert refs_in_order == [
        new_blocking["ref"],
        old_blocking["ref"],
        new_nonblocking["ref"],
        old_nonblocking["ref"],
    ]


def test_dr_escalations_blocking_only_filters(server: MCPServer) -> None:
    """blocking_only=True returns only escalations with blocking=True."""
    blocking = _escalate(server, question="Blocked?", blocking=True)
    _escalate(server, question="Flag for later?", blocking=False)

    payload = _call(server, "dr_escalations", blocking_only=True, project=PROJECT)

    assert [e["ref"] for e in payload["escalations"]] == [blocking["ref"]]


def test_dr_answer_without_produces_resolves_and_creates_no_record(server: MCPServer) -> None:
    """dr_answer without produces resolves the escalation and creates no
    record -- and, since no_record wasn't set either, warns that this
    resolution now exists only inside the escalation (Change 1, "no
    record, no decision")."""
    escalation = _escalate(server, question="Rename the bucket?")

    payload = _call(
        server, "dr_answer", ref=escalation["ref"], resolution="Don't do that.", project=PROJECT
    )

    assert payload["status"] == "answered"
    assert payload["resolution"] == "Don't do that."
    assert payload["produced_ref"] is None
    assert payload["produced_record"] is None
    assert any("no_record" in w for w in payload["warnings"])


def test_dr_answer_with_produces_creates_record_and_sets_produced_ref(server: MCPServer) -> None:
    """dr_answer(produces=...) creates the record atomically and links it via produced_ref."""
    escalation = _escalate(server, question="How should retries work?")

    payload = _call(
        server,
        "dr_answer",
        ref=escalation["ref"],
        resolution="Use exponential backoff, capped at 5 retries.",
        produces={
            "rec_type": "ADR",
            "title": "Use exponential backoff for retries",
            "fields": {"decision": "exponential backoff, cap 5"},
        },
        project=PROJECT,
    )

    assert payload["status"] == "answered"
    assert payload["produced_ref"] is not None
    assert payload["produced_record"] is not None
    assert payload["produced_record"]["ref"] == payload["produced_ref"]
    assert payload["produced_record"]["title"] == "Use exponential backoff for retries"
    # produces created an ADR with no implements-bet link -- the usual dr_create
    # governance warning surfaces here too, since dr_answer's produces path
    # reuses the same _create_warnings computation.
    assert any("implements-bet" in w for w in payload["warnings"])

    refetched = _call(server, "dr_get", ref=payload["produced_ref"], project=PROJECT)
    assert refetched["ref"] == payload["produced_ref"]


def test_dr_answer_already_answered_returns_actionable_message_not_traceback(server: MCPServer) -> None:
    """Answering an already-answered escalation returns the actionable message, not a traceback."""
    escalation = _escalate(server, question="Ship now?")
    _call(server, "dr_answer", ref=escalation["ref"], resolution="Yes, ship it.", project=PROJECT)

    message = _call_expecting_error(
        server, "dr_answer", ref=escalation["ref"], resolution="Actually, no.", project=PROJECT
    )

    assert "already" in message.lower()
    assert "Yes, ship it." in message


def test_escalations_do_not_appear_in_dr_list_and_records_do_not_appear_in_dr_escalations(
    server: MCPServer,
) -> None:
    """Escalations and records stay confined to their own tool's results."""
    record = _call(server, "dr_create", rec_type="ADR", title="Some record", project=PROJECT)
    escalation = _escalate(server, question="Some escalation")

    record_refs = [r["ref"] for r in _call(server, "dr_list", project=PROJECT)["records"]]
    escalation_refs = [e["ref"] for e in _call(server, "dr_escalations", project=PROJECT)["escalations"]]

    assert escalation["ref"] not in record_refs
    assert record["ref"] not in escalation_refs
    assert record["ref"] in record_refs
    assert escalation["ref"] in escalation_refs


# ----------------------------------------------------------------------
# New governance checks: placeholder lint, attestation gate, dr_list(stale=True)
# ----------------------------------------------------------------------


def test_dr_set_status_accept_surfaces_placeholder_lint_warning(server: MCPServer) -> None:
    """Placeholder-lint warnings from Store.set_status reach the tool response,
    naming both the placeholder and the field it sits in."""
    idr = _call(
        server,
        "dr_create",
        rec_type="IDR",
        title="Bet on inference budget",
        fields={"investment": "inference budget cap of $[X]/month"},
        project=PROJECT,
    )

    payload = _call(server, "dr_set_status", ref=idr["ref"], status="accepted", project=PROJECT)

    assert payload["status"] == "accepted"  # warns, never blocks
    assert any("[X]" in w and "fields.investment" in w for w in payload["warnings"])


def test_dr_set_status_accept_surfaces_attestation_gate_warning(server: MCPServer) -> None:
    """The requires-attestation commitment-gate warning reaches the tool response,
    naming the not-yet-accepted target."""
    adr = _call(
        server, "dr_create", rec_type="ADR", title="Operational readiness", project=PROJECT
    )
    idr = _call(
        server,
        "dr_create",
        rec_type="IDR",
        title="Go all in on launch",
        links=[{"ref": adr["ref"], "relation": "requires-attestation"}],
        project=PROJECT,
    )

    payload = _call(server, "dr_set_status", ref=idr["ref"], status="accepted", project=PROJECT)

    assert payload["status"] == "accepted"  # always succeeds
    assert any(adr["ref"] in w and "proposed" in w for w in payload["warnings"])


def test_dr_list_stale_returns_documented_shape(server: MCPServer) -> None:
    """dr_list(stale=True) returns rows with a stale_reasons list naming the
    offending ref -- and the same record is absent from unfunded=true, since
    it still has its governing link, it just points at a dead bet."""
    idr = _call(
        server,
        "dr_create",
        rec_type="IDR",
        title="Bet to deprecate",
        status="accepted",
        project=PROJECT,
    )
    _call(server, "dr_set_status", ref=idr["ref"], status="deprecated", project=PROJECT)
    adr = _call(
        server,
        "dr_create",
        rec_type="ADR",
        title="Work under the dead bet",
        links=[{"ref": idr["ref"], "relation": "implements-bet"}],
        project=PROJECT,
    )

    payload = _call(server, "dr_list", stale=True, project=PROJECT)

    assert len(payload["records"]) == 1
    row = payload["records"][0]
    assert row["ref"] == adr["ref"]
    assert isinstance(row["stale_reasons"], list)
    assert any(
        idr["ref"] in reason and "deprecated" in reason for reason in row["stale_reasons"]
    )

    unfunded_payload = _call(server, "dr_list", unfunded=True, project=PROJECT)
    assert adr["ref"] not in [r["ref"] for r in unfunded_payload["records"]]


# ----------------------------------------------------------------------
# "No record, no decision": dr_answer(no_record=...) and
# dr_escalations(unrecorded=True)
# ----------------------------------------------------------------------


def test_dr_answer_without_produces_or_no_record_warns_naming_both_remedies(
    server: MCPServer,
) -> None:
    """dr_answer with neither produces nor no_record warns, naming both
    remedies -- the decision now exists only inside the escalation."""
    escalation = _escalate(server, question="Should we do X?")

    payload = _call(
        server, "dr_answer", ref=escalation["ref"], resolution="Yes, do X.", project=PROJECT
    )

    assert payload["no_record"] is False
    assert any("produces" in w and "no_record" in w for w in payload["warnings"])


def test_dr_answer_with_produces_does_not_warn_no_record(server: MCPServer) -> None:
    """dr_answer(produces=...) suppresses the no-record warning entirely."""
    escalation = _escalate(server, question="How should retries work?")

    payload = _call(
        server,
        "dr_answer",
        ref=escalation["ref"],
        resolution="Use exponential backoff.",
        produces={"rec_type": "ADR", "title": "Use exponential backoff for retries"},
        project=PROJECT,
    )

    assert not any("no_record" in w for w in payload["warnings"])


def test_dr_answer_with_no_record_true_suppresses_warning_and_persists(
    server: MCPServer,
) -> None:
    """dr_answer(no_record=True) suppresses the warning and the flag persists."""
    escalation = _escalate(server, question="Rename the bucket?")

    payload = _call(
        server,
        "dr_answer",
        ref=escalation["ref"],
        resolution="Don't do that.",
        no_record=True,
        project=PROJECT,
    )

    assert payload["no_record"] is True
    assert payload["warnings"] == []


def test_dr_escalations_unrecorded_returns_the_sweep_set(server: MCPServer) -> None:
    """dr_escalations(unrecorded=True) surfaces exactly the answered,
    no-record, not-deliberately-so set -- excluding open, no_record=True,
    and record-producing escalations."""
    forgotten = _escalate(server, question="Forgotten record?")
    _call(
        server,
        "dr_answer",
        ref=forgotten["ref"],
        resolution="Forgot to write it down.",
        project=PROJECT,
    )

    deliberate = _escalate(server, question="Deliberately no record?")
    _call(
        server,
        "dr_answer",
        ref=deliberate["ref"],
        resolution="Don't do that.",
        no_record=True,
        project=PROJECT,
    )

    recorded = _escalate(server, question="Produced a record?")
    _call(
        server,
        "dr_answer",
        ref=recorded["ref"],
        resolution="Use exponential backoff.",
        produces={"rec_type": "ADR", "title": "Use exponential backoff for retries"},
        project=PROJECT,
    )

    still_open = _escalate(server, question="Still open?")

    payload = _call(server, "dr_escalations", unrecorded=True, project=PROJECT)
    refs = {e["ref"] for e in payload["escalations"]}

    assert refs == {forgotten["ref"]}
    assert deliberate["ref"] not in refs
    assert recorded["ref"] not in refs
    assert still_open["ref"] not in refs


# ----------------------------------------------------------------------
# dr_withdraw: the third honest ending for an escalation
# ----------------------------------------------------------------------


def test_dr_withdraw_returns_documented_shape(server: MCPServer) -> None:
    """dr_withdraw sets status="withdrawn", stamps withdrawn_at, and
    records withdrawn_reason -- returned in full, plus a warnings list."""
    escalation = _escalate(server, question="Still needed?")

    payload = _call(
        server,
        "dr_withdraw",
        ref=escalation["ref"],
        reason="Resolved elsewhere before anyone got to this.",
        project=PROJECT,
    )

    assert payload["status"] == "withdrawn"
    assert payload["withdrawn_at"] is not None
    assert payload["withdrawn_reason"] == "Resolved elsewhere before anyone got to this."
    assert payload["resolution"] is None
    assert payload["produced_ref"] is None
    assert isinstance(payload["warnings"], list)


def test_dr_withdraw_blank_reason_is_actionable_not_a_traceback(server: MCPServer) -> None:
    """A blank reason is rejected with an actionable message, not a crash."""
    escalation = _escalate(server, question="Still relevant?")

    message = _call_expecting_error(
        server, "dr_withdraw", ref=escalation["ref"], reason="", project=PROJECT
    )

    assert "reason" in message.lower()


def test_dr_withdraw_an_answered_escalation_returns_actionable_message_not_traceback(
    server: MCPServer,
) -> None:
    """Withdrawing an already-answered escalation returns the actionable
    message naming its status and resolution, not a traceback."""
    escalation = _escalate(server, question="Ship now?")
    _call(server, "dr_answer", ref=escalation["ref"], resolution="Yes, ship it.", project=PROJECT)

    message = _call_expecting_error(
        server, "dr_withdraw", ref=escalation["ref"], reason="Never mind.", project=PROJECT
    )

    assert "answered" in message.lower()
    assert "Yes, ship it." in message


def test_dr_withdraw_duplicate_of_self_reference_is_actionable_not_a_traceback(
    server: MCPServer,
) -> None:
    """duplicate_of naming the escalation's own ref is rejected with an
    actionable message, not a crash."""
    escalation = _escalate(server, question="Same underlying decision as itself?")

    message = _call_expecting_error(
        server,
        "dr_withdraw",
        ref=escalation["ref"],
        reason="Duplicate.",
        duplicate_of=escalation["ref"],
        project=PROJECT,
    )

    assert escalation["ref"] in message


def test_dr_withdraw_duplicate_of_nonexistent_target_is_actionable_not_a_traceback(
    server: MCPServer,
) -> None:
    """duplicate_of pointing at a nonexistent escalation is rejected."""
    escalation = _escalate(server, question="Duplicate of what?")

    message = _call_expecting_error(
        server,
        "dr_withdraw",
        ref=escalation["ref"],
        reason="Duplicate.",
        duplicate_of="ESC-9999",
        project=PROJECT,
    )

    assert "ESC-9999" in message


def test_dr_withdraw_duplicate_of_a_withdrawn_duplicate_warns_naming_ultimate_target(
    server: MCPServer,
) -> None:
    """duplicate_of pointing at an escalation that is itself withdrawn as a
    duplicate warns and names the ultimate target of the chain."""
    original = _escalate(server, question="Where the decision actually lives.")
    middle = _escalate(server, question="An earlier duplicate report.")
    _call(
        server,
        "dr_withdraw",
        ref=middle["ref"],
        reason="Duplicate.",
        duplicate_of=original["ref"],
        project=PROJECT,
    )

    newest = _escalate(server, question="Yet another report of the same thing.")
    payload = _call(
        server,
        "dr_withdraw",
        ref=newest["ref"],
        reason="Duplicate.",
        duplicate_of=middle["ref"],
        project=PROJECT,
    )

    assert payload["duplicate_of"] == middle["ref"]
    assert any(
        original["ref"] in w and middle["ref"] in w for w in payload["warnings"]
    )


def test_dr_withdraw_does_not_appear_in_unrecorded_sweep(server: MCPServer) -> None:
    """A withdrawn escalation must not appear in
    dr_escalations(unrecorded=True) -- the load-bearing behavior this tool
    exists for."""
    withdrawn = _escalate(server, question="Moot by the time anyone looked?")
    _call(
        server,
        "dr_withdraw",
        ref=withdrawn["ref"],
        reason="Overtaken by events.",
        project=PROJECT,
    )

    payload = _call(server, "dr_escalations", unrecorded=True, project=PROJECT)
    refs = {e["ref"] for e in payload["escalations"]}

    assert withdrawn["ref"] not in refs


def test_dr_escalations_status_withdrawn_returns_withdrawn_items(server: MCPServer) -> None:
    """dr_escalations(status="withdrawn") returns withdrawn escalations
    through the existing status filter."""
    withdrawn = _escalate(server, question="Was withdrawn.")
    _call(server, "dr_withdraw", ref=withdrawn["ref"], reason="Moot.", project=PROJECT)
    still_open = _escalate(server, question="Still open.")

    payload = _call(server, "dr_escalations", status="withdrawn", project=PROJECT)
    refs = {e["ref"] for e in payload["escalations"]}

    assert refs == {withdrawn["ref"]}
    assert still_open["ref"] not in refs


# ----------------------------------------------------------------------
# dr_revise: reasoning changed, decision did not
# ----------------------------------------------------------------------


def test_dr_revise_replaces_body_md_and_preserves_previous_text(server: MCPServer) -> None:
    """dr_revise replaces body_md and keeps the exact prior text in the
    revision entry."""
    record = _call(
        server,
        "dr_create",
        rec_type="ADR",
        title="Use DynamoDB",
        body_md="Original reasoning.",
        project=PROJECT,
    )

    payload = _call(
        server,
        "dr_revise",
        ref=record["ref"],
        body_md="Revised reasoning.",
        note="ADR-0024 removed the benefit this cited.",
        project=PROJECT,
    )

    assert payload["body_md"] == "Revised reasoning."
    assert payload["revisions"][0]["previous_body_md"] == "Original reasoning."
    assert payload["revisions"][0]["note"] == "ADR-0024 removed the benefit this cited."
    assert payload["warnings"] == []


def test_dr_revise_blank_note_is_rejected_not_a_traceback(server: MCPServer) -> None:
    """dr_revise with an empty note is a rejected call naming the requirement."""
    record = _call(server, "dr_create", rec_type="ADR", title="Decision", project=PROJECT)

    message = _call_expecting_error(
        server, "dr_revise", ref=record["ref"], body_md="New text.", note="", project=PROJECT
    )

    assert "note" in message.lower()


def test_dr_revise_on_accepted_record_does_not_warn(server: MCPServer) -> None:
    """dr_revise on an accepted record succeeds with no supersede warning --
    this is the right tool for a reasoning-only change on an accepted record."""
    record = _call(
        server, "dr_create", rec_type="ADR", title="Decision", body_md="v1", project=PROJECT
    )
    _call(server, "dr_set_status", ref=record["ref"], status="accepted", project=PROJECT)

    payload = _call(
        server,
        "dr_revise",
        ref=record["ref"],
        body_md="v2",
        note="Reasoning changed.",
        project=PROJECT,
    )

    assert payload["status"] == "accepted"
    assert payload["warnings"] == []


def test_dr_revise_multiple_revisions_accumulate_in_order(server: MCPServer) -> None:
    """Repeated dr_revise calls append to `revisions` in order."""
    record = _call(
        server, "dr_create", rec_type="ADR", title="Decision", body_md="v1", project=PROJECT
    )
    _call(server, "dr_revise", ref=record["ref"], body_md="v2", note="first", project=PROJECT)
    payload = _call(
        server, "dr_revise", ref=record["ref"], body_md="v3", note="second", project=PROJECT
    )

    assert payload["body_md"] == "v3"
    assert [r["previous_body_md"] for r in payload["revisions"]] == ["v1", "v2"]
    assert [r["note"] for r in payload["revisions"]] == ["first", "second"]


def test_dr_export_renders_revisions_section(server: MCPServer, tmp_path: Path) -> None:
    """dr_export output contains a rendered "## Revisions" section, same
    treatment as status_history and reviews."""
    record = _call(
        server,
        "dr_create",
        rec_type="ADR",
        title="Use DynamoDB",
        body_md="Original reasoning.",
        project=PROJECT,
    )
    _call(
        server,
        "dr_revise",
        ref=record["ref"],
        body_md="Revised reasoning.",
        note="ADR-0024 removed the benefit this cited.",
        project=PROJECT,
    )

    out_dir = str(tmp_path / "decisions")
    payload = _call(server, "dr_export", ref=record["ref"], out_dir=out_dir, project=PROJECT)

    content = Path(payload["written"][0]).read_text(encoding="utf-8")
    assert "## Revisions" in content
    assert "ADR-0024 removed the benefit this cited." in content
    assert "Original reasoning." in content


# ----------------------------------------------------------------------
# dr_search: tokenized, OR-of-terms ranking (matched_terms in the response)
# ----------------------------------------------------------------------


def test_dr_search_finds_record_via_multi_term_query_and_reports_matched_terms(
    server: MCPServer,
) -> None:
    """The reported failure, at the tool layer: a multi-term query finds a
    record containing only one of its terms, and the response reports how
    many terms matched."""
    record = _call(
        server,
        "dr_create",
        rec_type="ADR",
        title="Deploy runner authentication",
        fields={
            "decision": (
                "Use a scoped service account for the deploy runner, not a "
                "shared admin credential."
            )
        },
        project=PROJECT,
    )

    payload = _call(
        server,
        "dr_search",
        query="shared credentials runner service account deploy authentication SAML",
        project=PROJECT,
    )

    refs = [r["ref"] for r in payload["results"]]
    assert record["ref"] in refs
    row = next(r for r in payload["results"] if r["ref"] == record["ref"])
    assert row["matched_terms"] >= 1


# ----------------------------------------------------------------------
# dr_events_pending / dr_events_ack (IDR-0003 subscriber tools)
# ----------------------------------------------------------------------


def _publish_event(event_queue: EventQueue, **overrides: Any) -> None:
    """Publish one record.created-shaped message directly to the mocked
    queue, bypassing SNS/events.py -- these tests exercise the tool layer
    over `EventQueue`, not the publisher.
    """
    body: dict[str, Any] = {
        "event_type": "record.created",
        "ref": "IDR-0009",
        "project": PROJECT,
        "rec_type": "IDR",
        "persona": "investor",
        "title": "Sample bet",
        "status": "proposed",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "tags": [],
        "links": [],
        "fields": {},
        "body_md_excluded": True,
    }
    body.update(overrides)
    event_queue._client.send_message(  # noqa: SLF001 - test-only reach into the injected client
        QueueUrl=event_queue.queue_url,
        MessageBody=json.dumps(body),
        MessageAttributes={
            "project": {"DataType": "String", "StringValue": str(body["project"])},
            "rec_type": {"DataType": "String", "StringValue": str(body["rec_type"])},
            "persona": {"DataType": "String", "StringValue": str(body["persona"])},
            "status": {"DataType": "String", "StringValue": str(body["status"])},
            "event_type": {"DataType": "String", "StringValue": str(body["event_type"])},
        },
    )


def test_dr_events_pending_returns_events_with_receipt_handles(
    events_server: MCPServer, event_queue: EventQueue
) -> None:
    """dr_events_pending returns the documented shape, receipt_handle included."""
    _publish_event(event_queue)

    payload = _call(events_server, "dr_events_pending")

    assert len(payload["events"]) == 1
    event = payload["events"][0]
    assert event["ref"] == "IDR-0009"
    assert event["event_type"] == "record.created"
    assert event["receipt_handle"]


def test_dr_events_pending_on_empty_queue_returns_empty_list(events_server: MCPServer) -> None:
    """No events waiting -> {"events": []}, not an error."""
    payload = _call(events_server, "dr_events_pending")
    assert payload["events"] == []


def test_dr_events_ack_deletes_and_returns_count(
    events_server: MCPServer, event_queue: EventQueue
) -> None:
    """dr_events_ack deletes the given handle and reports the count deleted."""
    _publish_event(event_queue)
    pending = _call(events_server, "dr_events_pending")
    handle = pending["events"][0]["receipt_handle"]

    payload = _call(events_server, "dr_events_ack", receipt_handles=[handle])

    assert payload["deleted"] == 1
    assert _call(events_server, "dr_events_pending")["events"] == []


def test_dr_events_ack_partial_failure_returns_true_count(
    events_server: MCPServer, event_queue: EventQueue
) -> None:
    """A bogus handle mixed with a real one still deletes the real one and
    reports only the true deleted count, not len(receipt_handles)."""
    _publish_event(event_queue)
    pending = _call(events_server, "dr_events_pending")
    real_handle = pending["events"][0]["receipt_handle"]

    payload = _call(
        events_server, "dr_events_ack", receipt_handles=[real_handle, "not-a-real-handle"]
    )

    assert payload["deleted"] == 1


def test_dr_events_pending_missing_queue_url_is_actionable_not_a_traceback(
    server: MCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No DR_EVENTS_QUEUE_URL and no injected EventQueue -> ToolError naming
    the env var, not a raw exception. Uses the plain `server` fixture
    (built with no event_queue) precisely because this is the
    no-queue-configured path."""
    monkeypatch.delenv("DR_EVENTS_QUEUE_URL", raising=False)

    message = _call_expecting_error(server, "dr_events_pending")

    assert "DR_EVENTS_QUEUE_URL" in message


def test_dr_events_ack_missing_queue_url_is_actionable_not_a_traceback(
    server: MCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as above, for dr_events_ack."""
    monkeypatch.delenv("DR_EVENTS_QUEUE_URL", raising=False)

    message = _call_expecting_error(server, "dr_events_ack", receipt_handles=["whatever"])

    assert "DR_EVENTS_QUEUE_URL" in message
