"""Tests for `triad_dr.store.Store` against spec section 7's acceptance criteria.

Covers (numbers reference the task's distilled test list, itself drawn from
`decision-registers-mcp-spec.md` section 7):

  1. seq allocation under concurrent creates (no dup refs)
  2. status transition validation, including a rejected illegal transition
  3. supersede transaction sets both directions and flips the old status
  4. project resolution precedence: arg > env > NoProjectError, with
     remediation text and known-projects list in the error message
  5. dr_list ordering: created_at descending, not SK order
  6. dr_update patch semantics: wholesale replace, protected attrs rejected
  7. link validation: malformed ref rejected, well-formed forward ref accepted
  8. dr_review: appends without changing status; missed+IDR cause handling
  9. dr_due: past review_date surfaces until a later review is recorded
  10. governance warnings: unfunded ADR, MDR evidence/plan
  11. kill cascade: deprecating a governing IDR orphans without cascading
  12. escalation queue: seq allocation, blocking-first-then-oldest ordering,
      blocking_only filter, answer with/without produces, already-answered
      rejection, governance warnings that never block, governing_ref
      validation, project scoping, and partition isolation from records
  13. new governance checks (real user-feedback gaps): placeholder lint on
      accept, the requires-attestation commitment gate on accept, and
      dr_list(stale=True)
  15. "no record, no decision": dr_answer(no_record=...) warning behavior
      and escalations(unrecorded=True)
  16. dr_revise: reasoning changed, decision did not
  17. dr_search tokenization (regression test for the reported live bug)
  18. dr_withdraw: the third honest ending for an escalation -- sets
      status/reason/timestamp; blank reason rejected; withdrawing an
      already-resolved escalation rejected naming its status; a withdrawn
      escalation never appears in escalations(unrecorded=True) (the
      load-bearing case this feature exists for); duplicate_of validation
      (self-reference, nonexistent target, malformed ref) and the
      duplicate-of-a-withdrawn-duplicate chain warning; escalations(status=
      "withdrawn") returns them
"""

from __future__ import annotations

import concurrent.futures
from typing import Any

import pytest

from triad_dr.errors import (
    EscalationAlreadyResolvedError,
    EscalationNotFoundError,
    InvalidRefError,
    InvalidTransitionError,
    MissingCauseError,
    MissingRevisionNoteError,
    MissingWithdrawalReasonError,
    NoProjectError,
    ProtectedAttributeError,
)
from triad_dr.store import Store

PROJECT = "acme-app"


# ----------------------------------------------------------------------
# 1. Seq allocation under concurrent creates
# ----------------------------------------------------------------------


def test_seq_allocation_concurrent_creates_no_duplicate_refs(store: Store) -> None:
    """Many concurrent creates for the same type produce no duplicate refs."""
    n = 30

    def _create(i: int) -> str:
        record, _warnings = store.create(
            "ADR", title=f"Concurrent decision {i}", project=PROJECT
        )
        return record.ref

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        refs = list(executor.map(_create, range(n)))

    assert len(refs) == n
    assert len(set(refs)) == n, f"duplicate refs allocated: {refs}"


# ----------------------------------------------------------------------
# 2. Status transition validation
# ----------------------------------------------------------------------


def test_status_transition_validation(store: Store) -> None:
    """Legal transitions succeed and update status_history."""
    record, _ = store.create("ADR", title="Use single-table design", project=PROJECT)
    assert record.status == "proposed"

    updated, orphans, _warnings = store.set_status(
        record.ref, "accepted", note="looks good", project=PROJECT
    )
    assert updated.status == "accepted"
    assert orphans == []
    assert updated.status_history[-1].status == "accepted"
    assert updated.status_history[-1].note == "looks good"


def test_status_transition_rejects_illegal_transition(store: Store) -> None:
    """An illegal transition (accepted -> proposed) is rejected."""
    record, _ = store.create("ADR", title="Use single-table design", project=PROJECT)
    store.set_status(record.ref, "accepted", project=PROJECT)

    with pytest.raises(InvalidTransitionError):
        store.set_status(record.ref, "proposed", project=PROJECT)

    # draft -> accepted, skipping proposed, is also illegal.
    draft_record, _ = store.create(
        "ADR", title="Another decision", status="draft", project=PROJECT
    )
    with pytest.raises(InvalidTransitionError):
        store.set_status(draft_record.ref, "accepted", project=PROJECT)


# ----------------------------------------------------------------------
# 3. Supersede transaction
# ----------------------------------------------------------------------


def test_supersede_transaction_sets_both_directions_and_flips_old_status(
    store: Store,
) -> None:
    """dr_supersede atomically creates the new record and flips the old one."""
    old, _ = store.create("ADR", title="Use DynamoDB", project=PROJECT)
    store.set_status(old.ref, "accepted", project=PROJECT)

    new, _warnings = store.supersede(
        old.ref,
        {"title": "Use DynamoDB with single-table design", "body_md": "revised"},
        project=PROJECT,
    )

    assert new.supersedes == old.ref
    assert new.status == "proposed"

    # DEFERRED: the link lands immediately so the chain is followable, but the
    # predecessor keeps its status. Flipping now would leave no accepted record
    # at all -- old retired, replacement merely proposed -- which is wrong in a
    # register where a human ratifies everything.
    mid = store.get(old.ref, project=PROJECT)
    assert mid.superseded_by == new.ref, "link is immediate"
    assert mid.status == "accepted", "status is deferred until the replacement is accepted"

    store.set_status(new.ref, "accepted", project=PROJECT)

    refetched_old = store.get(old.ref, project=PROJECT)
    assert refetched_old.status == "superseded"
    assert refetched_old.superseded_by == new.ref
    assert refetched_old.status_history[-1].status == "superseded"


# ----------------------------------------------------------------------
# 4. Project resolution precedence
# ----------------------------------------------------------------------


