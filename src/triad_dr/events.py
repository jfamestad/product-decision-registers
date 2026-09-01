"""DynamoDB Streams -> SNS bridge for decision record events.

Every write to the registers table touches up to three items: the record
itself (``REC#<TYPE>#<seq>``), its per-project/per-type sequence counter
(``CTR#<TYPE>``), and the ``META#PROJECTS`` registry entry (which bumps a
freshness ``version`` on every write -- see `store.py`'s
`_upsert_project_registry`). A raw DynamoDB Stream on this table therefore
fires up to three stream records per decision, two of which are pure
bookkeeping and neither of which is itself a usable event: a MODIFY tells
you an item changed, not *what* changed semantically (e.g. that a bet was
accepted).

This module is the Lambda handler (see
`infra/stacks/registers_stack.py`, `_create_events_function`) that sits
between the stream and an SNS topic. It:

1. Filters the stream down to writes on ``REC#`` items only -- counters,
   escalations, and the project registry are silently ignored.
2. Derives exactly three semantic event types by comparing INSERT's
   NewImage, or MODIFY's OldImage/NewImage: ``record.created``,
   ``record.status_changed``, ``review.recorded``. A MODIFY that is
   neither (a content edit, or `updated_at`-only churn) publishes
   nothing -- that is deliberate, not a gap: a patch to a draft nobody
   has read is not news.
3. Publishes a JSON summary (not the full record -- `body_md` is
   deliberately excluded) to SNS, with `project` / `rec_type` /
   `persona` / `status` / `event_type` as String message attributes so
   subscribers can filter without parsing the body.

Escalations (``ESC#`` items) are out of scope for this bet (IDR-0003) and
are silently ignored, same as counters and the project registry.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any, NamedTuple

import logging as _stdlib_logging

import boto3
from boto3.dynamodb.types import TypeDeserializer

# Stdlib logging, deliberately, NOT the structlog this package pins elsewhere.
# This module runs as a Lambda, where the "stdout is the MCP protocol channel"
# constraint does not apply and CloudWatch captures stdout/stderr either way.
# More importantly it must import with ZERO third-party dependencies beyond
# boto3 (which the Python 3.12 runtime ships): the deployment asset is a plain
# file copy with no pip install, so a structlog import would ImportError at
# cold start -- invisible to `cdk synth`, which never imports the handler.
# For the same reason this module must not import anything from the triad_dr
# package: it is deployed with handler "events.handler", not
# "triad_dr.events.handler", so the package __init__ (which does pin structlog)
# never runs.
logger = _stdlib_logging.getLogger(__name__)
logger.setLevel(_stdlib_logging.INFO)

_deserializer = TypeDeserializer()

_REC_PREFIX = "REC#"
_PUBLISHABLE_EVENT_NAMES = frozenset({"INSERT", "MODIFY"})


class _DerivedEvent(NamedTuple):
    """One semantic event derived from a single DynamoDB stream record.

    Attributes:
        event_type: One of "record.created", "record.status_changed",
            "review.recorded".
        attributes: SNS ``MessageAttributes``, ready to pass to
            ``sns_client.publish``.
        body: The JSON-serializable message body.
    """

    event_type: str
    attributes: dict[str, dict[str, str]]
    body: dict[str, Any]


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Turn a DynamoDB Streams batch into semantic SNS notifications.

    Runs unattended (triggered by the event source mapping in
    `registers_stack.py`), so a single malformed or unexpected stream
    record must never fail the whole batch -- it is logged to stderr via
    the log and skipped, and every other record in the batch is still
    processed.

    Args:
        event: A DynamoDB Streams event as delivered by the Lambda event
            source mapping. ``event["Records"]`` is a list of stream
            records in DynamoDB wire format (``{"S": "..."}``-style
            AttributeValues), not the resource-layer Python types the
            rest of `triad_dr` works with.
        context: The Lambda context object. Unused.

    Returns:
        A dict with `published`, `skipped`, and `failed` counts for this
        batch.
    """
    topic_arn = os.environ.get("DR_EVENTS_TOPIC_ARN")
    sns_client = None
    if topic_arn:
        sns_client = boto3.client("sns")
    else:
        logger.warning("dr_events.topic_arn_not_configured note=%r", "DR_EVENTS_TOPIC_ARN is unset; skipping publish for this batch.")

    published = 0
    skipped = 0
    failed = 0

    for record in event.get("Records", []):
        try:
            derived, record_skipped = _derive_events(record)
        except Exception:
            # Per-record boundary, deliberately broad: this is the one thing
            # standing between one bad stream record (malformed keys, an
            # AttributeValue the TypeDeserializer rejects, a required field
            # missing from an image) and the whole batch failing. Every other
            # record in the batch must still get processed.
            logger.exception("dr_events.record_processing_failed raw_record=%r", record)
            failed += 1
            continue

        skipped += record_skipped
        for derived_event in derived:
            if sns_client is None:
                skipped += 1
                continue
            sns_client.publish(
                TopicArn=topic_arn,
                Message=json.dumps(derived_event.body),
                MessageAttributes=derived_event.attributes,
            )
            published += 1

    logger.info("dr_events.batch_complete published=%r skipped=%r failed=%r", published, skipped, failed)
    return {"published": published, "skipped": skipped, "failed": failed}


