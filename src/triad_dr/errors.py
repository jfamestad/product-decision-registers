"""Exception hierarchy for the decision registers store.

Every exception here carries an actionable, human-readable message. Callers
(the MCP tool layer) are expected to surface `str(exc)` directly rather than
a raw boto3/botocore traceback — see spec section 3 ("Credentials") and
section 6 ("complain, don't guess").
"""

from __future__ import annotations


class TriadDRError(Exception):
    """Base class for all decision-register store errors."""


class NoProjectError(TriadDRError):
    """Raised when no project can be resolved for a project-scoped call.

    Resolution order is: explicit `project` argument, then `DR_PROJECT`
    env var, then this error. Per spec section 3, the message must explain
    both remediation paths and list known projects so the caller isn't
    guessing.
    """

    def __init__(self, known_projects: list[str]) -> None:
        """Build the actionable no-project message.

        Args:
            known_projects: Project slugs currently in the registry, used
                to give the caller concrete options.
        """
        projects_str = ", ".join(known_projects) if known_projects else "(none yet)"
        message = (
            "No project set. Set DR_PROJECT in this server's env "
            "(recommended: per-repo .mcp.json) or pass project=\"...\" "
            f"explicitly. Known projects: {projects_str}."
        )
        super().__init__(message)
        self.known_projects = known_projects


class TableMissingError(TriadDRError):
    """Raised when the DynamoDB table does not exist.

    Per spec section 6, the server never auto-creates the table; it tells
    the operator to run `make deploy`.
    """

    def __init__(self, table_name: str) -> None:
        """Build the actionable table-missing message.

        Args:
            table_name: The DynamoDB table name that could not be found.
        """
        message = (
            f'DynamoDB table "{table_name}" does not exist. Run `make deploy` '
            "to create it, then retry."
        )
        super().__init__(message)
        self.table_name = table_name


class CredentialsError(TriadDRError):
    """Raised when AWS credentials are missing or expired.

    Wraps botocore's UnauthorizedSSOTokenError / SSOTokenLoadError /
    TokenRetrievalError / NoCredentialsError so callers never see a raw
    boto3 traceback. Names the actual AWS_PROFILE value when set.
    """

    def __init__(self, profile: str | None) -> None:
        """Build the actionable credentials message.

        Args:
            profile: The AWS_PROFILE env var value, if set, so the
                remediation command is copy-pasteable.
        """
        profile_arg = profile if profile else "<profile>"
        message = (
            "AWS credentials are missing or expired (SSO session likely "
            f"expired) — run: aws sso login --profile {profile_arg}"
        )
        super().__init__(message)
        self.profile = profile


class InvalidTransitionError(TriadDRError):
    """Raised when a status transition is not allowed by the lifecycle."""

    def __init__(self, current: str, target: str, allowed: set[str]) -> None:
        """Build the actionable invalid-transition message.

        Args:
            current: The record's current status.
            target: The requested target status.
            allowed: The set of statuses `current` may legally move to.
        """
        allowed_str = ", ".join(sorted(allowed)) if allowed else "(none — terminal status)"
        message = (
            f'Cannot transition status from "{current}" to "{target}". '
            f"Allowed next states from \"{current}\": {allowed_str}."
        )
        super().__init__(message)
        self.current = current
        self.target = target


class InvalidRefError(TriadDRError):
    """Raised when a ref does not match the `<TYPE>-<seq04>` format."""

    def __init__(self, ref: str) -> None:
        """Build the actionable invalid-ref message.

        Args:
            ref: The malformed ref string.
        """
        message = (
            f'Invalid ref "{ref}". Refs must match <TYPE>-<seq04>, e.g. '
            '"ADR-0007", "IDR-0001", "MDR-0003".'
        )
        super().__init__(message)
        self.ref = ref


class ProtectedAttributeError(TriadDRError):
    """Raised when `dr_update` is asked to patch a protected attribute."""

    def __init__(self, attribute: str, guidance: str) -> None:
        """Build the actionable protected-attribute message.

        Args:
            attribute: The protected attribute name that was rejected.
            guidance: A clause explaining why it's protected and, where one
                exists, which dedicated tool to use instead (e.g. "it is
                machine-written. Use dr_set_status to change status.").
        """
        message = f'"{attribute}" is not patchable via dr_update — {guidance}'
        super().__init__(message)
        self.attribute = attribute
        self.guidance = guidance


class RecordNotFoundError(TriadDRError):
    """Raised when a ref or id does not resolve to an existing record."""

    def __init__(self, identifier: str) -> None:
        """Build the actionable record-not-found message.

        Args:
            identifier: The ref or id that could not be found.
        """
        message = f'No record found for "{identifier}".'
        super().__init__(message)
        self.identifier = identifier


class EscalationNotFoundError(TriadDRError):
    """Raised when an escalation ref does not resolve to an existing item."""

    def __init__(self, ref: str) -> None:
        """Build the actionable escalation-not-found message.

        Args:
            ref: The escalation ref that could not be found.
        """
        message = f'No escalation found for "{ref}".'
        super().__init__(message)
        self.ref = ref