def test_project_resolution_precedence(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit project argument wins over DR_PROJECT env var."""
    monkeypatch.setenv("DR_PROJECT", "env-project")
    assert store.resolve_project("arg-project") == "arg-project"
    assert store.resolve_project(None) == "env-project"


def test_project_resolution_no_project_raises_with_remediation_and_known_projects(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No arg, no env -> NoProjectError with remediation text and known projects."""
    monkeypatch.delenv("DR_PROJECT", raising=False)
    store.create("ADR", title="Seed a known project", project="known-project")

    with pytest.raises(NoProjectError) as exc_info:
        store.resolve_project(None)

    message = str(exc_info.value)
    assert "DR_PROJECT" in message
    assert 'project="' in message
    assert "known-project" in message


# ----------------------------------------------------------------------
# 5. dr_list ordering
# ----------------------------------------------------------------------


def _stamp_created_at(dynamodb_resource: Any, table_name: str, ref: str, created_at: str) -> None:
    """Directly overwrite a record's created_at in the mocked table.

    Bypasses the store on purpose: store.create() always stamps "now", so
    the ordering test needs a way to force distinct, out-of-SK-order
    created_at values without adding a test-only backdoor to the public
    Store API.
    """
    rec_type, seq_str = ref.split("-")
    table = dynamodb_resource.Table(table_name)
    table.update_item(
        Key={"pk": f"PROJ#{PROJECT}", "sk": f"REC#{rec_type}#{seq_str}"},
        UpdateExpression="SET #created_at = :created_at",
        ExpressionAttributeNames={"#created_at": "created_at"},
        ExpressionAttributeValues={":created_at": created_at},
    )


def test_list_ordering_is_created_at_descending_not_sk_order(
    store: Store, dynamodb_resource: Any
) -> None:
    """SK sorts ADR < IDR < MDR lexically; list() must sort by created_at instead.

    Stamped so that neither ascending nor descending SK order matches the
    correct created_at-descending order -- only real client-side sorting
    by created_at can pass this.
    """
    adr, _ = store.create("ADR", title="Builder decision", project=PROJECT)
    idr, _ = store.create("IDR", title="Investor bet", project=PROJECT)
    mdr, _ = store.create("MDR", title="Seller motion", project=PROJECT)

    # Correct created_at-desc order: IDR (newest), ADR (middle), MDR (oldest).
    # Ascending SK order would be ADR, IDR, MDR. Descending SK order would be
    # MDR, IDR, ADR. Neither matches -- only created_at sorting is correct.
    _stamp_created_at(dynamodb_resource, store.table_name, adr.ref, "2022-06-01T00:00:00+00:00")
    _stamp_created_at(dynamodb_resource, store.table_name, idr.ref, "2026-01-01T00:00:00+00:00")
    _stamp_created_at(dynamodb_resource, store.table_name, mdr.ref, "2020-01-01T00:00:00+00:00")

    results = store.list(project=PROJECT)
    refs_in_order = [r.ref for r in results]

    assert refs_in_order == [idr.ref, adr.ref, mdr.ref]


# ----------------------------------------------------------------------
# 6. dr_update patch semantics
# ----------------------------------------------------------------------


def test_update_replaces_tags_wholesale_not_merged(store: Store) -> None:
    """Patching tags replaces the list wholesale; it does not merge/append."""
    record, _ = store.create("ADR", title="Decision", tags=["a", "b"], project=PROJECT)

    updated, _warnings = store.update(record.ref, {"tags": ["c"]}, project=PROJECT)

    assert updated.tags == ["c"]


def test_update_protected_attribute_raises_and_names_right_tool(store: Store) -> None:
    """Patching a protected attribute errors and names the correct tool."""
    record, _ = store.create("ADR", title="Decision", project=PROJECT)

    with pytest.raises(ProtectedAttributeError) as status_exc:
        store.update(record.ref, {"status": "accepted"}, project=PROJECT)
    assert "dr_set_status" in str(status_exc.value)

    with pytest.raises(ProtectedAttributeError) as reviews_exc:
        store.update(record.ref, {"reviews": []}, project=PROJECT)
    assert "dr_review" in str(reviews_exc.value)

    with pytest.raises(ProtectedAttributeError):
        store.update(record.ref, {"seq": 99}, project=PROJECT)


def test_update_warns_on_accepted_record_edit(store: Store) -> None:
    """Editing title/body_md/fields on an accepted record warns, not blocks."""
    record, _ = store.create("ADR", title="Decision", project=PROJECT)
    store.set_status(record.ref, "accepted", project=PROJECT)

    updated, warnings = store.update(record.ref, {"title": "Revised decision"}, project=PROJECT)

    assert updated.title == "Revised decision"
    assert any("dr_supersede" in w for w in warnings)


# ----------------------------------------------------------------------
# 7. Link validation
# ----------------------------------------------------------------------


def test_link_validation_malformed_ref_rejected(store: Store) -> None:
    """A malformed link ref is rejected at create time."""
    with pytest.raises(InvalidRefError):
        store.create(
            "ADR",
            title="Decision",
            links=[{"ref": "not-a-ref", "relation": "implements-bet"}],
            project=PROJECT,
        )


def test_link_validation_well_formed_forward_reference_accepted(store: Store) -> None:
    """A well-formed ref to a not-yet-existing record is accepted (forward ref)."""
    record, warnings = store.create(
        "ADR",
        title="Decision",
        links=[{"ref": "IDR-9999", "relation": "implements-bet"}],
        project=PROJECT,
    )

    assert record.links[0].ref == "IDR-9999"
    # implements-bet link is present, so no unfunded warning.
    assert not any("implements-bet" in w for w in warnings)


# ----------------------------------------------------------------------
# 8. dr_review
# ----------------------------------------------------------------------


def test_review_appends_without_changing_status(store: Store) -> None:
    """dr_review appends to reviews and never touches status."""
    record, _ = store.create("IDR", title="Bet on X", project=PROJECT)
    store.set_status(record.ref, "accepted", project=PROJECT)

    updated, _warnings = store.review(
        record.ref, verdict="met", evidence="Success criteria hit per dashboard.", project=PROJECT
    )

    assert updated.status == "accepted"
    assert len(updated.reviews) == 1
    assert updated.reviews[0].verdict == "met"


def test_review_missed_idr_without_cause_raises_naming_all_three_options(
    store: Store,
) -> None:
    """A missed verdict on an IDR without cause errors, naming all 3 options."""
    record, _ = store.create("IDR", title="Bet on X", project=PROJECT)

    with pytest.raises(MissingCauseError) as exc_info:
        store.review(record.ref, verdict="missed", evidence="Nobody used it.", project=PROJECT)

    message = str(exc_info.value)
    assert "market" in message
    assert "delivery" in message
    assert "both" in message


def test_review_missed_idr_with_cause_succeeds(store: Store) -> None:
    """A missed verdict on an IDR with cause="delivery" succeeds."""
    record, _ = store.create("IDR", title="Bet on X", project=PROJECT)

    updated, _warnings = store.review(
        record.ref,
        verdict="missed",
        evidence="Feature was never actually built as specified.",
        cause="delivery",
        project=PROJECT,
    )

    assert updated.reviews[0].cause == "delivery"
    assert updated.status not in ("rejected", "deprecated")  # review never changes status


def test_review_missed_on_mdr_without_cause_succeeds(store: Store) -> None:
    """A missed verdict on an MDR (not an IDR) never requires cause."""
    record, _ = store.create("MDR", title="Launch experiment", project=PROJECT)

    updated, _warnings = store.review(
        record.ref, verdict="missed", evidence="Experiment underperformed.", project=PROJECT
    )

    assert updated.reviews[0].cause is None


# ----------------------------------------------------------------------
# 9. dr_due
# ----------------------------------------------------------------------


def test_due_surfaces_past_review_date_until_reviewed(store: Store) -> None:
    """An IDR with a past review_date and no later review appears in due()."""
    record, _ = store.create(
        "IDR",
        title="Bet on Y",
        fields={"review_date": "2020-01-01"},
        project=PROJECT,
    )

    due_refs = [r.ref for r in store.due(project=PROJECT)]
    assert record.ref in due_refs

    store.review(
        record.ref, verdict="met", evidence="Reviewed after the fact.", project=PROJECT
    )

    due_refs_after = [r.ref for r in store.due(project=PROJECT)]
    assert record.ref not in due_refs_after


# ----------------------------------------------------------------------
# 10. Governance warnings
# ----------------------------------------------------------------------


def test_create_adr_with_no_implements_bet_link_warns_and_succeeds(store: Store) -> None:
    """An ADR with no implements-bet link warns but the create still succeeds."""
    record, warnings = store.create("ADR", title="Unfunded work", project=PROJECT)

    assert any("implements-bet" in w for w in warnings)

    unfunded = store.list(unfunded=True, project=PROJECT)
    assert record.ref in [r.ref for r in unfunded]


def test_create_mdr_with_neither_user_evidence_nor_evidence_plan_warns(store: Store) -> None:
    """An MDR with neither user_evidence nor evidence_plan warns but succeeds."""
    record, warnings = store.create("MDR", title="Positioning experiment", project=PROJECT)

    assert any("user_evidence" in w and "evidence_plan" in w for w in warnings)
    assert record.status == "proposed"  # never blocked


def test_create_mdr_with_evidence_plan_alone_does_not_warn(store: Store) -> None:
    """An MDR with only evidence_plan set (no user_evidence yet) does not warn."""
    _record, warnings = store.create(
        "MDR",
        title="Positioning experiment",
        fields={"evidence_plan": "8-person prototype round, week of Sep 15."},
        project=PROJECT,
    )

    assert not any("user_evidence" in w for w in warnings)


# ----------------------------------------------------------------------
# 11. Kill cascade
# ----------------------------------------------------------------------


def test_kill_cascade_orphans_linked_records_without_changing_their_status(
    store: Store,
) -> None:
    """Deprecating a governing IDR returns linked ADR/MDR refs as orphans,
    without changing their status."""
    idr, _ = store.create("IDR", title="Standing ops bet", status="accepted", project=PROJECT)
    adr, _ = store.create(
        "ADR",
        title="Builder work under the bet",
        links=[{"ref": idr.ref, "relation": "implements-bet"}],
        project=PROJECT,
    )
    mdr, _ = store.create(
        "MDR",
        title="Seller motion under the bet",
        links=[{"ref": idr.ref, "relation": "implements-bet"}],
        project=PROJECT,
    )

    _updated_idr, orphan_refs, warnings = store.set_status(
        idr.ref, "deprecated", project=PROJECT
    )

    assert set(orphan_refs) == {adr.ref, mdr.ref}
    assert warnings  # response says plainly that they are now orphaned

    refetched_adr = store.get(adr.ref, project=PROJECT)
    refetched_mdr = store.get(mdr.ref, project=PROJECT)
    assert refetched_adr.status == "proposed"  # unchanged, not cascaded
    assert refetched_mdr.status == "proposed"  # unchanged, not cascaded


# ----------------------------------------------------------------------
# 12. Escalation queue
# ----------------------------------------------------------------------

OTHER_PROJECT = "example-project"


def _valid_options() -> list[dict[str, Any]]:
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
    store: Store,
    question: str = "What now?",
    blocking: bool = False,
    project: str = PROJECT,
) -> Any:
    """Shorthand for filing a well-formed escalation in tests that don't
    care about the specific content."""
    escalation, _warnings = store.escalate(
        question=question,
        context="Context for the test.",
        options=_valid_options(),
        recommendation="Proceed with plan A.",
        cost_of_delay="Nothing until Friday.",
        blocking=blocking,
        project=project,
    )
    return escalation