def _derive_events(record: dict[str, Any]) -> tuple[list[_DerivedEvent], int]:
    """Derive zero or more semantic events from one raw stream record.

    Args:
        record: One entry from ``event["Records"]``, DynamoDB Streams wire
            format.

    Returns:
        A tuple of (derived events, a skip count). The skip count is 1 if
        this record was filtered out (not a ``REC#`` item, a REMOVE, or a
        MODIFY with no semantic change) and 0 otherwise -- it is never
        more than 1 regardless of how many derived events come back,
        since a filtered-out record produces no events at all.

    Raises:
        KeyError: The record is missing an expected structural field
            (``dynamodb``, ``Keys``, ``sk``, ``NewImage``, ...).
        Exception: Whatever `TypeDeserializer` raises on an unrecognized
            or malformed AttributeValue.
    """
    event_name = record["eventName"]
    dynamodb_data = record["dynamodb"]
    sk = _deserializer.deserialize(dynamodb_data["Keys"]["sk"])

    if not sk.startswith(_REC_PREFIX):
        logger.debug("dr_events.skipped_non_record_item sk=%r event_name=%r", sk, event_name)
        return [], 1

    if event_name not in _PUBLISHABLE_EVENT_NAMES:
        logger.debug("dr_events.skipped_event_name sk=%r event_name=%r", sk, event_name)
        return [], 1

    if event_name == "INSERT":
        new_image = _deserialize_image(dynamodb_data["NewImage"])
        return [_created_event(new_image)], 0

    # MODIFY: a single write can legitimately be both a status change and a
    # review (e.g. dr_set_status and dr_review landing in the same logical
    # edit), so both are checked and both are emitted when both fire.
    old_image = _deserialize_image(dynamodb_data["OldImage"])
    new_image = _deserialize_image(dynamodb_data["NewImage"])

    events: list[_DerivedEvent] = []
    status_event = _status_changed_event(old_image, new_image)
    if status_event is not None:
        events.append(status_event)
    review_event = _review_recorded_event(old_image, new_image)
    if review_event is not None:
        events.append(review_event)

    if not events:
        # A content edit, or updated_at-only churn: the highest-volume,
        # lowest-signal event this stream can produce. Deliberately not news.
        logger.debug("dr_events.skipped_no_semantic_change sk=%r", sk)
        return [], 1
    return events, 0


def _deserialize_image(image: dict[str, Any]) -> dict[str, Any]:
    """Deserialize a DynamoDB Streams image from wire format to native Python.

    Args:
        image: A stream image (``NewImage`` or ``OldImage``), e.g.
            ``{"status": {"S": "accepted"}, "seq": {"N": "7"}}``.

    Returns:
        The same item with every value converted to its native Python
        type (str, Decimal, bool, list, dict, None) via
        `boto3.dynamodb.types.TypeDeserializer`.
    """
    return {key: _deserializer.deserialize(value) for key, value in image.items()}


