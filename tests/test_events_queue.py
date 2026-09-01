"""Tests for `triad_dr.events_queue` -- the SQS client for the durable
decision-event subscriber queue (IDR-0003, `DrEventsQueue`).

moto-backed (`mock_aws` supports SQS the same way `tests/conftest.py`'s
fixtures use it for DynamoDB). Reuses the `aws_credentials` fixture from
`conftest.py`. Covers:

  - `pending` returns parsed events with receipt handles and message
    attributes intact, including event-type-specific extras (e.g.
    record.status_changed's from/to)
  - `pending` on an empty queue returns `[]` without raising
  - `ack` deletes only the handles it is given; whatever is left unacked
    remains on the queue and is genuinely redelivered once a real 1-second
    visibility timeout elapses (not simulated)
  - `ack`'s partial-failure reporting: a bogus handle mixed with a real one
    still deletes the real one and returns the true count, not the
    requested count
  - a missing `DR_EVENTS_QUEUE_URL` raises `EventsQueueNotConfiguredError`
    naming the env var
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from triad_dr.errors import EventsQueueNotConfiguredError
from triad_dr.events_queue import EventQueue

QUEUE_NAME = "triad-decision-registers-events-queue-test"


@pytest.fixture
def sqs_client(aws_credentials: None) -> Iterator[Any]:
    """Start moto's SQS mock and yield a boto3 SQS client.

    Args:
        aws_credentials: Ensures dummy credentials are in place first
            (shared fixture from `tests/conftest.py`).
    """
    with mock_aws():
        yield boto3.client("sqs", region_name="us-east-1")


@pytest.fixture
def queue_url(sqs_client: Any) -> str:
    """Create the test queue and return its URL."""
    response = sqs_client.create_queue(QueueName=QUEUE_NAME)
    return str(response["QueueUrl"])


@pytest.fixture
def event_queue(sqs_client: Any, queue_url: str) -> EventQueue:
    """Build an `EventQueue` wired to the mocked SQS queue."""
    return EventQueue(queue_url=queue_url, client=sqs_client)


def _publish_raw_event(
    sqs_client: Any, queue_url: str, body: dict[str, Any], attributes: dict[str, str]
) -> None:
    """Send one message shaped like an SNS raw-delivery event: JSON body,
    String message attributes -- matching what `DrEventsQueue`'s
    `raw_message_delivery=True` subscription actually delivers.
    """
    sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(body),
        MessageAttributes={
            name: {"DataType": "String", "StringValue": value}
            for name, value in attributes.items()
        },
    )


def _sample_body(**overrides: Any) -> dict[str, Any]:
    """A record.created message body, shaped like events.py's `_summary()`."""
    body: dict[str, Any] = {
        "event_type": "record.created",
        "ref": "IDR-0003",
        "project": "founding3",
        "rec_type": "IDR",
        "persona": "investor",
        "title": "Wire the first subscriber",
        "status": "accepted",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "tags": ["events"],
        "links": [],
        "fields": {"bet": "durable subscriber"},
        "body_md_excluded": True,
    }
    body.update(overrides)
    return body


def _sample_attributes(**overrides: str) -> dict[str, str]:
    """The five String message attributes events.py always sets."""
    attrs = {
        "project": "founding3",
        "rec_type": "IDR",
        "persona": "investor",
        "status": "accepted",
        "event_type": "record.created",
    }
    attrs.update(overrides)
    return attrs


# ----------------------------------------------------------------------
# pending()
# ----------------------------------------------------------------------


def test_pending_returns_parsed_events_with_receipt_handles_and_attributes(
    sqs_client: Any, queue_url: str, event_queue: EventQueue
) -> None:
    """pending() parses the body, keeps message attributes, and attaches a receipt_handle."""
    _publish_raw_event(sqs_client, queue_url, _sample_body(), _sample_attributes())

    events = event_queue.pending()

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "record.created"
    assert event.ref == "IDR-0003"
    assert event.project == "founding3"
    assert event.rec_type == "IDR"
    assert event.tags == ["events"]
    assert event.fields == {"bet": "durable subscriber"}
    assert event.attributes == _sample_attributes()
    assert event.receipt_handle