def _stamp_escalation_raised_at(
    dynamodb_resource: Any, table_name: str, ref: str, raised_at: str, project: str = PROJECT
) -> None:
    """Directly overwrite an escalation's raised_at in the mocked table.

    Bypasses the store on purpose, same rationale as `_stamp_created_at`
    above: store.escalate() always stamps "now", so the ordering test
    needs a way to force distinct, controllable raised_at values.
    """
    seq_str = ref.split("-")[1]
    table = dynamodb_resource.Table(table_name)
    table.update_item(
        Key={"pk": f"PROJ#{project}", "sk": f"ESC#{seq_str}"},
        UpdateExpression="SET #raised_at = :raised_at",
        ExpressionAttributeNames={"#raised_at": "raised_at"},
        ExpressionAttributeValues={":raised_at": raised_at},
    )


def test_escalation_seq_allocation_concurrent_and_independent_of_record_seqs(
    store: Store,
) -> None:
    """Concurrent escalate() calls produce no duplicate refs, and ESC seqs
    are independent of ADR/IDR/MDR seqs allocated in the same project."""
    store.create("ADR", title="Unrelated ADR one", project=PROJECT)
    store.create("ADR", title="Unrelated ADR two", project=PROJECT)
    store.create("IDR", title="Unrelated IDR", project=PROJECT)

    n = 30

    def _file(i: int) -> str:
        return _escalate(store, question=f"Deploy variant {i}?").ref

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        refs = list(executor.map(_file, range(n)))

    assert len(refs) == n
    assert len(set(refs)) == n, f"duplicate escalation refs allocated: {refs}"
    # ESC has its own counter -- unaffected by the 3 ADR/IDR records already
    # created in this project, so the first escalation is still ESC-0001.
    assert "ESC-0001" in refs


def test_escalations_ordering_is_blocking_first_then_newest(
    store: Store, dynamodb_resource: Any
) -> None:
    """Ordering is blocking DESC, then raised_at DESCENDING (newest first).

    Same recency direction as list(), so the two listings never disagree
    about what "first" means; blocking simply floats to the top because
    that is what the flag is for. Escalations are filed in an order, and
    stamped with raised_at values, such that creation order, SK order, and
    a blocking-blind newest-first sort would each produce a different
    (wrong) sequence -- only blocking-first, newest-within-group sorting
    produces the asserted sequence.
    """
    new_blocking = _escalate(store, question="New blocking?", blocking=True)
    old_nonblocking = _escalate(store, question="Old flag?", blocking=False)
    old_blocking = _escalate(store, question="Old blocking?", blocking=True)
    new_nonblocking = _escalate(store, question="New flag?", blocking=False)

    _stamp_escalation_raised_at(
        dynamodb_resource, store.table_name, new_blocking.ref, "2026-06-01T00:00:00+00:00"
    )
    _stamp_escalation_raised_at(
        dynamodb_resource, store.table_name, old_nonblocking.ref, "2026-01-01T00:00:00+00:00"
    )
    _stamp_escalation_raised_at(
        dynamodb_resource, store.table_name, old_blocking.ref, "2026-02-01T00:00:00+00:00"
    )
    _stamp_escalation_raised_at(
        dynamodb_resource, store.table_name, new_nonblocking.ref, "2026-05-01T00:00:00+00:00"
    )

    results = store.escalations(project=PROJECT)
    refs_in_order = [e.ref for e in results]

    assert refs_in_order == [
        new_blocking.ref,
        old_blocking.ref,
        new_nonblocking.ref,
        old_nonblocking.ref,
    ]


def test_escalations_blocking_only_filters(store: Store) -> None:
    """blocking_only=True returns only escalations with blocking=True."""
    blocking = _escalate(store, question="Blocked?", blocking=True)
    _escalate(store, question="Flag for later?", blocking=False)

    results = store.escalations(blocking_only=True, project=PROJECT)

    assert [e.ref for e in results] == [blocking.ref]


def test_answer_sets_status_and_resolution_without_creating_record(store: Store) -> None:
    """answer() sets status/answered_at/resolution and, when produces is
    omitted, does not create a record."""
    escalation = _escalate(store, question="Rename the bucket?")

    updated, record, _warnings = store.answer(
        escalation.ref, resolution="Don't do that.", project=PROJECT
    )

    assert updated.status == "answered"
    assert updated.answered_at is not None
    assert updated.resolution == "Don't do that."
    assert updated.produced_ref is None
    assert record is None


def test_answer_with_produces_creates_record_and_sets_produced_ref_atomically(
    store: Store,
) -> None:
    """answer(produces=...) creates the record AND sets produced_ref, in
    the same atomic write."""
    escalation = _escalate(store, question="How should retries work?")

    updated, record, _warnings = store.answer(
        escalation.ref,
        resolution="Use exponential backoff, capped at 5 retries.",
        produces={
            "rec_type": "ADR",
            "title": "Use exponential backoff for retries",
            "fields": {"decision": "exponential backoff, cap 5"},
        },
        project=PROJECT,
    )

    assert record is not None
    assert record.ref.startswith("ADR-")
    assert record.title == "Use exponential backoff for retries"
    assert updated.produced_ref == record.ref
    assert updated.status == "answered"

    # Both sides actually landed: the record is independently fetchable...
    refetched_record = store.get(record.ref, project=PROJECT)
    assert refetched_record.ref == record.ref
    # ...and the escalation is durably answered, not just in the return value.
    refetched_escalations = store.escalations(status="answered", project=PROJECT)
    assert escalation.ref in [e.ref for e in refetched_escalations]


def test_answer_already_answered_raises_naming_prior_resolution(store: Store) -> None:
    """Answering an already-answered escalation raises
    EscalationAlreadyResolvedError naming the existing resolution and when
    it was made."""
    escalation = _escalate(store, question="Ship now?")
    first, _record, _warnings = store.answer(
        escalation.ref, resolution="Yes, ship it.", project=PROJECT
    )

    with pytest.raises(EscalationAlreadyResolvedError) as exc_info:
        store.answer(escalation.ref, resolution="Actually, no.", project=PROJECT)

    message = str(exc_info.value)
    assert "Yes, ship it." in message
    assert first.answered_at in message