def _to_native(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals to int/float for JSON output.

    `TypeDeserializer` turns every DynamoDB Number into a `Decimal`, which
    `json.dumps` cannot serialize on its own.

    Args:
        value: Any value from a deserialized stream image.

    Returns:
        The same structure with every Decimal replaced by an int (if
        whole) or a float.
    """
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: _to_native(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_native(item) for item in value]
    return value


def _summary(image: dict[str, Any], event_type: str) -> dict[str, Any]:
    """Build the message-body summary common to all three event types.

    Deliberately excludes `body_md` -- it is long prose subscribers rarely
    need inline, and one that does can call dr_get. `body_md_excluded`
    marks the omission as by design rather than a missing field.

    Args:
        image: A deserialized NewImage (native Python types).
        event_type: One of "record.created", "record.status_changed",
            "review.recorded".

    Returns:
        A JSON-serializable summary dict.
    """
    return {
        "event_type": event_type,
        "ref": image.get("ref"),
        "project": image.get("project"),
        "rec_type": image.get("rec_type"),
        "persona": image.get("persona"),
        "title": image.get("title"),
        "status": image.get("status"),
        "created_at": image.get("created_at"),
        "updated_at": image.get("updated_at"),
        "tags": _to_native(image.get("tags", [])),
        "links": _to_native(image.get("links", [])),
        "fields": _to_native(image.get("fields", {})),
        "body_md_excluded": True,
    }


def _message_attributes(body: dict[str, Any], event_type: str) -> dict[str, dict[str, str]]:
    """Build the SNS String message attributes subscribers filter on.

    Args:
        body: A message body already built by `_summary` (or a variant of
            it) -- carries project/rec_type/persona/status.
        event_type: The derived event type.

    Returns:
        SNS ``MessageAttributes``, with exactly project, rec_type,
        persona, status, and event_type -- every value a String.
    """

    def _attr(value: Any) -> dict[str, str]:
        return {"DataType": "String", "StringValue": "" if value is None else str(value)}

    return {
        "project": _attr(body.get("project")),
        "rec_type": _attr(body.get("rec_type")),
        "persona": _attr(body.get("persona")),
        "status": _attr(body.get("status")),
        "event_type": _attr(event_type),
    }


def _created_event(new_image: dict[str, Any]) -> _DerivedEvent:
    """Build the `record.created` event for an INSERT of a `REC#` item.

    Args:
        new_image: Deserialized NewImage.

    Returns:
        The derived event.
    """
    body = _summary(new_image, "record.created")
    return _DerivedEvent("record.created", _message_attributes(body, "record.created"), body)


def _status_changed_event(
    old_image: dict[str, Any], new_image: dict[str, Any]
) -> _DerivedEvent | None:
    """Build the `record.status_changed` event, if `status` differs.

    Args:
        old_image: Deserialized OldImage.
        new_image: Deserialized NewImage.

    Returns:
        The derived event carrying `from`/`to`, or None if `status` is
        unchanged between the two images.
    """
    old_status = old_image.get("status")
    new_status = new_image.get("status")
    if old_status == new_status:
        return None
    body = _summary(new_image, "record.status_changed")
    body["from"] = old_status
    body["to"] = new_status
    return _DerivedEvent(
        "record.status_changed", _message_attributes(body, "record.status_changed"), body
    )


def _review_recorded_event(
    old_image: dict[str, Any], new_image: dict[str, Any]
) -> _DerivedEvent | None:
    """Build the `review.recorded` event, if `reviews` grew.

    Per `store.py`'s `review()`, reviews are only ever appended, so the
    newest review is the last element of the new image's `reviews` list.

    Args:
        old_image: Deserialized OldImage.
        new_image: Deserialized NewImage.

    Returns:
        The derived event carrying the newest review's `verdict` and
        `cause` (`cause` is None when absent), or None if `reviews` did
        not grow.
    """
    old_reviews = old_image.get("reviews") or []
    new_reviews = new_image.get("reviews") or []
    if len(new_reviews) <= len(old_reviews):
        return None
    newest = new_reviews[-1]
    body = _summary(new_image, "review.recorded")
    body["verdict"] = newest.get("verdict")
    body["cause"] = newest.get("cause")
    return _DerivedEvent("review.recorded", _message_attributes(body, "review.recorded"), body)