def test_pending_carries_event_type_specific_extras(
    sqs_client: Any, queue_url: str, event_queue: EventQueue
) -> None:
    """A status_changed event's from/to (not a named field) lands in `extra`."""
    body = _sample_body(event_type="record.status_changed")
    body["from"] = "proposed"
    body["to"] = "accepted"
    _publish_raw_event(
        sqs_client, queue_url, body, _sample_attributes(event_type="record.status_changed")
    )

    events = event_queue.pending()

    assert events[0].extra == {"from": "proposed", "to": "accepted"}


def test_pending_on_empty_queue_returns_empty_list(event_queue: EventQueue) -> None:
    """An empty queue returns [] without raising -- the only case that does."""
    assert event_queue.pending() == []


# ----------------------------------------------------------------------
# ack()
# ----------------------------------------------------------------------


def test_ack_deletes_only_given_handles_others_remain_and_redeliver(
    sqs_client: Any, queue_url: str
) -> None:
    """ack() deletes only the acked handle; the other remains and is
    genuinely redelivered after a real 1-second visibility timeout."""
    sqs_client.set_queue_attributes(QueueUrl=queue_url, Attributes={"VisibilityTimeout": "1"})
    queue = EventQueue(queue_url=queue_url, client=sqs_client)
    _publish_raw_event(sqs_client, queue_url, _sample_body(ref="IDR-0001"), _sample_attributes())
    _publish_raw_event(sqs_client, queue_url, _sample_body(ref="IDR-0002"), _sample_attributes())

    events = queue.pending()
    assert len(events) == 2
    keep = next(e for e in events if e.ref == "IDR-0001")
    to_ack = next(e for e in events if e.ref == "IDR-0002")

    deleted = queue.ack([to_ack.receipt_handle])
    assert deleted == 1

    time.sleep(1.5)  # Real visibility timeout elapsing, not simulated.

    # Only the un-acked message comes back -- proof the acked one was
    # actually deleted (not merely still invisible) and the kept one is
    # genuinely redelivered rather than lost.
    redelivered = queue.pending()
    assert len(redelivered) == 1
    assert redelivered[0].ref == keep.ref


def test_ack_with_no_handles_returns_zero(event_queue: EventQueue) -> None:
    """ack([]) is a no-op that returns 0, not an error."""
    assert event_queue.ack([]) == 0


def test_ack_partial_failure_reports_and_returns_actual_deleted_count(
    sqs_client: Any, queue_url: str, event_queue: EventQueue
) -> None:
    """A bogus receipt handle mixed with a real one still deletes the real
    one, and the returned count reflects only the real deletion -- never
    inflated to match the number of handles requested."""
    _publish_raw_event(sqs_client, queue_url, _sample_body(), _sample_attributes())
    events = event_queue.pending()
    real_handle = events[0].receipt_handle

    deleted = event_queue.ack([real_handle, "not-a-real-receipt-handle"])

    assert deleted == 1


# ----------------------------------------------------------------------
# Missing DR_EVENTS_QUEUE_URL
# ----------------------------------------------------------------------


def test_missing_queue_url_env_var_raises_typed_error_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No queue_url arg, no DR_EVENTS_QUEUE_URL -> actionable error naming the var."""
    monkeypatch.delenv("DR_EVENTS_QUEUE_URL", raising=False)

    with pytest.raises(EventsQueueNotConfiguredError) as exc_info:
        EventQueue()

    assert "DR_EVENTS_QUEUE_URL" in str(exc_info.value)
    assert "DrEventsQueueUrl" in str(exc_info.value)


def test_constructor_arg_overrides_env_var(
    sqs_client: Any, queue_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit queue_url wins over DR_EVENTS_QUEUE_URL."""
    monkeypatch.setenv("DR_EVENTS_QUEUE_URL", "https://example.invalid/should-not-be-used")

    queue = EventQueue(queue_url=queue_url, client=sqs_client)

    assert queue.queue_url == queue_url