def test_escalate_governance_warnings_never_block(store: Store) -> None:
    """Fewer than 2 options, missing recommendation, missing cost_of_delay,
    and missing governing_ref each warn -- but the escalation still
    succeeds as "open". A blocked worker must always be able to file."""
    escalation, warnings = store.escalate(
        question="What now?",
        context="Stuck.",
        options=[],
        recommendation="",
        cost_of_delay="",
        project=PROJECT,
    )

    assert escalation.status == "open"
    assert any("option" in w for w in warnings)
    assert any("recommendation" in w for w in warnings)
    assert any("cost_of_delay" in w for w in warnings)
    assert any("governing_ref" in w for w in warnings)

    # A single option also counts as "fewer than 2".
    _escalation2, warnings2 = store.escalate(
        question="What now?",
        context="Stuck.",
        options=[_valid_options()[0]],
        recommendation="Do the thing.",
        cost_of_delay="Nothing urgent.",
        governing_ref="IDR-0001",
        project=PROJECT,
    )
    assert any("option" in w for w in warnings2)
    assert not any("recommendation" in w for w in warnings2)
    assert not any("governing_ref" in w for w in warnings2)


def test_escalate_governing_ref_validation(store: Store) -> None:
    """A malformed governing_ref raises InvalidRefError; a well-formed ref
    to a not-yet-existing record is accepted (forward reference, same as
    links)."""
    with pytest.raises(InvalidRefError):
        store.escalate(
            question="q",
            context="c",
            options=_valid_options(),
            recommendation="r",
            cost_of_delay="d",
            governing_ref="not-a-ref",
            project=PROJECT,
        )

    escalation, warnings = store.escalate(
        question="q",
        context="c",
        options=_valid_options(),
        recommendation="r",
        cost_of_delay="d",
        governing_ref="IDR-9999",
        project=PROJECT,
    )

    assert escalation.governing_ref == "IDR-9999"
    assert not any("governing_ref" in w for w in warnings)


def test_escalations_are_project_scoped_disjoint_queues(store: Store) -> None:
    """Two projects' escalation queues are disjoint.

    Both projects allocate from their own ESC counter, so `esc_a` and
    `esc_b` legitimately share the ref "ESC-0001" -- refs are unique per
    project, not globally. Isolation is checked by content and by the
    `project` attribute on what each partition's query returns, not by ref
    equality.
    """
    esc_a = _escalate(store, question="q-a only in acme-app", project=PROJECT)
    esc_b = _escalate(store, question="q-b only in example-project", project=OTHER_PROJECT)

    results_project = store.escalations(project=PROJECT)
    results_other = store.escalations(project=OTHER_PROJECT)

    assert all(e.project == PROJECT for e in results_project)
    assert all(e.project == OTHER_PROJECT for e in results_other)

    questions_project = [e.question for e in results_project]
    questions_other = [e.question for e in results_other]
    assert esc_a.question in questions_project
    assert esc_a.question not in questions_other
    assert esc_b.question in questions_other
    assert esc_b.question not in questions_project


def test_escalations_and_records_do_not_leak_across_queries(store: Store) -> None:
    """Escalations do not appear in list() results, and records do not
    appear in escalations() results -- the two partitioned item types must
    not leak into each other's queries."""
    record, _ = store.create("ADR", title="Some record", project=PROJECT)
    escalation = _escalate(store, question="Some escalation")

    record_refs = [r.ref for r in store.list(project=PROJECT)]
    escalation_refs = [e.ref for e in store.escalations(project=PROJECT)]

    assert escalation.ref not in record_refs
    assert record.ref not in escalation_refs
    assert record.ref in record_refs
    assert escalation.ref in escalation_refs


# ----------------------------------------------------------------------
# 13a. Placeholder lint on accept
# ----------------------------------------------------------------------


def test_accept_warns_on_bracket_placeholder_in_field_naming_the_field(store: Store) -> None:
    """An IDR with "$[X]/month" in investment warns on accept, naming the field."""
    record, _ = store.create(
        "IDR",
        title="Bet on inference budget",
        fields={"investment": "inference budget cap of $[X]/month"},
        project=PROJECT,
    )

    updated, _orphans, warnings = store.set_status(record.ref, "accepted", project=PROJECT)

    assert updated.status == "accepted"  # warns, never blocks
    assert any("[X]" in w and "fields.investment" in w for w in warnings)


@pytest.mark.parametrize("placeholder_text", ["TBD", "TODO", "???"])
def test_accept_warns_on_word_and_symbol_placeholders(
    store: Store, placeholder_text: str
) -> None:
    """TBD/TODO/??? in a field each warn on accept, and the transition succeeds."""
    record, _ = store.create(
        "IDR",
        title="Bet with an unfinished field",
        fields={"kill_criteria": f"{placeholder_text} -- decide later"},
        project=PROJECT,
    )

    updated, _orphans, warnings = store.set_status(record.ref, "accepted", project=PROJECT)

    assert updated.status == "accepted"
    assert any("fields.kill_criteria" in w for w in warnings)


def test_accept_does_not_warn_on_markdown_checkbox(store: Store) -> None:
    """A markdown checkbox "- [x]" in body_md must NOT be flagged as a placeholder.

    Regression guard: "[x]"/"[X]" has the same shape as the single-letter
    bracket placeholder pattern, so this is the case that would
    false-positive without the checkbox-context exclusion.
    """
    record, _ = store.create(
        "ADR",
        title="Checklist decision",
        body_md="Rollout checklist:\n- [x] Feature flag added\n- [ ] Docs updated\n",
        project=PROJECT,
    )

    updated, _orphans, warnings = store.set_status(record.ref, "accepted", project=PROJECT)

    assert updated.status == "accepted"
    assert warnings == []


def test_accept_placeholder_lint_never_blocks_with_multiple_placeholders(store: Store) -> None:
    """Several placeholders across fields and body_md all warn; the transition
    still succeeds every time."""
    record, _ = store.create(
        "IDR",
        title="Bet riddled with placeholders",
        fields={"bet": "TBD", "thesis": "???", "investment": "$[X]/month"},
        body_md="See <amount> for the real number. FIXME before shipping.",
        project=PROJECT,
    )

    updated, _orphans, warnings = store.set_status(record.ref, "accepted", project=PROJECT)

    assert updated.status == "accepted"
    assert any("fields.bet" in w for w in warnings)
    assert any("fields.thesis" in w for w in warnings)
    assert any("fields.investment" in w for w in warnings)
    assert any("body_md" in w for w in warnings)


# ----------------------------------------------------------------------
# 13b. Attestation gate on accept
# ----------------------------------------------------------------------


def test_accept_idr_with_proposed_attestation_target_warns_naming_it(store: Store) -> None:
    """An IDR with requires-attestation links to a proposed ADR warns on accept."""
    adr, _ = store.create("ADR", title="Operational readiness", project=PROJECT)
    idr, _ = store.create(
        "IDR",
        title="Go all in on launch",
        links=[{"ref": adr.ref, "relation": "requires-attestation"}],
        project=PROJECT,
    )

    updated, _orphans, warnings = store.set_status(idr.ref, "accepted", project=PROJECT)

    assert updated.status == "accepted"  # always succeeds
    assert any(adr.ref in w and "proposed" in w for w in warnings)


def test_accept_idr_with_all_attestations_accepted_does_not_warn(store: Store) -> None:
    """When every requires-attestation target is accepted, no attestation warning fires."""
    adr, _ = store.create("ADR", title="Operational readiness", project=PROJECT)
    store.set_status(adr.ref, "accepted", project=PROJECT)
    mdr, _ = store.create("MDR", title="Acquisition readiness", project=PROJECT)
    store.set_status(mdr.ref, "accepted", project=PROJECT)

    idr, _ = store.create(
        "IDR",
        title="Go all in on launch",
        links=[
            {"ref": adr.ref, "relation": "requires-attestation"},
            {"ref": mdr.ref, "relation": "requires-attestation"},
        ],
        project=PROJECT,
    )

    _updated, _orphans, warnings = store.set_status(idr.ref, "accepted", project=PROJECT)

    assert not any("requires-attestation" in w for w in warnings)


def test_accept_idr_with_nonexistent_attestation_target_warns_distinctly(store: Store) -> None:
    """A requires-attestation link to a nonexistent ref warns, saying "does not exist"
    -- distinctly from a link to a record that is merely still proposed."""
    idr, _ = store.create(
        "IDR",
        title="Go all in early",
        links=[{"ref": "ADR-9999", "relation": "requires-attestation"}],
        project=PROJECT,
    )

    updated, _orphans, warnings = store.set_status(idr.ref, "accepted", project=PROJECT)

    assert updated.status == "accepted"  # always succeeds
    assert any("ADR-9999" in w and "does not exist" in w for w in warnings)


