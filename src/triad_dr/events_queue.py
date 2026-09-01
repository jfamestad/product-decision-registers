"""SQS access for the durable decision-event subscriber queue (IDR-0003).

`infra/stacks/registers_stack.py` subscribes an SQS queue (`DrEventsQueue`)
to the `DrEventsTopic` SNS topic with raw message delivery, so a consumer
that is not running when a decision lands still sees it later -- SNS itself
is fan-out only and never replays a notification to a subscriber that
missed it. This module is the pure-Python client for that queue; the MCP
tool layer (`dr_events_pending` / `dr_events_ack` in `server.py`) is a thin
adapter over it, same relationship `store.py` has to the record tools.

**Receive and ack are deliberately two separate calls, never one.** SQS is
at-least-once delivery: a message being receivable does not mean it is safe
to delete. If `pending()` deleted on receive, any event whose downstream
application failed -- the caller crashed mid-apply, raised an exception,
was killed -- would vanish from the queue forever with nothing left to say
it ever existed. Instead, `pending()` only *receives*: SQS makes the
message invisible to other receivers for the queue's visibility timeout
(currently ~5 minutes, see the CDK stack) and nothing more happens until
the caller explicitly calls `ack()`. An event whose apply raised is simply
never acked; it becomes visible again once the timeout elapses and is
redelivered on a later `pending()` call. That redelivery is the intended
behavior, not a bug -- it is what "at least once" actually promises. The
mirror-image failure mode is the one this shape is built to prevent: acking
an event that was never actually applied is unconditional and irreversible,
and silently loses whatever decision that event carried.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import boto3
import structlog
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
from pydantic import BaseModel, Field

from .errors import CredentialsError, EventsQueueNotConfiguredError

logger = structlog.get_logger(__name__)

_ENV_VAR = "DR_EVENTS_QUEUE_URL"

# SQS's own hard cap on messages per ReceiveMessage/DeleteMessageBatch call.
_SQS_BATCH_LIMIT = 10

# SQS ClientError codes meaning "this queue URL is not a real queue" --
# distinct from a credentials failure, and worth its own actionable message
# naming the CDK output the URL should have come from.
_QUEUE_MISSING_CODES = frozenset(
    {"AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist"}
)

# Keys `events.py`'s `_summary()` always writes to the message body (see
# events.py's module docstring and `_summary`/`_created_event`/
# `_status_changed_event`/`_review_recorded_event`). Everything else found
# in a message body is event-type-specific (`from`/`to` on
# record.status_changed, `verdict`/`cause` on review.recorded) and is kept
# in `PendingEvent.extra` instead of being modeled as named fields here --
# this module has no reason to track events.py's per-type shape forever.
_KNOWN_BODY_KEYS = frozenset(
    {
        "event_type",
        "ref",
        "project",
        "rec_type",
        "persona",
        "title",
        "status",
        "created_at",
        "updated_at",
        "tags",
        "links",
        "fields",
        "body_md_excluded",
    }
)


class PendingEvent(BaseModel):
    """One decision-record event received from the queue, not yet acknowledged.

    Attributes:
        event_type: One of "record.created", "record.status_changed",
            "review.recorded" (see events.py).
        ref: The record's human ref, e.g. "IDR-0003".
        project: Project slug.
        rec_type: One of "ADR", "IDR", "MDR".
        persona: The record's owning persona.
        title: Record title.
        status: The record's status as of this event.
        created_at: The record's creation timestamp (ISO-8601).
        updated_at: The record's last-update timestamp (ISO-8601).
        tags: The record's tags at the time of this event.
        links: The record's links at the time of this event.
        fields: The record's type-specific fields at the time of this event.
        body_md_excluded: Always True -- events.py deliberately never
            includes body_md in the published message; see its `_summary`.
        extra: Event-type-specific keys present in the raw message body but
            not modeled above -- e.g. "from"/"to" on record.status_changed,
            "verdict"/"cause" on review.recorded.
        attributes: The SNS message attributes, delivered as SQS message
            attributes because the subscription uses raw message delivery
            -- project, rec_type, persona, status, event_type, each a
            plain string.
        receipt_handle: Pass this (and only this) to `EventQueue.ack` once
            the event has actually been applied. Changes on redelivery,
            even for the same underlying event -- do not cache it keyed by
            ref/event_type.
    """

    event_type: str
    ref: str | None = None
    project: str | None = None
    rec_type: str | None = None
    persona: str | None = None
    title: str | None = None
    status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)
    body_md_excluded: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)
    attributes: dict[str, str] = Field(default_factory=dict)
    receipt_handle: str


def _parse_message(message: dict[str, Any]) -> PendingEvent:
    """Parse one raw SQS message (raw SNS delivery) into a `PendingEvent`.

    Args:
        message: One entry from `receive_message`'s ``Messages`` list.

    Returns:
        The parsed `PendingEvent`.
    """
    body = json.loads(message["Body"])
    known = {key: value for key, value in body.items() if key in _KNOWN_BODY_KEYS}
    extra = {key: value for key, value in body.items() if key not in _KNOWN_BODY_KEYS}
    attributes = {
        name: attr.get("StringValue", "")
        for name, attr in message.get("MessageAttributes", {}).items()
    }
    return PendingEvent(
        **known,
        extra=extra,
        attributes=attributes,
        receipt_handle=message["ReceiptHandle"],
    )


class EventQueue:
    """SQS-backed client for the durable decision-event subscriber queue.

    Pure Python, no MCP awareness -- the MCP tool layer (`dr_events_pending`
    / `dr_events_ack` in `server.py`) is a separate, thin consumer of this
    class, the same relationship `store.py` has to the record tools.
    Dependency injection only: no global mutable state, no implicit
    singleton client.

    Attributes:
        queue_url: The SQS queue URL this instance operates against.
    """

    def __init__(self, queue_url: str | None = None, client: Any | None = None) -> None:
        """Initialize the event queue client.

        Args:
            queue_url: The SQS queue URL to operate against. Falls back to
                the ``DR_EVENTS_QUEUE_URL`` environment variable when
                omitted.
            client: A boto3 SQS client, injected for testing (moto) or
                custom session/region configuration. Defaults to a new
                client built from the default boto3 session/region chain.

        Raises:
            EventsQueueNotConfiguredError: Neither `queue_url` nor
                ``DR_EVENTS_QUEUE_URL`` is set. Raised here, at
                construction, rather than deferred to the first call --
                an unset queue must never be indistinguishable from an
                empty one.
        """
        resolved = queue_url or os.environ.get(_ENV_VAR)
        if not resolved:
            raise EventsQueueNotConfiguredError(_ENV_VAR)
        self.queue_url = resolved
        self._client = client if client is not None else boto3.client("sqs")

    def _call(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        """Invoke a boto3 SQS call, translating infra errors into actionable ones.

        Mirrors `store.py`'s `Store._call`.

        Args:
            fn: A bound boto3 method (e.g. ``self._client.receive_message``).
            **kwargs: Keyword arguments forwarded to `fn`.

        Returns:
            Whatever `fn` returns.

        Raises:
            CredentialsError: AWS credentials are missing or an SSO
                session has expired.
            EventsQueueNotConfiguredError: The configured queue URL is not
                a queue SQS recognizes.
        """
        try:
            return fn(**kwargs)
        except (
            UnauthorizedSSOTokenError,
            SSOTokenLoadError,
            TokenRetrievalError,
            NoCredentialsError,
        ) as exc:
            raise CredentialsError(os.environ.get("AWS_PROFILE")) from exc
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _QUEUE_MISSING_CODES:
                raise EventsQueueNotConfiguredError(_ENV_VAR, queue_url=self.queue_url) from exc
            raise

    def pending(self, max_messages: int = 10) -> list[PendingEvent]:
        """Receive pending events from the queue, WITHOUT deleting them.

        Uses long polling (``WaitTimeSeconds=2``) so a currently-empty
        queue does not spin. See the module docstring for why this never
        deletes on the caller's behalf -- call `ack()` only after each
        returned event has actually been applied.

        Args:
            max_messages: Maximum number of events to receive. SQS caps a
                single `ReceiveMessage` call at 10; a larger value makes
                additional polls (stopping early once the queue reports
                fewer messages available than requested, rather than
                paying for a further 2-second wait that would likely
                return nothing).

        Returns:
            The received events, parsed. Order is best-effort (SQS makes
            no strong ordering guarantee), not authoritative. An empty
            queue returns `[]` -- the ONLY case that does; every failure
            to reach the queue at all raises instead of returning `[]`, so
            an empty result is never ambiguous with a failure.

        Raises:
            CredentialsError: AWS credentials are missing or expired.
            EventsQueueNotConfiguredError: The queue could not be reached.
        """
        events: list[PendingEvent] = []
        remaining = max_messages
        while remaining > 0:
            batch_size = min(remaining, _SQS_BATCH_LIMIT)
            response = self._call(
                self._client.receive_message,
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=batch_size,
                WaitTimeSeconds=2,
                MessageAttributeNames=["All"],
            )
            messages = response.get("Messages", [])
            events.extend(_parse_message(message) for message in messages)
            remaining -= len(messages)
            if len(messages) < batch_size:
                # Queue had fewer messages available than requested this
                # round; another call would just pay out a further 2s
                # long-poll wait for nothing.
                break
        return events

    def ack(self, receipt_handles: list[str]) -> int:
        """Permanently delete acknowledged messages from the queue.

        Args:
            receipt_handles: Receipt handles of events that have already
                been successfully applied -- see the module docstring for
                why this must never be called before that.

        Returns:
            The number of messages actually deleted. Can be less than
            ``len(receipt_handles)`` on partial failure (an expired
            handle, or a message already deleted by a prior call) --
            never inflated to match the input length. Partial failures
            are logged (`events_queue.ack_partial_failure`) rather than
            silently swallowed; whatever wasn't deleted remains on the
            queue and will be redelivered.

        Raises:
            CredentialsError: AWS credentials are missing or expired.
            EventsQueueNotConfiguredError: The queue could not be reached.
        """
        if not receipt_handles:
            return 0

        deleted = 0
        for start in range(0, len(receipt_handles), _SQS_BATCH_LIMIT):
            chunk = receipt_handles[start : start + _SQS_BATCH_LIMIT]
            entries = [
                {"Id": str(index), "ReceiptHandle": handle}
                for index, handle in enumerate(chunk)
            ]
            response = self._call(
                self._client.delete_message_batch,
                QueueUrl=self.queue_url,
                Entries=entries,
            )
            successful = response.get("Successful", [])
            failed = response.get("Failed", [])
            deleted += len(successful)
            if failed:
                logger.warning(
                    "events_queue.ack_partial_failure",
                    requested_count=len(chunk),
                    failed_count=len(failed),
                    failures=[
                        {
                            "id": failure.get("Id"),
                            "code": failure.get("Code"),
                            "message": failure.get("Message"),
                        }
                        for failure in failed
                    ],
                )
        return deleted
