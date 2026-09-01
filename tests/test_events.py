"""Tests for `triad_dr.events.handler` against realistic DynamoDB Streams
wire-format events (IDR-0003: event-driven notification of decision records).

No AWS is touched -- these are pure handler tests. The SNS client is a small
in-memory stub swapped in for `boto3.client("sns")` via monkeypatch, per the
task's "no AWS needed beyond mocking the SNS client" instruction. DynamoDB
Streams wire-format images (``{"S": "..."}``-style AttributeValues) are built
with `boto3.dynamodb.types.TypeSerializer` so the fixtures are the real wire
shape, not a hand-rolled approximation of it.

Covers:
  - INSERT of a REC# item -> record.created, with all 5 message attributes
  - MODIFY status change -> record.status_changed, carrying from/to
  - MODIFY reviews grew -> review.recorded, carrying verdict/cause
  - MODIFY reviews grew with no cause -> cause is None, not dropped
  - MODIFY pure content edit -> publishes nothing (the noise exclusion)
  - CTR#/META#PROJECTS items -> publish nothing (the three-writes problem)
  - ESC# item -> publishes nothing (out of scope for this bet)
  - body_md excluded from the published body, body_md_excluded true
  - a malformed record in a batch does not block the rest of the batch
  - missing DR_EVENTS_TOPIC_ARN -> no crash, nothing published
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from boto3.dynamodb.types import TypeSerializer

from triad_dr import events

TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:dr-events"

_serializer = TypeSerializer()


class _FakeSnsClient:
    """Records every `publish` call instead of touching real SNS."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> dict[str, Any]:
        self.published.append(kwargs)
        return {"MessageId": "fake-message-id"}


@pytest.fixture
def fake_sns(monkeypatch: pytest.MonkeyPatch) -> _FakeSnsClient:
    """Swap `boto3.client("sns")` for an in-memory stub."""
    client = _FakeSnsClient()
    monkeypatch.setattr(events.boto3, "client", lambda *args, **kwargs: client)
    return client