def test_accept_non_idr_with_requires_attestation_links_does_not_trigger_gate(
    store: Store,
) -> None:
    """The attestation gate is IDR-only -- an ADR with such links is unaffected."""
    adr, _ = store.create(
        "ADR",
        title="Some decision",
        links=[{"ref": "IDR-9999", "relation": "requires-attestation"}],
        project=PROJECT,
    )

    _updated, _orphans, warnings = store.set_status(adr.ref, "accepted", project=PROJECT)

    assert not any("requires-attestation" in w for w in warnings)


# ----------------------------------------------------------------------
# 13c. dr_list(stale=True)
# ----------------------------------------------------------------------


def test_stale_link_to_superseded_target_appears_in_stale_not_unfunded(store: Store) -> None:
    """A record linking to a superseded IDR shows up under stale=True and NOT
    under unfunded=True -- the gap that motivated this feature."""
    idr, _ = store.create("IDR", title="Original bet", status="accepted", project=PROJECT)
    store.supersede(idr.ref, {"title": "Replacement bet", "status": "accepted"}, project=PROJECT)
    adr, _ = store.create(
        "ADR",
        title="Work under the original bet",
        links=[{"ref": idr.ref, "relation": "implements-bet"}],
        project=PROJECT,
    )

    stale_refs = [r.ref for r in store.list(stale=True, project=PROJECT)]
    unfunded_refs = [r.ref for r in store.list(unfunded=True, project=PROJECT)]

    assert adr.ref in stale_refs
    assert adr.ref not in unfunded_refs  # it HAS a governing link -- just a dead one

    reasons = store.stale_reasons(project=PROJECT)
    assert adr.ref in reasons
    assert any(idr.ref in reason and "superseded" in reason for reason in reasons[adr.ref])


def test_stale_body_md_prose_ref_to_deprecated_record_is_flagged(store: Store) -> None:
    """A body_md prose ref to a deprecated record is flagged stale."""
    idr, _ = store.create("IDR", title="Bet to deprecate", status="accepted", project=PROJECT)
    store.set_status(idr.ref, "deprecated", project=PROJECT)
    adr, _ = store.create(
        "ADR",
        title="Decision mentioning the dead bet",
        body_md=f"This follows from {idr.ref}, which funded the original work.",
        project=PROJECT,
    )

    stale_refs = [r.ref for r in store.list(stale=True, project=PROJECT)]
    assert adr.ref in stale_refs

    reasons = store.stale_reasons(project=PROJECT)
    assert any(idr.ref in reason and "deprecated" in reason for reason in reasons[adr.ref])


def test_stale_body_md_prose_ref_to_nonexistent_record_is_flagged(store: Store) -> None:
    """A prose ref to a record that was never created is flagged stale."""
    adr, _ = store.create(
        "ADR",
        title="Decision mentioning a bet that never existed",
        body_md="This follows from IDR-9999, which does not exist in this project.",
        project=PROJECT,
    )

    stale_refs = [r.ref for r in store.list(stale=True, project=PROJECT)]
    assert adr.ref in stale_refs

    reasons = store.stale_reasons(project=PROJECT)
    assert any("IDR-9999" in reason and "does not exist" in reason for reason in reasons[adr.ref])


def test_stale_all_links_and_prose_pointing_at_accepted_records_is_not_flagged(
    store: Store,
) -> None:
    """A record whose links and prose all point at accepted records is not stale."""
    idr, _ = store.create("IDR", title="Healthy bet", status="accepted", project=PROJECT)
    adr, _ = store.create(
        "ADR",
        title="Work under a healthy bet",
        links=[{"ref": idr.ref, "relation": "implements-bet"}],
        body_md=f"See {idr.ref} for the funding bet.",
        project=PROJECT,
    )

    stale_refs = [r.ref for r in store.list(stale=True, project=PROJECT)]
    assert adr.ref not in stale_refs

    reasons = store.stale_reasons(project=PROJECT)
    assert adr.ref not in reasons


def test_stale_composes_with_rec_type_filter(store: Store) -> None:
    """stale=True composes with rec_type the same way other filters compose."""
    idr, _ = store.create("IDR", title="Bet to deprecate", status="accepted", project=PROJECT)
    store.set_status(idr.ref, "deprecated", project=PROJECT)
    stale_adr, _ = store.create(
        "ADR",
        title="Stale ADR",
        links=[{"ref": idr.ref, "relation": "implements-bet"}],
        project=PROJECT,
    )
    stale_mdr, _ = store.create(
        "MDR",
        title="Stale MDR",
        links=[{"ref": idr.ref, "relation": "implements-bet"}],
        project=PROJECT,
    )

    stale_adr_refs = [r.ref for r in store.list(stale=True, rec_type="ADR", project=PROJECT)]

    assert stale_adr_refs == [stale_adr.ref]
    assert stale_mdr.ref not in stale_adr_refs


def test_due_excludes_retired_bets(store: Store) -> None:
    """Superseded, deprecated and rejected IDRs never come due.

    A superseded bet handed its obligation to the record that replaced it,
    and a dead bet has nothing left to judge. Without this, every supersede
    would permanently add noise to dr_due -- the one report the register owes
    its operator. Found by dogfooding: the founding bet was superseded on day
    one and its predecessor kept reporting as due.

    draft/proposed DO still come due: a bet drafted, never decided, and left
    past its own review date is a lapsed intention worth surfacing.
    """
    fields = {"review_date": "2026-01-01"}

    # proposed and never decided -- still surfaces, deliberately
    pending, _ = store.create("IDR", title="Never decided", fields=fields, project=PROJECT)

    # accepted then superseded -- the successor carries the obligation
    old, _ = store.create("IDR", title="Old bet", fields=fields, project=PROJECT)
    store.set_status(old.ref, "accepted", project=PROJECT)
    new_bet, _ = store.supersede(
        old.ref, {"title": "Replacement bet", "fields": fields}, project=PROJECT
    )
    store.set_status(new_bet.ref, "accepted", project=PROJECT)

    # accepted then deprecated -- dead
    dead, _ = store.create("IDR", title="Killed bet", fields=fields, project=PROJECT)
    store.set_status(dead.ref, "accepted", project=PROJECT)
    store.set_status(dead.ref, "deprecated", project=PROJECT)

    due_refs = {r.ref for r in store.due(as_of="2026-06-01", project=PROJECT)}

    assert new_bet.ref in due_refs, "the accepted successor must come due"
    assert pending.ref in due_refs, "an undecided bet past its date still surfaces"
    assert old.ref not in due_refs, "a superseded bet must not come due"
    assert dead.ref not in due_refs, "a deprecated bet must not come due"


# ----------------------------------------------------------------------
# 14. Freshness signal, fields merge, and {{ref}} (production feedback)
# ----------------------------------------------------------------------


def test_project_version_bumps_on_every_write(store: Store) -> None:
    """The freshness counter increases on create, update and set_status.

    This is the cheap "did anything change" signal. A worker briefed at v47
    whose next call reports v52 knows its cached read is stale. Cost of the
    gap in real use: a full runbook written against a topology another
    session had already superseded.
    """
    start = store.project_version(project=PROJECT)

    record, _ = store.create("ADR", title="First", project=PROJECT)
    after_create = store.project_version(project=PROJECT)
    assert after_create > start

    store.update(record.ref, {"title": "Renamed"}, project=PROJECT)
    after_update = store.project_version(project=PROJECT)
    assert after_update > after_create

    store.set_status(record.ref, "accepted", project=PROJECT)
    assert store.project_version(project=PROJECT) > after_update


def test_list_since_returns_only_what_changed(store: Store) -> None:
    """since= filters on updated_at, answering "what did I miss"."""
    old, _ = store.create("ADR", title="Written before the briefing", project=PROJECT)
    cutoff = store.get(old.ref, project=PROJECT).updated_at

    new, _ = store.create("ADR", title="Filed by a concurrent session", project=PROJECT)

    changed = {r.ref for r in store.list(since=cutoff, project=PROJECT)}
    assert new.ref in changed
    assert old.ref not in changed, "strictly after: the cutoff record itself is not 'changed'"