class EscalationAlreadyResolvedError(TriadDRError):
    """Raised when `dr_answer` or `dr_withdraw` targets an escalation whose
    status isn't "open".

    An escalation resolves to "answered" or "withdrawn" exactly once,
    whichever comes first. The message names the prior resolution and when
    it was made, so the caller doesn't have to fetch the escalation
    separately to find out why the call was rejected.
    """

    def __init__(
        self,
        ref: str,
        status: str,
        resolution: str | None,
        answered_at: str | None,
        action: str = "answered again",
    ) -> None:
        """Build the actionable already-resolved message.

        Args:
            ref: The escalation ref that was targeted.
            status: The escalation's current status (anything but "open").
            resolution: The prior resolution text, when the status is
                "answered".
            answered_at: The ISO-8601 timestamp the prior resolution was
                recorded, when the status is "answered".
            action: The verb phrase naming what the rejected call tried to
                do -- "answered again" (default, from `dr_answer`) or
                "withdrawn" (from `dr_withdraw`) -- so the message names
                the attempted action, not just the escalation's status.
        """
        if resolution is not None and answered_at is not None:
            detail = f'It was answered on {answered_at}: "{resolution}".'
        else:
            detail = f'Its current status is "{status}".'
        message = f"{ref} is already {status} and cannot be {action}. {detail}"
        super().__init__(message)
        self.ref = ref
        self.status = status
        self.resolution = resolution
        self.answered_at = answered_at


class EventsQueueNotConfiguredError(TriadDRError):
    """Raised when the events subscriber queue cannot be resolved or reached.

    Covers two distinct failures with the same actionable shape (per the
    same "complain, don't guess" contract as `NoProjectError` /
    `TableMissingError`): the `DR_EVENTS_QUEUE_URL` env var being unset,
    and a configured URL that SQS itself does not recognize (e.g. the
    stack was redeployed and the queue's URL changed, or the wrong
    region/account is in play). An empty `EventQueue.pending()` result
    looks identical to "no events waiting" -- this is why both cases are a
    hard error instead of a quiet empty list.
    """

    def __init__(self, env_var: str, queue_url: str | None = None) -> None:
        """Build the actionable events-queue message.

        Args:
            env_var: The environment variable name (``DR_EVENTS_QUEUE_URL``).
            queue_url: The queue URL that failed to resolve, when one was
                given but SQS rejected it. ``None`` when the env var itself
                was never set, which produces a different message.
        """
        if queue_url is None:
            message = (
                f"{env_var} is not set. Set it to the queue URL output by "
                '`cdk deploy` as "DrEventsQueueUrl" (see '
                "infra/stacks/registers_stack.py) -- e.g. in this MCP "
                "server's env or .mcp.json. Without it there is no queue "
                "to read, and returning an empty list instead would be "
                'indistinguishable from "no events waiting".'
            )
        else:
            message = (
                f'SQS queue "{queue_url}" (from {env_var}) could not be '
                "reached -- it may not exist in this account/region, or "
                "the stack was redeployed with a new queue. Re-check the "
                '"DrEventsQueueUrl" output from `cdk deploy` and update '
                f"{env_var}."
            )
        super().__init__(message)
        self.env_var = env_var
        self.queue_url = queue_url


class MissingCauseError(TriadDRError):
    """Raised when `dr_review` records a missed IDR verdict without `cause`.

    Per spec section 1/5, `cause` is required (not merely warned about)
    when verdict="missed" on an IDR, because the three causes route to
    different accountable parties and different next actions.
    """

    def __init__(self) -> None:
        """Build the actionable missing-cause message naming all three options."""
        message = (
            'A "missed" verdict on an IDR requires cause. Choose one: '
            '"market" (Seller accountability — the thesis about the audience '
            "was wrong and is disproven; next record is an MDR), "
            '"delivery" (Builder accountability — the built thing did not '
            "match the specified experience; the thesis is untested, not "
            "disproven; next record is an ADR), or "
            '"both" (some of each — the bet stays untested until the '
            "delivery gap closes)."
        )
        super().__init__(message)


class MissingRevisionNoteError(TriadDRError):
    """Raised when `dr_revise` is called without a non-empty `note`.

    Per the tool's own rationale, `note` is not optional decoration: a
    revision with no explanation of why the reasoning changed is
    indistinguishable from a plain edit, and explaining why is the entire
    reason to reach for `dr_revise` instead of `dr_update`.
    """

    def __init__(self) -> None:
        """Build the actionable missing-note message."""
        message = (
            "dr_revise requires a non-empty note explaining why the "
            "record's stated reasoning changed even though the decision "
            "itself did not. A revision with no note is indistinguishable "
            "from a plain edit — if there is truly nothing to explain, use "
            "dr_update instead."
        )
        super().__init__(message)


class MissingWithdrawalReasonError(TriadDRError):
    """Raised when `dr_withdraw` is called without a non-empty `reason`.

    Per the tool's own rationale, `reason` is not optional decoration: a
    withdrawal with no reason is indistinguishable from an abandoned
    escalation, and telling those apart is the entire point of
    `dr_withdraw` existing as a real, reachable exit distinct from
    `dr_answer`.
    """

    def __init__(self) -> None:
        """Build the actionable missing-reason message."""
        message = (
            "dr_withdraw requires a non-empty reason explaining why the "
            "question became moot. A withdrawal with no reason is "
            "indistinguishable from an abandoned escalation — telling "
            "those apart is the entire point of dr_withdraw existing "
            "separately from dr_answer. When duplicate_of is given, this "
            "reason may be brief; the link itself carries the meaning."
        )
        super().__init__(message)