def _wire_image(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a native Python item dict into DynamoDB Streams wire format."""
    return {key: _serializer.serialize(value) for key, value in item.items()}


def _stream_record(
    event_name: str,
    keys: dict[str, Any],
    new_image: dict[str, Any] | None = None,
    old_image: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one realistic DynamoDB Streams record, in wire format."""
    dynamodb_data: dict[str, Any] = {"Keys": _wire_image(keys)}
    if new_image is not None:
        dynamodb_data["NewImage"] = _wire_image(new_image)
    if old_image is not None:
        dynamodb_data["OldImage"] = _wire_image(old_image)
    return {
        "eventID": "1",
        "eventName": event_name,
        "eventSource": "aws:dynamodb",
        "dynamodb": dynamodb_data,
    }


def _base_record(**overrides: Any) -> dict[str, Any]:
    """A full REC# item, shaped like `store.py`'s `_to_item` output."""
    item: dict[str, Any] = {
        "pk": "PROJ#acme-app",
        "sk": "REC#ADR#0001",
        "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "project": "acme-app",
        "rec_type": "ADR",
        "seq": 1,
        "ref": "ADR-0001",
        "title": "Use DynamoDB single-table design",
        "status": "proposed",
        "persona": "builder",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "deciders": [],
        "tags": ["infra"],
        "links": [{"ref": "IDR-0001", "relation": "implements-bet"}],
        "supersedes": None,
        "superseded_by": None,
        "status_history": [
            {"at": "2026-08-01T00:00:00+00:00", "status": "proposed", "note": "created"}
        ],
        "reviews": [],
        "fields": {"context": "because we needed one"},
        "body_md": "# ADR-0001: Use DynamoDB\n\nBody text nobody should see over SNS.",
    }
    item.update(overrides)
    return item


# ----------------------------------------------------------------------
# INSERT -> record.created
# ----------------------------------------------------------------------


def test_insert_of_rec_item_emits_record_created_with_all_attributes(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """An INSERT of a REC# item publishes exactly one record.created event
    with all five message attributes set correctly."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    new_image = _base_record()
    record = _stream_record(
        "INSERT", {"pk": new_image["pk"], "sk": new_image["sk"]}, new_image=new_image
    )

    result = events.handler({"Records": [record]}, None)

    assert result == {"published": 1, "skipped": 0, "failed": 0}
    assert len(fake_sns.published) == 1
    call = fake_sns.published[0]
    assert call["TopicArn"] == TOPIC_ARN
    assert call["MessageAttributes"] == {
        "project": {"DataType": "String", "StringValue": "acme-app"},
        "rec_type": {"DataType": "String", "StringValue": "ADR"},
        "persona": {"DataType": "String", "StringValue": "builder"},
        "status": {"DataType": "String", "StringValue": "proposed"},
        "event_type": {"DataType": "String", "StringValue": "record.created"},
    }
    body = json.loads(call["Message"])
    assert body["event_type"] == "record.created"
    assert body["ref"] == "ADR-0001"
    assert body["title"] == "Use DynamoDB single-table design"


# ----------------------------------------------------------------------
# MODIFY status change -> record.status_changed
# ----------------------------------------------------------------------


def test_modify_status_change_emits_status_changed_with_from_to(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """A MODIFY where status differs publishes record.status_changed,
    carrying from/to."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    old_image = _base_record(status="proposed")
    new_image = _base_record(
        status="accepted",
        updated_at="2026-08-02T00:00:00+00:00",
        status_history=old_image["status_history"]
        + [{"at": "2026-08-02T00:00:00+00:00", "status": "accepted", "note": None}],
    )
    record = _stream_record(
        "MODIFY",
        {"pk": new_image["pk"], "sk": new_image["sk"]},
        new_image=new_image,
        old_image=old_image,
    )

    result = events.handler({"Records": [record]}, None)

    assert result == {"published": 1, "skipped": 0, "failed": 0}
    call = fake_sns.published[0]
    assert call["MessageAttributes"]["event_type"]["StringValue"] == "record.status_changed"
    assert call["MessageAttributes"]["status"]["StringValue"] == "accepted"
    body = json.loads(call["Message"])
    assert body["event_type"] == "record.status_changed"
    assert body["from"] == "proposed"
    assert body["to"] == "accepted"


# ----------------------------------------------------------------------
# MODIFY reviews grew -> review.recorded
# ----------------------------------------------------------------------


def test_modify_reviews_grew_emits_review_recorded_with_verdict_and_cause(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """A MODIFY where `reviews` grew publishes review.recorded, carrying
    the newest review's verdict and cause."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    old_image = _base_record(
        rec_type="IDR", persona="investor", sk="REC#IDR#0001", ref="IDR-0001", reviews=[]
    )
    new_review = {
        "at": "2026-08-05T00:00:00+00:00",
        "verdict": "missed",
        "evidence": "Nobody used it, per usage dashboard.",
        "cause": "market",
        "note": None,
    }
    new_image = _base_record(
        rec_type="IDR",
        persona="investor",
        sk="REC#IDR#0001",
        ref="IDR-0001",
        reviews=[new_review],
        updated_at="2026-08-05T00:00:00+00:00",
    )
    record = _stream_record(
        "MODIFY",
        {"pk": new_image["pk"], "sk": new_image["sk"]},
        new_image=new_image,
        old_image=old_image,
    )

    result = events.handler({"Records": [record]}, None)

    assert result == {"published": 1, "skipped": 0, "failed": 0}
    body = json.loads(fake_sns.published[0]["Message"])
    assert body["event_type"] == "review.recorded"
    assert body["verdict"] == "missed"
    assert body["cause"] == "market"


def test_modify_reviews_grew_without_cause_is_none_not_dropped(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """A review with no cause (e.g. verdict="met") carries cause=None,
    rather than omitting the key."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    old_image = _base_record(
        rec_type="IDR", persona="investor", sk="REC#IDR#0002", ref="IDR-0002", reviews=[]
    )
    new_review = {
        "at": "2026-08-05T00:00:00+00:00",
        "verdict": "met",
        "evidence": "Success criteria hit per dashboard.",
        "cause": None,
        "note": None,
    }
    new_image = _base_record(
        rec_type="IDR",
        persona="investor",
        sk="REC#IDR#0002",
        ref="IDR-0002",
        reviews=[new_review],
    )
    record = _stream_record(
        "MODIFY",
        {"pk": new_image["pk"], "sk": new_image["sk"]},
        new_image=new_image,
        old_image=old_image,
    )

    events.handler({"Records": [record]}, None)

    body = json.loads(fake_sns.published[0]["Message"])
    assert body["verdict"] == "met"
    assert "cause" in body
    assert body["cause"] is None


def test_modify_both_status_change_and_review_emits_both_events(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """A single MODIFY that is both a status change and a review growth
    emits both events, not just one."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    old_image = _base_record(
        rec_type="IDR",
        persona="investor",
        sk="REC#IDR#0003",
        ref="IDR-0003",
        status="accepted",
        reviews=[],
    )
    new_review = {
        "at": "2026-08-05T00:00:00+00:00",
        "verdict": "missed",
        "evidence": "Missed on delivery.",
        "cause": "delivery",
        "note": None,
    }
    new_image = _base_record(
        rec_type="IDR",
        persona="investor",
        sk="REC#IDR#0003",
        ref="IDR-0003",
        status="deprecated",
        reviews=[new_review],
    )
    record = _stream_record(
        "MODIFY",
        {"pk": new_image["pk"], "sk": new_image["sk"]},
        new_image=new_image,
        old_image=old_image,
    )

    result = events.handler({"Records": [record]}, None)

    assert result == {"published": 2, "skipped": 0, "failed": 0}
    event_types = {call["MessageAttributes"]["event_type"]["StringValue"] for call in fake_sns.published}
    assert event_types == {"record.status_changed", "review.recorded"}


# ----------------------------------------------------------------------
# MODIFY pure content edit -> publishes nothing
# ----------------------------------------------------------------------


def test_modify_pure_content_edit_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """A MODIFY that only touches title/body_md/updated_at -- no status or
    reviews change -- publishes nothing."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    old_image = _base_record(title="Original title")
    new_image = _base_record(title="Revised title", updated_at="2026-08-03T00:00:00+00:00")
    record = _stream_record(
        "MODIFY",
        {"pk": new_image["pk"], "sk": new_image["sk"]},
        new_image=new_image,
        old_image=old_image,
    )

    result = events.handler({"Records": [record]}, None)

    assert result == {"published": 0, "skipped": 1, "failed": 0}
    assert fake_sns.published == []


# ----------------------------------------------------------------------
# CTR# / META#PROJECTS -> publish nothing
# ----------------------------------------------------------------------


def test_counter_and_project_registry_items_publish_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """CTR#<TYPE> counter writes and META#PROJECTS registry writes -- the
    two pieces of bookkeeping that ride along with every real write --
    publish nothing."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    ctr_old = {"pk": "PROJ#acme-app", "sk": "CTR#ADR", "next_seq": 4}
    ctr_new = {"pk": "PROJ#acme-app", "sk": "CTR#ADR", "next_seq": 5}
    ctr_record = _stream_record(
        "MODIFY",
        {"pk": ctr_new["pk"], "sk": ctr_new["sk"]},
        new_image=ctr_new,
        old_image=ctr_old,
    )
    proj_old = {
        "pk": "META#PROJECTS",
        "sk": "PROJ#acme-app",
        "project": "acme-app",
        "created_at": "2026-08-01T00:00:00+00:00",
        "version": 5,
    }
    proj_new = {**proj_old, "version": 6}
    proj_record = _stream_record(
        "MODIFY",
        {"pk": proj_new["pk"], "sk": proj_new["sk"]},
        new_image=proj_new,
        old_image=proj_old,
    )

    result = events.handler({"Records": [ctr_record, proj_record]}, None)

    assert result == {"published": 0, "skipped": 2, "failed": 0}
    assert fake_sns.published == []


# ----------------------------------------------------------------------
# ESC# -> publishes nothing (out of scope for this bet)
# ----------------------------------------------------------------------


def test_escalation_item_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """An ESC# escalation item -- even a brand-new one -- publishes nothing."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    esc_image = {
        "pk": "PROJ#acme-app",
        "sk": "ESC#0001",
        "ref": "ESC-0001",
        "project": "acme-app",
        "raised_by": "agent-x",
        "raised_at": "2026-08-01T00:00:00+00:00",
        "question": "What now?",
        "context": "Stuck.",
        "options": [],
        "recommendation": "Proceed.",
        "cost_of_delay": "Nothing until Friday.",
        "blocking": False,
        "governing_ref": None,
        "status": "open",
        "answered_at": None,
        "resolution": None,
        "produced_ref": None,
    }
    record = _stream_record(
        "INSERT", {"pk": esc_image["pk"], "sk": esc_image["sk"]}, new_image=esc_image
    )

    result = events.handler({"Records": [record]}, None)

    assert result == {"published": 0, "skipped": 1, "failed": 0}
    assert fake_sns.published == []


# ----------------------------------------------------------------------
# body_md exclusion
# ----------------------------------------------------------------------


def test_body_md_excluded_from_published_body(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """`body_md` never appears in the published message body, and
    `body_md_excluded` marks the omission as intentional."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    new_image = _base_record(body_md="# Long prose that should never leave the table.")
    record = _stream_record(
        "INSERT", {"pk": new_image["pk"], "sk": new_image["sk"]}, new_image=new_image
    )

    events.handler({"Records": [record]}, None)

    body = json.loads(fake_sns.published[0]["Message"])
    assert "body_md" not in body
    assert body["body_md_excluded"] is True


# ----------------------------------------------------------------------
# Robustness
# ----------------------------------------------------------------------


def test_malformed_record_does_not_block_the_rest_of_the_batch(
    monkeypatch: pytest.MonkeyPatch, fake_sns: _FakeSnsClient
) -> None:
    """A malformed stream record (missing the `dynamodb` key entirely) is
    logged and skipped as failed, without preventing a good record later
    in the same batch from publishing."""
    monkeypatch.setenv("DR_EVENTS_TOPIC_ARN", TOPIC_ARN)
    malformed_record = {"eventID": "0", "eventName": "INSERT", "eventSource": "aws:dynamodb"}
    good_image = _base_record()
    good_record = _stream_record(
        "INSERT", {"pk": good_image["pk"], "sk": good_image["sk"]}, new_image=good_image
    )

    result = events.handler({"Records": [malformed_record, good_record]}, None)

    assert result == {"published": 1, "skipped": 0, "failed": 1}
    assert len(fake_sns.published) == 1
    assert json.loads(fake_sns.published[0]["Message"])["ref"] == good_image["ref"]


def test_missing_topic_arn_does_not_crash_and_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With DR_EVENTS_TOPIC_ARN unset, the handler never constructs an SNS
    client, never crashes, and reports nothing published."""
    monkeypatch.delenv("DR_EVENTS_TOPIC_ARN", raising=False)
    call_count = {"n": 0}

    def _unexpected_client_call(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        raise AssertionError("boto3.client should not be called when the topic ARN is unset")

    monkeypatch.setattr(events.boto3, "client", _unexpected_client_call)

    new_image = _base_record()
    record = _stream_record(
        "INSERT", {"pk": new_image["pk"], "sk": new_image["sk"]}, new_image=new_image
    )

    result = events.handler({"Records": [record]}, None)

    assert result == {"published": 0, "skipped": 1, "failed": 0}
    assert call_count["n"] == 0