def test_update_merges_fields_instead_of_replacing(store: Store) -> None:
    """Patching one fields key leaves the others intact.

    The whole point: changing review_date must not require re-transmitting
    bet/thesis/investment/success_criteria/kill_criteria verbatim, where one
    dropped key would silently destroy it on an accepted record.
    """
    record, _ = store.create(
        "IDR",
        title="The bet",
        fields={
            "bet": "one sentence",
            "thesis": "why",
            "investment": "10 days",
            "review_date": "2026-10-11",
        },
        project=PROJECT,
    )

    updated, _ = store.update(
        record.ref, {"fields": {"review_date": "2026-11-01"}}, project=PROJECT
    )

    assert updated.fields["review_date"] == "2026-11-01"
    assert updated.fields["bet"] == "one sentence", "merge must not drop untouched keys"
    assert updated.fields["thesis"] == "why"
    assert updated.fields["investment"] == "10 days"


def test_update_fields_explicit_none_deletes_the_key(store: Store) -> None:
    """Passing null for a fields key removes it, since merge alone cannot."""
    record, _ = store.create(
        "IDR", title="Bet", fields={"bet": "keep", "delegation": "drop me"}, project=PROJECT
    )
    updated, _ = store.update(record.ref, {"fields": {"delegation": None}}, project=PROJECT)
    assert "delegation" not in updated.fields
    assert updated.fields["bet"] == "keep"


def test_update_non_fields_attrs_still_replace_wholesale(store: Store) -> None:
    """Lists keep wholesale-replace semantics; only `fields` merges."""
    record, _ = store.create("ADR", title="X", tags=["a", "b"], project=PROJECT)
    updated, _ = store.update(record.ref, {"tags": ["c"]}, project=PROJECT)
    assert updated.tags == ["c"], "tags replace wholesale, they do not merge"


def test_update_names_the_correction_for_a_fields_key(store: Store) -> None:
    """{"delegation": ...} names the fix rather than saying "unknown key".

    A real operator error: delegation is a key inside fields, and the bare
    "Unknown patch key" sent them hunting.
    """
    record, _ = store.create("IDR", title="Bet", project=PROJECT)
    with pytest.raises(ValueError) as exc_info:
        store.update(record.ref, {"delegation": "none"}, project=PROJECT)
    message = str(exc_info.value)
    assert "inside `fields`" in message
    assert '{"fields": {"delegation": ...}}' in message


def test_ref_placeholder_is_substituted_on_create(store: Store) -> None:
    """{{ref}} becomes the assigned ref in body_md and in fields values."""
    record, _ = store.create(
        "ADR",
        title="Self-referencing",
        body_md="# {{ref}}: A decision\n\nSee {{ref}} for detail.",
        fields={"context": "Recorded as {{ref}}."},
        project=PROJECT,
    )
    assert "{{ref}}" not in record.body_md
    assert record.body_md.startswith(f"# {record.ref}: A decision")
    assert record.fields["context"] == f"Recorded as {record.ref}."


def test_self_reference_warning_fires_on_a_wrong_guessed_ref(store: Store) -> None:
    """A hand-written heading ref that isn't the assigned one warns.

    Happened twice in one evening under concurrent sessions: an agent wrote
    "# ADR-0023" into a record that landed as ADR-0024.
    """
    record, warnings = store.create(
        "ADR", title="Guessed wrong", body_md="# ADR-0099: Guessed\n\nBody.", project=PROJECT
    )
    assert record.ref != "ADR-0099"
    assert any("ADR-0099" in w and record.ref in w for w in warnings), warnings


def test_self_reference_warning_ignores_mid_body_references(store: Store) -> None:
    """Only the FIRST heading is checked; body prose cites other records freely.

    Guards against the obvious over-broad implementation: warning on every
    cross-reference would fire constantly and be learned-ignored within a day.
    """
    # Deliberately high refs so they cannot collide with this record's own,
    # which would make an unrelated warning (e.g. the unfunded one, which
    # names the record itself) look like a false positive from this check.
    _record, warnings = store.create(
        "ADR",
        title="Cites others",
        body_md="# {{ref}}: Mine\n\nSupersedes the approach in ADR-0901, per IDR-0902.",
        links=[{"ref": "IDR-0902", "relation": "implements-bet"}],
        project=PROJECT,
    )
    assert not any("ADR-0901" in w for w in warnings), warnings


def test_rejected_replacement_releases_its_predecessor(store: Store) -> None:
    """Rejecting a pending replacement clears the predecessor's pointer.

    Nothing is replacing it after all, so leaving superseded_by set would
    claim otherwise. The rejected record keeps its own `supersedes` as a
    record of what it was trying to replace.
    """
    old, _ = store.create("ADR", title="Original", project=PROJECT)
    store.set_status(old.ref, "accepted", project=PROJECT)
    new, _ = store.supersede(old.ref, {"title": "Attempted replacement"}, project=PROJECT)

    store.set_status(new.ref, "rejected", project=PROJECT)

    refetched_old = store.get(old.ref, project=PROJECT)
    assert refetched_old.superseded_by is None, "pointer released"
    assert refetched_old.status == "accepted", "predecessor was never retired"
    assert store.get(new.ref, project=PROJECT).supersedes == old.ref, "intent preserved"


def test_adopt_successor_links_a_standalone_draft(store: Store) -> None:
    """An already-drafted record can be linked as a successor after the fact.

    supersedes/superseded_by are structural and cannot be patched, so without
    this a supersession drafted now and accepted later could never be linked
    at all — the relationship would live only in prose and status notes, which
    no query can follow. Reported from real use.
    """
    old, _ = store.create("ADR", title="Original topology", project=PROJECT)
    store.set_status(old.ref, "accepted", project=PROJECT)
    # Drafted standalone, the way the prose workaround produced it.
    standalone, _ = store.create(
        "ADR", title="Revised topology", body_md="Supersedes on acceptance.", project=PROJECT
    )

    successor, warnings = store.adopt_successor(old.ref, standalone.ref, project=PROJECT)

    assert successor.supersedes == old.ref
    assert store.get(old.ref, project=PROJECT).superseded_by == standalone.ref
    assert store.get(old.ref, project=PROJECT).status == "accepted", "deferred, as with supersede"
    assert any("until" in w for w in warnings), warnings

    store.set_status(standalone.ref, "accepted", project=PROJECT)
    assert store.get(old.ref, project=PROJECT).status == "superseded"


def test_adopt_successor_repairs_a_superseded_record_with_no_link(store: Store) -> None:
    """The exact broken state the prose workaround leaves behind.

    A record manually set to "superseded" carries a null superseded_by,
    because the cross-link could only ever be written by dr_supersede at
    creation. Adopting fills it in without disturbing the status.
    """
    old, _ = store.create("ADR", title="Original", project=PROJECT)
    store.set_status(old.ref, "accepted", project=PROJECT)
    replacement, _ = store.create("ADR", title="Replacement", project=PROJECT)
    store.set_status(replacement.ref, "accepted", project=PROJECT)
    # Hand-driven, as the operator had to do it: status set, link impossible.
    store.set_status(old.ref, "superseded", note="superseded by the new one", project=PROJECT)
    assert store.get(old.ref, project=PROJECT).superseded_by is None, "the reported gap"

    _successor, warnings = store.adopt_successor(old.ref, replacement.ref, project=PROJECT)

    repaired = store.get(old.ref, project=PROJECT)
    assert repaired.superseded_by == replacement.ref, "link repaired"
    assert repaired.status == "superseded", "status untouched"
    assert any("repaired" in w for w in warnings), warnings


def test_adopt_successor_rejects_self_supersession(store: Store) -> None:
    """A record cannot supersede itself."""
    record, _ = store.create("ADR", title="Only one", project=PROJECT)
    with pytest.raises(InvalidRefError):
        store.adopt_successor(record.ref, record.ref, project=PROJECT)


