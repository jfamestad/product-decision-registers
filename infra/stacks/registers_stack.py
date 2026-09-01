"""CDK stack for the decision registers DynamoDB table, IAM policy, and the
DynamoDB Stream -> Lambda -> SNS event notification pipeline (IDR-0003,
IDR-0004).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_event_sources,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
    aws_sqs as sqs,
)
from constructs import Construct

# infra/stacks/registers_stack.py -> infra/stacks -> infra -> repo root -> src.
# The Lambda's code asset is the whole `src` directory (not just
# `src/triad_dr`) so that `triad_dr` stays an importable package and the
# handler's `from .logging import configure_logging` relative import works
# unchanged at runtime, the same as it does for every other entry point.
_SRC_DIR = Path(__file__).resolve().parents[2] / "src"

# The exact set of SNS message attributes the events Lambda publishes
# (see events.py's `_message_attributes`). A filter policy key outside
# this set silently matches nothing -- SNS just never delivers -- so
# `_validate_filter_policy` checks every spec's keys against it at synth
# time rather than letting a typo go unnoticed until a subscriber goes
# deaf.
_FILTERABLE_ATTRIBUTES = frozenset({"project", "rec_type", "persona", "status", "event_type"})


@dataclass(frozen=True)
class SubscriberSpec:
    """Declares one independent subscriber to the decision-event topic.

    Each spec gets its own queue, DLQ, topic subscription, IAM grant, and
    CfnOutput -- adding a subscriber is adding an entry to `SUBSCRIBERS`
    below plus `make deploy`. See `_create_events_queue` for why a shared
    queue is wrong for two subscribers with different jobs.

    Attributes:
        name: Slug used in the queue/DLQ physical names and CfnOutput
            description. Not used for CloudFormation logical IDs --
            see `construct_id`.
        construct_id: Explicit CDK construct ID for this subscriber's
            queue (its DLQ construct ID is this plus "Dlq"). Explicit
            rather than derived from `name` so the default subscriber's
            already-deployed logical IDs (`DrEventsQueue` /
            `DrEventsQueueDlq`) can be preserved verbatim -- changing a
            logical ID makes CloudFormation delete the live queue and
            create a new one, dropping any buffered decisions.
        filter_policy: SNS message-attribute filter restricting which
            events this subscriber receives, as {attribute: [allowed
            values]}. None means no filter -- the subscriber sees every
            event the topic publishes. Keys must be a subset of
            `_FILTERABLE_ATTRIBUTES`.
        description: Human-readable purpose, used in the CfnOutput
            description so an operator knows which subscriber a queue
            URL belongs to.
    """

    name: str
    construct_id: str
    filter_policy: dict[str, list[str]] | None
    description: str


# The default catch-all subscriber is ALREADY DEPLOYED under construct IDs
# `DrEventsQueue` / `DrEventsQueueDlq` (physical names
# triad-decision-registers-events-queue[-dlq]). Its construct_id must not
# change -- see the docstring above.
SUBSCRIBERS: tuple[SubscriberSpec, ...] = (
    SubscriberSpec(
        name="default",
        construct_id="DrEventsQueue",
        filter_policy=None,
        description="the default catch-all subscriber (dr_events_pending / dr_events_ack)",
    ),
    # Worked example -- a project-scoped PM worker that only wants status
    # changes on acme-app records. Uncomment and adjust to add a real
    # subscriber; give it its own construct_id, never reuse "DrEventsQueue".
    # SubscriberSpec(
    #     name="acme-app-pm-worker",
    #     construct_id="AcmeAppPmWorkerQueue",
    #     filter_policy={
    #         "project": ["acme-app"],
    #         "event_type": ["record.status_changed"],
    #     },
    #     description="the acme-app PM worker (status changes on acme-app records only)",
    # ),
)


def _validate_filter_policy(spec: SubscriberSpec) -> None:
    """Reject a subscriber spec whose filter policy names an unfilterable attribute.

    A typo'd key (e.g. `"projct"`) doesn't error in SNS -- it just never
    matches, and the subscriber silently receives nothing. That failure
    is invisible without a check, so this runs at synth time instead.

    Args:
        spec: The subscriber spec to validate.

    Raises:
        ValueError: If `spec.filter_policy` names a key outside
            `_FILTERABLE_ATTRIBUTES`.
    """
    if spec.filter_policy is None:
        return
    bad_keys = set(spec.filter_policy) - _FILTERABLE_ATTRIBUTES
    if bad_keys:
        raise ValueError(
            f"SubscriberSpec {spec.name!r} has filter_policy key(s) {sorted(bad_keys)} "
            f"not in the publishable attribute set {sorted(_FILTERABLE_ATTRIBUTES)}. "
            "A filter key outside this set matches nothing and the subscriber goes deaf."
        )


class RegistersStack(cdk.Stack):
    """Stack defining the decision registers table and its event pipeline.

    Resources:
      - DynamoDB table (triad-decision-registers) with on-demand billing,
        PITR enabled, a NEW_AND_OLD_IMAGES stream, RETAIN policy
      - IAM ManagedPolicy granting server access to the table
      - SNS topic that publishes semantic decision-record events
      - Lambda (src/triad_dr/events.py) that derives those events from the
        table's DynamoDB Stream and publishes them to the topic
      - SQS dead-letter queue for stream records the Lambda cannot process
        after retrying, so an unattended failure never silently drops a
        decision event
      - One SQS durable subscriber queue per entry in `SUBSCRIBERS`
        (IDR-0004), each subscribed to the SNS topic with raw message
        delivery, so a consumer that isn't running when a decision lands
        still sees it later -- SNS itself is fan-out only and does not
        replay. Each queue has its own DLQ, catching messages that fail
        redelivery repeatedly. Independent subscribers get independent
        queues rather than sharing one, because two consumers sharing a
        queue split events between them (a work-pool pattern) instead of
        each seeing every event (what a subscriber that must not miss a
        decision needs).
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        """Initialize the RegistersStack.

        Args:
            scope: The construct scope.
            construct_id: The construct ID.
            **kwargs: Additional keyword arguments to pass to the Stack.
        """
        super().__init__(scope, construct_id, **kwargs)

        table = self._create_table()
        policy = self._create_iam_policy(table)
        topic = self._create_events_topic()
        dlq = self._create_events_dlq()
        events_function = self._create_events_function(topic)
        self._create_events_source_mapping(events_function, table, dlq)
        self._grant_events_permissions(events_function, table, topic)

        # Outputs
        cdk.CfnOutput(
            self,
            "TableName",
            value=table.table_name,
            description="Name of the decision registers DynamoDB table",
        )

        cdk.CfnOutput(
            self,
            "TableArn",
            value=table.table_arn,
            description="ARN of the decision registers DynamoDB table",
        )

        cdk.CfnOutput(
            self,
            "ManagedPolicyArn",
            value=policy.managed_policy_arn,
            description=(
                "ARN of the IAM ManagedPolicy granting table access. "
                "Attach this to your SSO permission set for the first deployment."
            ),
        )

        cdk.CfnOutput(
            self,
            "DrEventsTopicArn",
            value=topic.topic_arn,
            description="ARN of the SNS topic publishing decision-record events",
        )

        cdk.CfnOutput(
            self,
            "DrEventsTopicName",
            value=topic.topic_name,
            description="Name of the SNS topic publishing decision-record events",
        )

        # One queue (+ DLQ, subscription, IAM grant, CfnOutput pair) per
        # entry in SUBSCRIBERS. Validate every spec's filter policy before
        # creating anything, so a bad spec fails synth cleanly rather than
        # partially provisioning.
        for spec in SUBSCRIBERS:
            _validate_filter_policy(spec)

        for spec in SUBSCRIBERS:
            queue = self._create_events_queue(topic, spec)
            self._grant_queue_permissions(policy, queue)

            # For the default subscriber (construct_id="DrEventsQueue") this
            # produces the output names "DrEventsQueueUrl" / "DrEventsQueueArn"
            # -- unchanged from before this refactor, since anything already
            # reading those output names must keep working.
            cdk.CfnOutput(
                self,
                f"{spec.construct_id}Url",
                value=queue.queue_url,
                description=(
                    f"URL of the SQS queue durably buffering decision-record "
                    f"events for {spec.description}. Set DR_EVENTS_QUEUE_URL "
                    "to this value for dr_events_pending / dr_events_ack."
                ),
            )

            cdk.CfnOutput(
                self,
                f"{spec.construct_id}Arn",
                value=queue.queue_arn,
                description=(
                    f"ARN of the SQS queue durably buffering decision-record "
                    f"events for {spec.description}"
                ),
            )

    def _create_table(self) -> dynamodb.Table:
        """Create the DynamoDB table for decision registers.

        Returns:
            The created DynamoDB table.
        """
        return dynamodb.Table(
            self,
            "RegistersTable",
            table_name="triad-decision-registers",
            partition_key=dynamodb.Attribute(
                name="pk",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="sk",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            # Old + new images: the events Lambda derives semantic events
            # (e.g. "status went proposed -> accepted") by diffing them --
            # a MODIFY alone doesn't say what changed. See events.py.
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            # RETAIN unconditionally: losing decision records to a cdk destroy is unacceptable.
            # The table costs nothing to keep.
        )

    def _create_iam_policy(self, table: dynamodb.Table) -> iam.ManagedPolicy:
        """Create an IAM ManagedPolicy granting server access to the table.

        Args:
            table: The DynamoDB table to grant access to.

        Returns:
            The created IAM ManagedPolicy.
        """
        return iam.ManagedPolicy(
            self,
            "RegistersServerPolicy",
            statements=[
                iam.PolicyStatement(
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:Query",
                        "dynamodb:Scan",
                        "dynamodb:DescribeTable",
                        "dynamodb:BatchGetItem",
                        "dynamodb:TransactWriteItems",
                        "dynamodb:TransactGetItems",
                    ],
                    resources=[table.table_arn],
                    effect=iam.Effect.ALLOW,
                )
            ],
        )

    def _create_events_topic(self) -> sns.Topic:
        """Create the SNS topic decision-record events are published to.

        Returns:
            The created SNS topic.
        """
        return sns.Topic(
            self,
            "DrEventsTopic",
            display_name="Decision Register Events",
        )

    def _create_events_dlq(self) -> sqs.Queue:
        """Create the dead-letter queue for stream records the events
        Lambda cannot process after exhausting its retries.

        An unattended pipeline that silently drops a decision event on
        failure would violate this bet's "no decision lost" criterion --
        this queue is where those records land instead, for inspection
        and manual replay.

        Returns:
            The created SQS queue.
        """
        return sqs.Queue(
            self,
            "DrEventsDlq",
            queue_name="triad-decision-registers-events-dlq",
            retention_period=cdk.Duration.days(14),
        )

    def _create_events_function(self, topic: sns.Topic) -> _lambda.Function:
        """Create the Lambda that derives semantic events from the stream
        and publishes them to `topic`.

        Args:
            topic: The SNS topic to publish to (wired in via env var).

        Returns:
            The created Lambda function.
        """
        return _lambda.Function(
            self,
            "DrEventsFunction",
            description=(
                "Derives record.created / record.status_changed / "
                "review.recorded events from the registers table's "
                "DynamoDB Stream and publishes them to SNS."
            ),
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="events.handler",
            code=_lambda.Code.from_asset(
                # Ship ONLY events.py. Two reasons, both load-bearing:
                #   1. src/triad_dr/logging.py would SHADOW the stdlib logging
                #      module for every import in the Lambda runtime, because
                #      the asset root is on sys.path.
                #   2. The rest of the package imports structlog, which the
                #      Python 3.12 runtime does not ship and this plain-file-copy
                #      asset does not install. cdk synth never imports the
                #      handler, so that failure would only appear at cold start.
                # Handler is "events.handler", not "triad_dr.events.handler":
                # importing it as part of the package would run __init__.py,
                # which pins structlog.
                str(_SRC_DIR / "triad_dr"),
                exclude=["*", "!events.py"],
            ),
            timeout=cdk.Duration.seconds(30),
            environment={"DR_EVENTS_TOPIC_ARN": topic.topic_arn},
        )

    def _create_events_source_mapping(
        self,
        events_function: _lambda.Function,
        table: dynamodb.Table,
        dlq: sqs.Queue,
    ) -> None:
        """Wire the table's DynamoDB Stream to `events_function`.

        Args:
            events_function: The Lambda to invoke per stream batch.
            table: The streamed table.
            dlq: Where batches that exhaust their retries land, so a
                failure is captured rather than silently dropped.
        """
        events_function.add_event_source(
            lambda_event_sources.DynamoEventSource(
                table,
                starting_position=_lambda.StartingPosition.TRIM_HORIZON,
                batch_size=10,
                bisect_batch_on_error=True,
                retry_attempts=2,
                on_failure=lambda_event_sources.SqsDlq(dlq),
            )
        )

    def _grant_events_permissions(
        self,
        events_function: _lambda.Function,
        table: dynamodb.Table,
        topic: sns.Topic,
    ) -> None:
        """Grant the events Lambda stream-read on the table and publish on the topic.

        Args:
            events_function: The Lambda to grant permissions to.
            table: The streamed table.
            topic: The topic the Lambda publishes to.
        """
        table.grant_stream_read(events_function)
        topic.grant_publish(events_function)

    def _create_events_queue(self, topic: sns.Topic, spec: SubscriberSpec) -> sqs.Queue:
        """Create one subscriber's durable SQS queue and subscribe it to `topic`.

        SNS is fan-out, not buffering: a subscriber that is not running
        when a decision lands never sees that notification -- SNS does not
        replay. This queue sits between the topic and whichever process
        drains it (see src/triad_dr/events_queue.py and the
        dr_events_pending / dr_events_ack MCP tools), so a decision made
        overnight is still there in the morning.

        Subscribed with raw_message_delivery=True: the SQS message body
        becomes the event's JSON payload directly (no SNS envelope to
        unwrap first), and the publisher's String message attributes
        (project/rec_type/persona/status/event_type -- see events.py's
        `_message_attributes`) survive unchanged as SQS message
        attributes.

        Each subscriber gets its own queue rather than sharing one:
        two consumers on one queue split events between them (correct
        for a work pool), not each receiving every event (what a
        subscriber that must not miss a decision needs). A subscriber
        that wants a narrower slice sets `spec.filter_policy` rather
        than receiving everything and discarding client-side.

        The default subscriber's queue/DLQ physical names
        (triad-decision-registers-events-queue[-dlq]) are preserved
        unchanged -- an SQS QueueName is immutable, so changing it, like
        changing the construct ID, would replace the live queue.

        Args:
            topic: The SNS topic to subscribe to.
            spec: The subscriber spec driving construct ID, physical
                naming, and filter policy.

        Returns:
            The created SQS queue.
        """
        name_suffix = "" if spec.name == "default" else f"-{spec.name}"
        dlq = sqs.Queue(
            self,
            f"{spec.construct_id}Dlq",
            queue_name=f"triad-decision-registers-events-queue{name_suffix}-dlq",
            retention_period=cdk.Duration.days(14),
        )
        queue = sqs.Queue(
            self,
            spec.construct_id,
            queue_name=f"triad-decision-registers-events-queue{name_suffix}",
            visibility_timeout=cdk.Duration.minutes(5),
            retention_period=cdk.Duration.days(14),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )
        filter_policy = None
        if spec.filter_policy is not None:
            filter_policy = {
                key: sns.SubscriptionFilter.string_filter(allowlist=values)
                for key, values in spec.filter_policy.items()
            }
        topic.add_subscription(
            sns_subscriptions.SqsSubscription(
                queue,
                raw_message_delivery=True,
                filter_policy=filter_policy,
            )
        )
        return queue

    def _grant_queue_permissions(self, policy: iam.ManagedPolicy, queue: sqs.Queue) -> None:
        """Grant the server's existing managed policy receive/delete access on `queue`.

        The same ManagedPolicy already attached to the operator's SSO
        permission set for table access (`_create_iam_policy`) picks up
        queue access too, so draining the queue (dr_events_pending /
        dr_events_ack) works under the same identity as everything else
        this server does -- no separate role or policy to attach.

        Args:
            policy: The existing RegistersServerPolicy managed policy.
            queue: The subscriber queue to grant access to.
        """
        policy.add_statements(
            iam.PolicyStatement(
                actions=[
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:DeleteMessageBatch",
                    "sqs:GetQueueAttributes",
                ],
                resources=[queue.queue_arn],
                effect=iam.Effect.ALLOW,
            )
        )