# ----------------------------------------------------------------------
# 15. "No record, no decision": dr_answer(no_record=...) and
#     escalations(unrecorded=True)
# ----------------------------------------------------------------------


def test_answer_without_produces_or_no_record_warns_naming_both_remedies(
    store: Store,
) -> None:
    """Answering with neither produces nor no_record warns, naming both
    remedies (pass produces, or set no_record=True)."""
    escalation = _escalate(store, question="Should we do X?")

    updated, record, warnings = store.answer(
        escalation.ref, resolution="Yes, do X.", project=PROJECT
    )

    assert record is None
    assert updated.no_record is False
    assert any("produces" in w and "no_record" in w for w in warnings)


def test_answer_with_produces_does_not_warn_no_record(store: Store) -> None:
    """Answering with produces suppresses the no-record warning entirely."""
    escalation = _escalate(store, question="How should retries work?")

    _updated, record, warnings = store.answer(
        escalation.ref,
        resolution="Use exponential backoff.",
        produces={"rec_type": "ADR", "title": "Use exponential backoff for retries"},
        project=PROJECT,
    )

    assert record is not None
    assert not any("no_record" in w for w in warnings)


def test_answer_with_no_record_true_does_not_warn_and_is_persisted(store: Store) -> None:
    """no_record=True suppresses the warning and durably marks the escalation."""
    escalation = _escalate(store, question="Rename the bucket?")

    updated, record, warnings = store.answer(
        escalation.ref, resolution="Don't do that.", no_record=True, project=PROJECT
    )

    assert record is None
    assert updated.no_record is True
    assert warnings == []

    refetched = store.escalations(status="answered", project=PROJECT)
    assert next(e for e in refetched if e.ref == escalation.ref).no_record is True


def test_escalations_unrecorded_returns_exactly_the_answered_no_record_not_flagged_set(
    store: Store,
) -> None:
    """unrecorded=True returns exactly the escalations that are
    status="answered" AND produced_ref is None AND no_record is False --
    excluding open ones, no_record=True ones, and ones that produced a
    record."""
    still_open = _escalate(store, question="Still open?")
    forgotten = _escalate(store, question="Forgotten record?")
    store.answer(forgotten.ref, resolution="Forgot to write it down.", project=PROJECT)

    deliberate = _escalate(store, question="Deliberately no record?")
    store.answer(
        deliberate.ref, resolution="Don't do that.", no_record=True, project=PROJECT
    )

    recorded = _escalate(store, question="Produced a record?")
    store.answer(
        recorded.ref,
        resolution="Use exponential backoff.",
        produces={"rec_type": "ADR", "title": "Use exponential backoff for retries"},
        project=PROJECT,
    )

    unrecorded_refs = {e.ref for e in store.escalations(unrecorded=True, project=PROJECT)}

    assert unrecorded_refs == {forgotten.ref}
    assert still_open.ref not in unrecorded_refs
    assert deliberate.ref not in unrecorded_refs
    assert recorded.ref not in unrecorded_refs


# ----------------------------------------------------------------------
# 16. dr_revise: reasoning changed, decision did not
# ----------------------------------------------------------------------


def test_revise_replaces_body_md_and_preserves_previous_text_in_revision_entry(
    store: Store,
) -> None:
    """revise() replaces body_md and keeps the exact prior text in the
    revision entry -- the whole point."""
    record, _ = store.create(
        "ADR",
        title="Use DynamoDB",
        body_md="Original reasoning: cost and latency.",
        project=PROJECT,
    )

    updated, warnings = store.revise(
        record.ref,
        body_md="Revised reasoning: cost alone still justifies it.",
        note="ADR-0024 removed the latency benefit this cited.",
        project=PROJECT,
    )

    assert warnings == []
    assert updated.body_md == "Revised reasoning: cost alone still justifies it."
    assert len(updated.revisions) == 1
    assert updated.revisions[0].previous_body_md == "Original reasoning: cost and latency."
    assert updated.revisions[0].note == "ADR-0024 removed the latency benefit this cited."
    assert updated.updated_at != record.updated_at


def test_revise_blank_or_missing_note_raises(store: Store) -> None:
    """An empty or whitespace-only note is a rejected call."""
    record, _ = store.create("ADR", title="Decision", body_md="Original.", project=PROJECT)

    with pytest.raises(MissingRevisionNoteError):
        store.revise(record.ref, body_md="New text.", note="", project=PROJECT)

    with pytest.raises(MissingRevisionNoteError):
        store.revise(record.ref, body_md="New text.", note="   ", project=PROJECT)


def test_revise_on_accepted_record_does_not_emit_supersede_warning(store: Store) -> None:
    """Unlike update(), revise() is legal on "accepted" and never emits the
    accepted-record supersede warning -- this IS the right tool for it."""
    record, _ = store.create("ADR", title="Decision", body_md="Original.", project=PROJECT)
    store.set_status(record.ref, "accepted", project=PROJECT)

    updated, warnings = store.revise(
        record.ref, body_md="New reasoning.", note="Rationale changed.", project=PROJECT
    )

    assert updated.status == "accepted"
    assert warnings == []


def test_revise_multiple_revisions_accumulate_in_order(store: Store) -> None:
    """Repeated revisions append to `revisions` in order, each preserving
    its own prior text."""
    record, _ = store.create("ADR", title="Decision", body_md="v1", project=PROJECT)

    store.revise(record.ref, body_md="v2", note="first revision", project=PROJECT)
    after_second, _ = store.revise(
        record.ref, body_md="v3", note="second revision", project=PROJECT
    )

    assert after_second.body_md == "v3"
    assert [r.previous_body_md for r in after_second.revisions] == ["v1", "v2"]
    assert [r.note for r in after_second.revisions] == ["first revision", "second revision"]


# ----------------------------------------------------------------------
# 17. dr_search tokenization -- regression test for the reported live bug
# ----------------------------------------------------------------------


def test_search_multi_term_query_finds_record_matching_only_one_term(store: Store) -> None:
    """The reported failure: a 60-character multi-term query must find a
    record containing just one of its terms as a substring, even though the
    record never contains the full seven-word phrase verbatim."""
    record, _ = store.create(
        "ADR",
        title="Deploy runner authentication",
        fields={
            "decision": (
                "Use a scoped service account for the deploy runner, not a "
                "shared admin credential."
            )
        },
        project=PROJECT,
    )

    results = store.search(
        "shared credentials runner service account deploy authentication SAML",
        project=PROJECT,
    )

    refs = [r.ref for r, _count in results]
    assert record.ref in refs


def test_search_ranks_by_matched_term_count_descending(store: Store) -> None:
    """A record matching more DISTINCT terms outranks one matching fewer."""
    weak, _ = store.create(
        "ADR", title="Unrelated decision", body_md="Mentions deploy once.", project=PROJECT
    )
    strong, _ = store.create(
        "ADR",
        title="Deploy runner IAM role authentication",
        body_md="Uses an IAM role, a deploy runner, and authentication together.",
        project=PROJECT,
    )

    results = store.search("deploy runner IAM role authentication", project=PROJECT)
    refs_in_order = [r.ref for r, _count in results]
    counts = {r.ref: count for r, count in results}

    assert refs_in_order.index(strong.ref) < refs_in_order.index(weak.ref)
    assert counts[strong.ref] > counts[weak.ref]


def test_search_single_term_query_behaves_exactly_as_before(
    store: Store, dynamodb_resource: Any
) -> None:
    """A single-term query is an unchanged case: plain case-insensitive
    substring match, ranked newest-first -- no regression from tokenizing."""
    older, _ = store.create(
        "ADR", title="Ship saved-search to beta users (prototype)", project=PROJECT
    )
    newer, _ = store.create(
        "ADR", title="Ship saved-search to beta users (v2)", project=PROJECT
    )
    unrelated, _ = store.create("ADR", title="Unrelated decision", project=PROJECT)
    _stamp_created_at(dynamodb_resource, store.table_name, older.ref, "2020-01-01T00:00:00+00:00")
    _stamp_created_at(dynamodb_resource, store.table_name, newer.ref, "2026-01-01T00:00:00+00:00")

    results = store.search("saved-search", project=PROJECT)
    refs_in_order = [r.ref for r, _count in results]

    assert unrelated.ref not in refs_in_order
    assert refs_in_order == [newer.ref, older.ref]  # created_at descending, same as before
    assert all(count == 1 for _r, count in results)


def test_search_query_matching_nothing_returns_empty(store: Store) -> None:
    """A query whose terms appear nowhere returns an empty list, not an error."""
    store.create("ADR", title="Use DynamoDB single-table design", project=PROJECT)

    assert store.search("nonexistent-term-xyz", project=PROJECT) == []


# ----------------------------------------------------------------------
# 18. dr_withdraw: the third honest ending for an escalation
# ----------------------------------------------------------------------


def test_withdraw_sets_status_reason_and_timestamp(store: Store) -> None:
    """withdraw() sets status="withdrawn", stamps withdrawn_at, and stores
    withdrawn_reason -- and does not touch produced_ref/resolution/
    answered_at, which stay unset (this was never answered)."""
    escalation = _escalate(store, question="Is this still needed?")

    updated, warnings = store.withdraw(
        escalation.ref, reason="Resolved by ESC-9999 already; this is stale.", project=PROJECT
    )

    assert updated.status == "withdrawn"
    assert updated.withdrawn_at is not None
    assert updated.withdrawn_reason == "Resolved by ESC-9999 already; this is stale."
    assert updated.resolution is None
    assert updated.answered_at is None
    assert updated.produced_ref is None
    assert warnings == []

    # Durable, not just the return value.
    refetched = store.escalations(status="withdrawn", project=PROJECT)
    assert escalation.ref in [e.ref for e in refetched]


def test_withdraw_blank_reason_raises(store: Store) -> None:
    """A blank or missing reason is rejected -- a withdrawal with no reason
    is indistinguishable from an abandoned escalation."""
    escalation = _escalate(store, question="Still relevant?")

    with pytest.raises(MissingWithdrawalReasonError):
        store.withdraw(escalation.ref, reason="", project=PROJECT)

    with pytest.raises(MissingWithdrawalReasonError):
        store.withdraw(escalation.ref, reason="   ", project=PROJECT)


def test_withdraw_an_answered_escalation_raises_naming_its_status(store: Store) -> None:
    """Withdrawing an already-answered escalation is rejected, naming the
    status and the resolution already on record -- an answered escalation
    recorded a decision that actually happened; withdrawing it would erase
    that decision."""
    escalation = _escalate(store, question="Ship now?")
    store.answer(escalation.ref, resolution="Yes, ship it.", project=PROJECT)

    with pytest.raises(EscalationAlreadyResolvedError) as exc_info:
        store.withdraw(escalation.ref, reason="Never mind.", project=PROJECT)

    message = str(exc_info.value)
    assert "answered" in message
    assert "Yes, ship it." in message


def test_withdraw_an_already_withdrawn_escalation_raises(store: Store) -> None:
    """Withdrawing an already-withdrawn escalation is rejected too --
    withdrawal, like answering, happens exactly once."""
    escalation = _escalate(store, question="Still open?")
    store.withdraw(escalation.ref, reason="Overtaken by events.", project=PROJECT)

    with pytest.raises(EscalationAlreadyResolvedError) as exc_info:
        store.withdraw(escalation.ref, reason="Withdrawing again.", project=PROJECT)

    assert "withdrawn" in str(exc_info.value)


def test_withdrawn_escalation_does_not_appear_in_unrecorded_sweep(store: Store) -> None:
    """THE LOAD-BEARING TEST: a withdrawn escalation must NOT appear in
    escalations(unrecorded=True).

    This is the entire reason dr_withdraw exists. Before it did, the only
    way to clear a moot escalation was answer() with a throwaway
    resolution, which made it indistinguishable from a genuine decision
    that leaked without a record -- corrupting the unrecorded sweep. A
    withdrawn escalation owes no record, so it must never show up here,
    even though it shares "answer() was never really the right verb for
    this" with the escalations that legitimately do show up.
    """
    withdrawn = _escalate(store, question="Moot by the time anyone looked?")
    store.withdraw(withdrawn.ref, reason="Overtaken by events.", project=PROJECT)

    forgotten = _escalate(store, question="A genuine, unrecorded decision.")
    store.answer(forgotten.ref, resolution="Forgot to write it down.", project=PROJECT)

    unrecorded_refs = {e.ref for e in store.escalations(unrecorded=True, project=PROJECT)}

    assert withdrawn.ref not in unrecorded_refs
    assert forgotten.ref in unrecorded_refs


def test_withdraw_duplicate_of_self_reference_raises(store: Store) -> None:
    """duplicate_of naming the escalation's own ref is rejected."""
    escalation = _escalate(store, question="Same underlying decision as itself?")

    with pytest.raises(InvalidRefError):
        store.withdraw(
            escalation.ref,
            reason="Duplicate.",
            duplicate_of=escalation.ref,
            project=PROJECT,
        )


def test_withdraw_duplicate_of_nonexistent_target_raises(store: Store) -> None:
    """duplicate_of pointing at a well-formed but nonexistent ref is
    rejected -- unlike record links, a duplicate must point at something
    real, since the point is redirecting a reader to where the decision
    actually lives."""
    escalation = _escalate(store, question="Duplicate of what, exactly?")

    with pytest.raises(EscalationNotFoundError):
        store.withdraw(
            escalation.ref,
            reason="Duplicate.",
            duplicate_of="ESC-9999",
            project=PROJECT,
        )


def test_withdraw_duplicate_of_malformed_ref_raises(store: Store) -> None:
    """A malformed duplicate_of ref is rejected."""
    escalation = _escalate(store, question="Duplicate of a typo?")

    with pytest.raises(InvalidRefError):
        store.withdraw(
            escalation.ref,
            reason="Duplicate.",
            duplicate_of="not-a-ref",
            project=PROJECT,
        )


def test_withdraw_duplicate_of_a_withdrawn_duplicate_warns_naming_ultimate_target(
    store: Store,
) -> None:
    """When duplicate_of points at an escalation that is itself withdrawn
    as a duplicate, the response warns and names the ULTIMATE target --
    the end of the chain, not just the immediate one hop away -- so a
    reader is redirected straight to where the decision actually lives
    instead of being sent down a two-hop path that might be dead."""
    original = _escalate(store, question="Where the decision actually lives.")
    middle = _escalate(store, question="An earlier duplicate report.")
    store.withdraw(middle.ref, reason="Duplicate.", duplicate_of=original.ref, project=PROJECT)

    newest = _escalate(store, question="Yet another report of the same thing.")
    updated, warnings = store.withdraw(
        newest.ref, reason="Duplicate.", duplicate_of=middle.ref, project=PROJECT
    )

    assert updated.duplicate_of == middle.ref
    assert any(original.ref in w and middle.ref in w for w in warnings)


def test_withdraw_duplicate_of_a_non_withdrawn_target_does_not_warn(store: Store) -> None:
    """duplicate_of pointing at an open (not withdrawn) escalation produces
    no chain warning -- there is nothing to warn about."""
    original = _escalate(store, question="Still open, not a duplicate chain.")
    dup = _escalate(store, question="Duplicate of the still-open one.")

    _updated, warnings = store.withdraw(
        dup.ref, reason="Duplicate.", duplicate_of=original.ref, project=PROJECT
    )

    assert warnings == []


def test_escalations_status_withdrawn_filter_returns_withdrawn_items(store: Store) -> None:
    """escalations(status="withdrawn") returns withdrawn escalations
    through the existing status filter, and excludes open/answered ones."""
    open_one = _escalate(store, question="Still open.")
    answered = _escalate(store, question="Was answered.")
    store.answer(answered.ref, resolution="Done.", project=PROJECT)
    withdrawn = _escalate(store, question="Was withdrawn.")
    store.withdraw(withdrawn.ref, reason="Moot.", project=PROJECT)

    results = store.escalations(status="withdrawn", project=PROJECT)
    refs = {e.ref for e in results}

    assert refs == {withdrawn.ref}
    assert open_one.ref not in refs
    assert answered.ref not in refs
