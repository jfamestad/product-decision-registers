"""DynamoDB-backed storage layer for decision registers.

Implements the data operations described in `decision-registers-mcp-spec.md`
sections 4 and 5 against the single-table layout in section 4:

    | Item              | pk              | sk                    |
    |-------------------|-----------------|-----------------------|
    | Decision record   | PROJ#<project>  | REC#<TYPE>#<seq04>    |
    | Escalation        | PROJ#<project>  | ESC#<seq04>           |
    | Sequence counter  | PROJ#<project>  | CTR#<TYPE>            |
    | Project registry  | META#PROJECTS   | PROJ#<project>        |

`CTR#<TYPE>` counters include `CTR#ESC` — escalations share the same
atomic-counter mechanism as ADR/IDR/MDR records, just with their own
per-project sequence.

`Store` is a pure Python API — it has no knowledge of MCP. The tool layer
that exposes these operations as `dr_*` MCP tools is a separate module.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
import structlog
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
from ulid import ULID

from .errors import (
    CredentialsError,
    EscalationAlreadyResolvedError,
    EscalationNotFoundError,
    InvalidRefError,
    InvalidTransitionError,
    MissingCauseError,
    MissingRevisionNoteError,
    MissingWithdrawalReasonError,
    NoProjectError,
    ProtectedAttributeError,
    RecordNotFoundError,
    TableMissingError,
)
from .models import (
    PERSONA_BY_TYPE,
    ALLOWED_TRANSITIONS,
    Cause,
    Escalation,
    EscalationOption,
    EscalationStatus,
    Link,
    Persona,
    RecType,
    Record,
    Review,
    Revision,
    Status,
    StatusEvent,
    Verdict,
)

logger = structlog.get_logger(__name__)

_REF_PATTERN = re.compile(r"^(ADR|IDR|MDR)-(\d{4})$")
_ESC_REF_PATTERN = re.compile(r"^ESC-(\d{4})$")
_PROJECT_SLUG_PATTERN = re.compile(r"^[a-z0-9-]{2,40}$")

# Attributes dr_update may never touch, and the guidance to show when it's tried.
# Per spec section 4/5: "status, status_history, reviews, seq, ref, id, project,
# rec_type, created_at, supersedes, superseded_by are not patchable — use the
# dedicated tools; attempting them is an error naming the right tool."
_PROTECTED_ATTRS: dict[str, str] = {
    "status": "it is machine-written. Use dr_set_status to change status.",
    "status_history": (
        "it is machine-written by dr_set_status. Use dr_set_status to add an entry."
    ),
    "reviews": "it is machine-written by dr_review. Use dr_review to add an entry.",
    "seq": "it is assigned once at creation and is immutable.",
    "ref": "it is derived from rec_type and seq and is immutable.",
    "id": "it is the record's permanent identity and is immutable.",
    "project": (
        "a record cannot change project. Use dr_create to write it under a "
        "different project."
    ),
    "rec_type": (
        "a record's type is fixed at creation. Use dr_create to write a new "
        "record of the desired type."
    ),
    "created_at": "it is stamped once at creation and is immutable.",
    "supersedes": (
        "it is machine-written by dr_supersede. Use dr_supersede to supersede "
        "this record."
    ),
    "superseded_by": "it is machine-written by dr_supersede.",
}

# Record attributes dr_update is allowed to replace wholesale. Of these,
# "fields" is special-cased in update() to merge key-by-key instead --
# replacing a nested map wholesale is the footgun that motivated that split
# (see update()'s docstring). Every other key here keeps wholesale-replace
# semantics: replacing a list wholesale is predictable, replacing a map
# wholesale is not.
_MUTABLE_ATTRS: frozenset[str] = frozenset(
    {"title", "body_md", "fields", "tags", "links", "deciders"}
)

# Known fields.<key> names per record type (spec section 4 templates),
# duplicated from server.py's _TOOL_DESCRIPTIONS/_TEMPLATE_FIELDS rather than
# imported from it -- store.py is a pure Python API with no knowledge of the
# MCP tool layer (see module docstring), so the dependency can only run
# store.py -> server.py, never the reverse. Used solely to make dr_update's
# "unknown patch key" error actionable when a caller mistakes a fields key
# (e.g. "delegation") for a top-level patch attribute -- a real operator
# error, not a hypothetical one.
_TEMPLATE_FIELD_NAMES: dict[str, frozenset[str]] = {
    "ADR": frozenset({"context", "decision", "consequences", "alternatives_considered"}),
    "IDR": frozenset(
        {
            "bet",
            "thesis",
            "investment",
            "success_criteria",
            "kill_criteria",
            "review_date",
            "delegation",
        }
    ),
    "MDR": frozenset(
        {
            "audience",
            "jobs_to_be_done",
            "positioning",
            "channel_motion",
            "evidence_plan",
            "user_evidence",
            "experiment",
            "success_metric",
        }
    ),
}

_GOVERNING_RELATION = "implements-bet"

# The commitment gate (spec section 4, "The commitment gate:
# requires-attestation"): an IDR's precondition links, named by relation
# rather than a new record type.
_ATTESTATION_RELATION = "requires-attestation"

# Placeholders and unmeasurable criteria (spec section 4): patterns scanned
# on transition to "accepted". A markdown checkbox ("- [x]" / "- [ ]")
# deliberately has the same shape as the single-letter bracket pattern
# below, so _CHECKBOX_RE identifies those spans up front and
# _find_placeholders excludes them, rather than trying to make the
# placeholder pattern itself avoid every checkbox shape.
_CHECKBOX_RE = re.compile(r"(?m)^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(\[[ xX]\])")
_BRACKET_LETTER_PLACEHOLDER_RE = re.compile(r"\[[A-Z]\]")
_WORD_PLACEHOLDER_RE = re.compile(r"\b(?:TBD|TODO|FIXME)\b", re.IGNORECASE)
_TRIPLE_QUESTION_PLACEHOLDER_RE = re.compile(r"\?\?\?")
_ANGLE_BRACKET_PLACEHOLDER_RE = re.compile(r"<[A-Za-z][\w .'-]{0,40}>")
_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    _BRACKET_LETTER_PLACEHOLDER_RE,
    _WORD_PLACEHOLDER_RE,
    _TRIPLE_QUESTION_PLACEHOLDER_RE,
    _ANGLE_BRACKET_PLACEHOLDER_RE,
)

# Stale references (spec section 4): a ref appearing in body_md prose, e.g.
# "see IDR-0003 for the bet this replaced."
_PROSE_REF_PATTERN = re.compile(r"\b(?:ADR|IDR|MDR)-\d{4}\b")

# Self-reference placeholder (create() Change 3): refs are allocated at seq
# time, so a record drafted before its own ref is known has no honest way to
# name itself. "{{ref}}" is the literal, case-sensitive token a caller writes
# instead of guessing -- create() substitutes it for the just-assigned ref in
# body_md and every string value in fields.
_REF_PLACEHOLDER = "{{ref}}"

# Matches only the FIRST markdown ATX heading ("# ", "## ", ...) in a body --
# deliberately not every heading, and not body text generally. Scoped this
# way because body prose legitimately references other records constantly
# ("see IDR-0003 for prior art"); a heading naming the WRONG ref is a
# different, narrower failure -- almost always a self-reference guessed
# during drafting that a concurrent session's write invalidated.
_FIRST_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]+(.*)$")


def _find_placeholders(text: str) -> list[str]:
    """Find unfilled placeholders in a string, in the order they appear.

    Per spec section 4 ("Placeholders and unmeasurable criteria"): matches
    single-letter bracket placeholders ("[X]", "[Y]"), TBD/TODO/FIXME
    (case-insensitive, bracketed or bare -- "[TBD]" is caught by the same
    word-boundary match as bare "TBD"), "???", and "<name>"-style
    angle-bracket placeholders. A markdown checkbox ("- [x]" / "- [ ]") is
    deliberately excluded -- its "[x]" is list syntax, not an unfilled
    placeholder, even though it has the same single-letter bracket shape.

    Args:
        text: The string to scan (a `fields` value or `body_md`).

    Returns:
        The literal matched substrings, e.g. ``["[X]", "TBD"]``, in the
        order they appear in `text`. Checkbox brackets are never included.
    """
    if not text:
        return []
    checkbox_spans = {m.span(1) for m in _CHECKBOX_RE.finditer(text)}
    matches = [m for pattern in _PLACEHOLDER_PATTERNS for m in pattern.finditer(text)]
    matches.sort(key=lambda m: m.start())

    found: list[str] = []
    seen_spans: set[tuple[int, int]] = set()
    for match in matches:
        span = match.span()
        if span in checkbox_spans or span in seen_spans:
            continue
        seen_spans.add(span)
        found.append(match.group(0))
    return found


def _substitute_ref_placeholder(value: Any, ref: str) -> Any:
    """Recursively replace the literal "{{ref}}" token with the assigned ref.

    Case-sensitive, exact-token match only -- no fuzzy matching, no
    surrounding-whitespace tolerance beyond what's literally in the token.
    Applied to `body_md` (a string) and to `fields` (a possibly-nested
    dict/list of strings) after `create()` allocates the seq, so a record
    can self-reference correctly without guessing a ref that a concurrent
    session might claim first.

    Args:
        value: A string, or a dict/list that may contain strings nested
            arbitrarily deep (as `fields` can), to substitute within.
        ref: The just-allocated ref, e.g. "ADR-0007".

    Returns:
        The same structure with every literal "{{ref}}" replaced by `ref`.
        Non-string, non-dict, non-list values pass through unchanged.
    """
    if isinstance(value, str):
        return value.replace(_REF_PLACEHOLDER, ref)
    if isinstance(value, dict):
        return {k: _substitute_ref_placeholder(v, ref) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_ref_placeholder(v, ref) for v in value]
    return value


def _self_reference_warning(body_md: str, ref: str) -> str | None:
    """Warn if body_md's FIRST markdown heading names a different ref.

    Scoped to the first heading only -- not every heading, and not body
    text generally. Body prose legitimately references other records
    constantly ("see IDR-0003 for prior art"), and warning on those would
    be noise indistinguishable from normal cross-referencing. A heading
    naming a ref other than this record's own is a different, much
    narrower thing: refs are assigned at write time, so any self-reference
    written into a heading during drafting is a guess, and this one
    guessed wrong -- most often because a concurrent session claimed the
    guessed ref first.

    Args:
        body_md: The record's body, already {{ref}}-substituted -- so a
            heading that correctly used the "{{ref}}" placeholder has
            already resolved to `ref` and will not warn.
        ref: The ref actually assigned to this record.

    Returns:
        A warning naming both refs, or None if the first heading has no
        well-formed ref, or names this record's own ref.
    """
    heading_match = _FIRST_HEADING_RE.search(body_md)
    if heading_match is None:
        return None
    ref_match = _PROSE_REF_PATTERN.search(heading_match.group(1))
    if ref_match is None:
        return None
    heading_ref = ref_match.group(0)
    if heading_ref == ref:
        return None
    return (
        f'This record\'s first heading names "{heading_ref}", but it was '
        f'assigned "{ref}" instead. Refs are allocated at write time, so a '
        "self-reference written during drafting is a guess -- this one "
        "guessed wrong, most likely because a concurrent session claimed "
        f'"{heading_ref}" first. Update the heading to "{ref}".'
    )


def _floats_to_decimal(value: Any) -> Any:
    """Recursively convert Python floats to Decimal for DynamoDB storage.

    Args:
        value: Any JSON-safe Python value (possibly nested dict/list).

    Returns:
        The same structure with every float replaced by a Decimal.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _floats_to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_floats_to_decimal(v) for v in value]
    return value


def _decimals_to_native(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals back to int/float on read.

    Args:
        value: A value freshly returned from boto3's DynamoDB resource layer.

    Returns:
        The same structure with every Decimal converted to int (if whole)
        or float.
    """
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: _decimals_to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimals_to_native(v) for v in value]
    return value


class Store:
    """DynamoDB-backed storage layer for ADR / IDR / MDR decision records.

    Pure Python API with no MCP awareness — the MCP tool layer is a separate,
    not-yet-built consumer of this class. Dependency injection only: no
    global mutable state, no implicit singleton table handle.

    Attributes:
        table_name: The DynamoDB table name this store operates against.
    """

    def __init__(self, table_name: str, resource: Any | None = None) -> None:
        """Initialize the store.

        Args:
            table_name: Name of the DynamoDB table (see infra/ for the CDK
                definition; default is "triad-decision-registers").
            resource: A boto3 DynamoDB ServiceResource (e.g.
                ``boto3.resource("dynamodb")``), injected for testing
                (moto) or custom session/region configuration. Defaults to
                a new resource built from the default boto3 session/region
                chain.
        """
        self.table_name = table_name
        self._resource = resource if resource is not None else boto3.resource("dynamodb")
        self._table = self._resource.Table(table_name)

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _project_pk(project: str) -> str:
        """Build the partition key for a project's items.

        Args:
            project: Project slug.

        Returns:
            The partition key, e.g. "PROJ#acme-app".
        """
        return f"PROJ#{project}"

    @staticmethod
    def _record_sk(rec_type: str, seq: int) -> str:
        """Build the sort key for a decision record item.

        Args:
            rec_type: One of "ADR", "IDR", "MDR".
            seq: Per-project, per-type sequence number.

        Returns:
            The sort key, e.g. "REC#ADR#0007".
        """
        return f"REC#{rec_type}#{seq:04d}"

    @staticmethod
    def _counter_sk(rec_type: str) -> str:
        """Build the sort key for a sequence counter item.

        Args:
            rec_type: One of "ADR", "IDR", "MDR".

        Returns:
            The sort key, e.g. "CTR#ADR".
        """
        return f"CTR#{rec_type}"

    @staticmethod
    def _escalation_sk(seq: int) -> str:
        """Build the sort key for an escalation item.

        Args:
            seq: Per-project ESC sequence number.

        Returns:
            The sort key, e.g. "ESC#0007".
        """
        return f"ESC#{seq:04d}"

    @staticmethod
    def _now() -> str:
        """Return the current UTC timestamp as an ISO-8601 string."""
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Project resolution
    # ------------------------------------------------------------------

    def resolve_project(self, explicit: str | None = None) -> str:
        """Resolve the active project namespace.

        Resolution order: explicit argument, then the ``DR_PROJECT``
        environment variable, then an error. Never falls back to a
        default project and never guesses.

        Args:
            explicit: An explicit project slug passed by the caller, if any.

        Returns:
            The resolved project slug.

        Raises:
            NoProjectError: Neither `explicit` nor `DR_PROJECT` is set.
            ValueError: The resolved project does not match the required
                slug format ``[a-z0-9-]{2,40}``.
        """
        project = explicit or os.environ.get("DR_PROJECT")
        if not project:
            raise NoProjectError(self._known_project_slugs())
        if not _PROJECT_SLUG_PATTERN.fullmatch(project):
            raise ValueError(
                f'Invalid project slug "{project}". Must match [a-z0-9-]{{2,40}}.'
            )
        return project

    def _known_project_slugs(self) -> list[str]:
        """Fetch known project slugs from the registry, for error messages.

        Returns:
            Sorted list of project slugs currently in the registry.
        """
        items = self._query_all(KeyConditionExpression=Key("pk").eq("META#PROJECTS"))
        return sorted(item["sk"].removeprefix("PROJ#") for item in items)

    # ------------------------------------------------------------------
    # Ref / link validation
    # ------------------------------------------------------------------

    def _parse_ref(self, ref: str) -> tuple[str, int]:
        """Validate and split a ref into (rec_type, seq).

        Args:
            ref: A record ref, e.g. "ADR-0007".

        Returns:
            The (rec_type, seq) tuple.

        Raises:
            InvalidRefError: `ref` does not match ``^(ADR|IDR|MDR)-\\d{4}$``.
        """
        match = _REF_PATTERN.fullmatch(ref)
        if not match:
            raise InvalidRefError(ref)
        return match.group(1), int(match.group(2))

    def _parse_escalation_ref(self, ref: str) -> int:
        """Validate and extract the seq from an escalation ref.

        Args:
            ref: An escalation ref, e.g. "ESC-0007".

        Returns:
            The parsed seq number.

        Raises:
            InvalidRefError: `ref` does not match ``^ESC-\\d{4}$``.
        """
        match = _ESC_REF_PATTERN.fullmatch(ref)
        if not match:
            raise InvalidRefError(ref)
        return int(match.group(1))

    def _validate_links(self, links: list[dict[str, str]] | list[Link]) -> list[Link]:
        """Validate and normalize a list of links.

        Existence of the target record is never verified — forward
        references are legal per spec section 4. Only ref format is
        checked.

        Args:
            links: Raw link dicts (``{"ref": ..., "relation": ...}``) or
                already-built `Link` models.

        Returns:
            A list of validated `Link` models.

        Raises:
            InvalidRefError: Any link's `ref` is not well-formed.
        """
        validated: list[Link] = []
        for link in links:
            link_obj = link if isinstance(link, Link) else Link(**link)
            self._parse_ref(link_obj.ref)
            validated.append(link_obj)
        return validated

    # ------------------------------------------------------------------
    # boto3 call wrapping / pagination
    # ------------------------------------------------------------------

    def _call(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        """Invoke a boto3 call, translating infra errors into actionable ones.

        Args:
            fn: A bound boto3 method (e.g. ``self._table.get_item``).
            **kwargs: Keyword arguments forwarded to `fn`.

        Returns:
            Whatever `fn` returns.

        Raises:
            CredentialsError: AWS credentials are missing or an SSO session
                has expired.
            TableMissingError: The DynamoDB table does not exist.
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
            if code == "ResourceNotFoundException":
                raise TableMissingError(self.table_name) from exc
            raise

    def _query_all(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Run a paginated Query to completion and return all items.

        Args:
            **kwargs: Arguments forwarded to ``Table.query``.

        Returns:
            All matching items across pages.
        """
        items: list[dict[str, Any]] = []
        while True:
            response = self._call(self._table.query, **kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items

    def _scan_all(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Run a paginated Scan to completion and return all items.

        Args:
            **kwargs: Arguments forwarded to ``Table.scan``.

        Returns:
            All matching items across pages.
        """
        items: list[dict[str, Any]] = []
        while True:
            response = self._call(self._table.scan, **kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items

    # ------------------------------------------------------------------
    # Item <-> Record conversion
    # ------------------------------------------------------------------

    def _to_item(self, record: Record) -> dict[str, Any]:
        """Convert a `Record` to a DynamoDB item dict (resource-layer format).

        Args:
            record: The record to serialize.

        Returns:
            A dict suitable for ``Table.put_item(Item=...)``, with pk/sk
            populated and floats converted to Decimal.
        """
        data = _floats_to_decimal(record.model_dump())
        data["pk"] = self._project_pk(record.project)
        data["sk"] = self._record_sk(record.rec_type, record.seq)
        return data

    def _from_item(self, item: dict[str, Any]) -> Record:
        """Convert a DynamoDB item dict back into a `Record`.

        Args:
            item: A raw item as returned by the boto3 resource layer.

        Returns:
            The parsed `Record`.
        """
        data = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        data = _decimals_to_native(data)
        return Record(**data)

    def _put_record(self, record: Record) -> None:
        """Write a record item, overwriting whatever was there before.

        Args:
            record: The record to persist.
        """
        self._call(self._table.put_item, Item=self._to_item(record))

    def _list_all_records(self, project: str) -> list[Record]:
        """Fetch every decision record (not counters) in a project partition.

        Args:
            project: Resolved project slug.

        Returns:
            All records in the project, in no particular order.
        """
        items = self._query_all(
            KeyConditionExpression=(
                Key("pk").eq(self._project_pk(project)) & Key("sk").begins_with("REC#")
            )
        )
        return [self._from_item(item) for item in items]

    # ------------------------------------------------------------------
    # Item <-> Escalation conversion
    # ------------------------------------------------------------------

    def _escalation_to_item(self, escalation: Escalation) -> dict[str, Any]:
        """Convert an `Escalation` to a DynamoDB item dict (resource-layer format).

        Args:
            escalation: The escalation to serialize.

        Returns:
            A dict suitable for ``Table.put_item(Item=...)``, with pk/sk
            populated and floats converted to Decimal.
        """
        data = _floats_to_decimal(escalation.model_dump())
        data["pk"] = self._project_pk(escalation.project)
        data["sk"] = self._escalation_sk(self._parse_escalation_ref(escalation.ref))
        return data

    def _from_escalation_item(self, item: dict[str, Any]) -> Escalation:
        """Convert a DynamoDB item dict back into an `Escalation`.

        Args:
            item: A raw item as returned by the boto3 resource layer.

        Returns:
            The parsed `Escalation`.
        """
        data = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        data = _decimals_to_native(data)
        return Escalation(**data)

    def _put_escalation(self, escalation: Escalation) -> None:
        """Write an escalation item, overwriting whatever was there before.

        Args:
            escalation: The escalation to persist.
        """
        self._call(self._table.put_item, Item=self._escalation_to_item(escalation))

    def _get_escalation(self, ref: str, project: str) -> Escalation:
        """Fetch a single escalation by its human ref.

        Args:
            ref: Escalation ref, e.g. "ESC-0007".
            project: Resolved project slug.

        Returns:
            The matching `Escalation`.

        Raises:
            InvalidRefError: `ref` is not well-formed.
            EscalationNotFoundError: No escalation exists for that ref.
        """
        seq = self._parse_escalation_ref(ref)
        response = self._call(
            self._table.get_item,
            Key={"pk": self._project_pk(project), "sk": self._escalation_sk(seq)},
        )
        item = response.get("Item")
        if item is None:
            raise EscalationNotFoundError(ref)
        return self._from_escalation_item(item)

    def _list_all_escalations(self, project: str) -> list[Escalation]:
        """Fetch every escalation (not records or counters) in a project partition.

        Args:
            project: Resolved project slug.

        Returns:
            All escalations in the project, in no particular order.
        """
        items = self._query_all(
            KeyConditionExpression=(
                Key("pk").eq(self._project_pk(project)) & Key("sk").begins_with("ESC#")
            )
        )
        return [self._from_escalation_item(item) for item in items]

    # ------------------------------------------------------------------
    # Governance warnings
    # ------------------------------------------------------------------

    def _create_warnings(self, record: Record) -> list[str]:
        """Compute the non-blocking governance warnings for a new record.

        Per spec sections 4/5: ADR/MDR records with no "implements-bet"
        link warn; MDRs with neither `user_evidence` nor `evidence_plan`
        in `fields` warn. Warnings never block the write.

        Args:
            record: The freshly created (or superseding) record.

        Returns:
            A list of human-readable warning strings (possibly empty).
        """
        warnings: list[str] = []
        if record.rec_type in ("ADR", "MDR"):
            has_bet_link = any(
                link.relation == _GOVERNING_RELATION for link in record.links
            )
            if not has_bet_link:
                warnings.append(
                    f'{record.ref} has no "{_GOVERNING_RELATION}" link to a governing '
                    "IDR. Per the register's rule (no bet, no work), link it to the "
                    "IDR that funds this work — dr_list(unfunded=True) will keep "
                    "surfacing it until you do."
                )
        if record.rec_type == "MDR":
            user_evidence = record.fields.get("user_evidence")
            evidence_plan = record.fields.get("evidence_plan")
            if not user_evidence and not evidence_plan:
                warnings.append(
                    f'{record.ref} has neither "user_evidence" nor "evidence_plan" '
                    "in fields. Without a plan to gather evidence, this bet's only "
                    "trip-wire is the governing IDR's review_date — by which point "
                    "the build is already paid for."
                )
        return warnings

    def _escalate_warnings(self, escalation: Escalation) -> list[str]:
        """Compute the non-blocking governance warnings for a new escalation.

        Per spec section 1: these warn, never block — a blocked worker must
        always be able to file. An escalation missing any of these is still
        written with `status="open"`; the warnings only flag that the item
        may sit unanswered longer than it should because it isn't as
        answerable-in-thirty-seconds as it could be.

        Args:
            escalation: The freshly filed escalation.

        Returns:
            A list of human-readable warning strings (possibly empty).
        """
        warnings: list[str] = []
        if len(escalation.options) < 2:
            warnings.append(
                f"{escalation.ref} has fewer than 2 options — an escalation "
                "with no alternatives is a question, not a decision."
            )
        if not escalation.recommendation:
            warnings.append(
                f"{escalation.ref} has no recommendation — the agent did the "
                "work and should state a view."
            )
        if not escalation.cost_of_delay:
            warnings.append(f"{escalation.ref} has no cost_of_delay stated.")
        if not escalation.governing_ref:
            warnings.append(
                f"{escalation.ref} has no governing_ref — the escalated work "
                "has no bet behind it."
            )
        return warnings

    def _placeholder_warnings(self, record: Record) -> list[str]:
        """Compute placeholder-lint warnings for a record moving to "accepted".

        Per spec section 4 ("Placeholders and unmeasurable criteria"): scans
        every `fields` value and `body_md` for unfilled placeholders and
        warns naming each one and where it sits. Never blocks the
        transition — an accepted bet committing "$[X]/month" has no
        appetite, and an appetite is the point, but the write still
        succeeds; the warning is what makes the gap visible.

        Args:
            record: The record transitioning to "accepted" (already
                mutated to that status by the caller).

        Returns:
            A list of human-readable warning strings, one per placeholder
            found (possibly empty).
        """
        warnings: list[str] = []
        for field_name, value in sorted(record.fields.items()):
            for placeholder in _find_placeholders(str(value)):
                warnings.append(
                    f'{record.ref} has an unfilled placeholder "{placeholder}" '
                    f'in fields.{field_name}. An accepted record still '
                    "carrying a placeholder has no real commitment behind "
                    "that value yet."
                )
        for placeholder in _find_placeholders(record.body_md):
            warnings.append(
                f'{record.ref} has an unfilled placeholder "{placeholder}" '
                "in body_md."
            )
        return warnings

    def _attestation_warnings(self, record: Record) -> list[str]:
        """Compute the commitment-gate warning for an IDR moving to "accepted".

        Per spec section 4 ("The commitment gate: requires-attestation"):
        an IDR names its Builder/Seller preconditions for going all in as
        links with relation "requires-attestation". On transition to
        "accepted", every such target is checked and named if it is not
        itself "accepted" — whether it exists in some other status or does
        not exist at all (the two read differently and are worded
        differently). Never blocks: a solo operator may knowingly commit
        ahead of an attestation, and the gate's only job is making sure
        that call is written down rather than reconstructed later.

        A no-op for any record that is not an IDR.

        Args:
            record: The record transitioning to "accepted" (already
                mutated to that status by the caller).

        Returns:
            A list with zero or one warning string naming every
            not-yet-accepted (or missing) attestation target.
        """
        if record.rec_type != "IDR":
            return []
        unmet: list[str] = []
        for link in record.links:
            if link.relation != _ATTESTATION_RELATION:
                continue
            try:
                target = self.get(link.ref, record.project)
            except RecordNotFoundError:
                unmet.append(f"{link.ref} (does not exist)")
                continue
            if target.status != "accepted":
                unmet.append(f"{link.ref} (is still {target.status})")
        if not unmet:
            return []
        return [
            f'{record.ref} is going "accepted" with "{_ATTESTATION_RELATION}" '
            f"target(s) not themselves accepted: {', '.join(unmet)}. Going all "
            "in before every attestation is itself accepted is a real, "
            "permitted call for a solo operator — this warning exists so "
            "that call is on the record, not reconstructed after the fact."
        ]

    # ------------------------------------------------------------------
    # dr_create
    # ------------------------------------------------------------------

    def create(
        self,
        rec_type: RecType,
        title: str,
        body_md: str = "",
        fields: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        links: list[dict[str, str]] | None = None,
        deciders: list[str] | None = None,
        status: Status = "proposed",
        project: str | None = None,
    ) -> tuple[Record, list[str]]:
        """Create a new decision record.

        Allocates the next sequence number atomically, writes the record,
        and upserts the project registry entry.

        Args:
            rec_type: One of "ADR", "IDR", "MDR".
            title: Imperative title, e.g. "Use DynamoDB single-table design".
            body_md: Full markdown body (canonical prose).
            fields: Type-specific structured fields (see spec section 4
                templates for the expected keys per type).
            tags: Free-text tags.
            links: Cross-type, same-project links as
                ``[{"ref": ..., "relation": ...}, ...]``.
            deciders: Names/identifiers of who decided.
            status: Initial status. Defaults to "proposed".
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the created `Record`, a list of non-blocking
            governance warning strings).

        Raises:
            NoProjectError: No project could be resolved.
            InvalidRefError: A link ref is malformed.
            TableMissingError: The DynamoDB table does not exist.
            CredentialsError: AWS credentials are missing or expired.
        """
        resolved_project = self.resolve_project(project)
        links_obj = self._validate_links(links or [])
        seq = self._allocate_seq(resolved_project, rec_type)
        ref = f"{rec_type}-{seq:04d}"
        now = self._now()
        # The ref only exists now, after allocation -- so anything the caller
        # drafted referring to "this record" was written before its name was
        # known. Under concurrent sessions a hand-written guess is often wrong
        # (another session claims the number mid-draft), so "{{ref}}" is the
        # supported way to self-reference and is resolved here.
        body_md = _substitute_ref_placeholder(body_md, ref)
        fields = _substitute_ref_placeholder(fields or {}, ref)
        record = Record(
            id=str(ULID()),
            project=resolved_project,
            rec_type=rec_type,
            seq=seq,
            ref=ref,
            title=title,
            status=status,
            persona=PERSONA_BY_TYPE[rec_type],
            created_at=now,
            updated_at=now,
            deciders=deciders or [],
            tags=tags or [],
            links=links_obj,
            supersedes=None,
            superseded_by=None,
            status_history=[StatusEvent(at=now, status=status, note="created")],
            reviews=[],
            fields=fields,
            body_md=body_md,
        )
        self._put_record(record)
        self._upsert_project_registry(resolved_project)
        warnings = self._create_warnings(record)
        self_ref = _self_reference_warning(record.body_md, ref)
        if self_ref:
            warnings.append(self_ref)
        logger.info("record.created", ref=ref, project=resolved_project, warnings=warnings)
        return record, warnings

    def _allocate_seq(self, project: str, rec_type: str) -> int:
        """Atomically allocate the next per-project, per-type sequence number.

        Args:
            project: Resolved project slug.
            rec_type: One of "ADR", "IDR", "MDR".

        Returns:
            The newly allocated sequence number.
        """
        response = self._call(
            self._table.update_item,
            Key={"pk": self._project_pk(project), "sk": self._counter_sk(rec_type)},
            UpdateExpression="ADD #next_seq :one",
            ExpressionAttributeNames={"#next_seq": "next_seq"},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["next_seq"])

    def _upsert_project_registry(self, project: str) -> int:
        """Upsert the project registry item and atomically bump its version.

        `version` is the freshness signal: a monotonic per-project change
        counter, bumped by every mutating operation (create, update,
        set_status, review) via one `ADD version :one` folded into the same
        UpdateItem this method already ran on every write -- no extra round
        trip. It is a COUNTER, not a timestamp: it cheaply answers "did
        anything change" (compare two integers) where `list(since=...)`
        answers "what changed" (a Query plus a client-side filter). They are
        deliberately not interchangeable -- records do not store the
        version they were written at, so `since` cannot be expressed in
        version units.

        Args:
            project: Resolved project slug.

        Returns:
            The project's new `version` after this increment.
        """
        response = self._call(
            self._table.update_item,
            Key={"pk": "META#PROJECTS", "sk": f"PROJ#{project}"},
            UpdateExpression=(
                "SET #project = if_not_exists(#project, :project), "
                "#created_at = if_not_exists(#created_at, :created_at) "
                "ADD #version :one"
            ),
            ExpressionAttributeNames={
                "#project": "project",
                "#created_at": "created_at",
                "#version": "version",
            },
            ExpressionAttributeValues={
                ":project": project,
                ":created_at": self._now(),
                ":one": 1,
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["version"])

    def project_version(self, project: str | None = None) -> int:
        """Fetch a project's current freshness-signal version.

        A single `GetItem` on the project registry item -- not a Query --
        for read paths (`dr_get`, `dr_list`, `dr_search`, `dr_due`,
        `dr_export`) that want the freshness signal without paying for a
        write. Mutating calls (create, update, set_status, review) get
        their new version for free from `_upsert_project_registry`'s own
        UpdateItem and never need this method.

        Args:
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The project's current `version`, or 0 if the project has never
            been written to (no registry item exists yet).

        Raises:
            NoProjectError: No project could be resolved.
        """
        resolved_project = self.resolve_project(project)
        response = self._call(
            self._table.get_item,
            Key={"pk": "META#PROJECTS", "sk": f"PROJ#{resolved_project}"},
        )
        item = response.get("Item")
        if item is None:
            return 0
        return int(item.get("version", 0))

    # ------------------------------------------------------------------
    # dr_get
    # ------------------------------------------------------------------

    def get(self, ref: str, project: str | None = None) -> Record:
        """Fetch a single record by its human ref.

        Args:
            ref: Record ref, e.g. "ADR-0007".
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The matching `Record`.

        Raises:
            InvalidRefError: `ref` is not well-formed.
            NoProjectError: No project could be resolved.
            RecordNotFoundError: No record exists for that ref.
        """
        rec_type, seq = self._parse_ref(ref)
        resolved_project = self.resolve_project(project)
        response = self._call(
            self._table.get_item,
            Key={
                "pk": self._project_pk(resolved_project),
                "sk": self._record_sk(rec_type, seq),
            },
        )
        item = response.get("Item")
        if item is None:
            raise RecordNotFoundError(ref)
        return self._from_item(item)

    def get_by_id(self, id: str, project: str | None = None) -> Record:  # noqa: A002
        """Fetch a single record by its internal ULID.

        Args:
            id: The record's ULID.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The matching `Record`.

        Raises:
            NoProjectError: No project could be resolved.
            RecordNotFoundError: No record exists for that id.
        """
        resolved_project = self.resolve_project(project)
        items = self._query_all(
            KeyConditionExpression=(
                Key("pk").eq(self._project_pk(resolved_project))
                & Key("sk").begins_with("REC#")
            ),
            FilterExpression=Attr("id").eq(id),
        )
        if not items:
            raise RecordNotFoundError(id)
        return self._from_item(items[0])

    # ------------------------------------------------------------------
    # dr_list
    # ------------------------------------------------------------------

    def list(
        self,
        rec_type: RecType | None = None,
        status: Status | None = None,
        tag: str | None = None,
        persona: Persona | None = None,
        unfunded: bool | None = None,
        stale: bool | None = None,
        since: str | None = None,
        limit: int = 50,
        project: str | None = None,
    ) -> list[Record]:
        """List records in a project, newest first.

        The SK (``REC#<TYPE>#<seq04>``) sorts lexically and would group by
        type before time, so this queries the partition and sorts by
        `created_at` descending client-side before applying `limit`.

        Args:
            rec_type: Filter to one record type.
            status: Filter to one status.
            tag: Filter to records carrying this tag.
            persona: Filter to one persona (derived 1:1 from rec_type).
            unfunded: If True, return only ADRs/MDRs with no
                "implements-bet" link.
            stale: If True, return only records with a stale reference
                (spec section 4, "Stale references"): a link whose target
                is "superseded"/"deprecated", or a body_md prose ref
                (``<TYPE>-<seq04>``) pointing at a record that is
                "superseded", "deprecated", or does not exist. Distinct
                from `unfunded` — a stale record still has its governing
                link, it just points at a dead bet. Use `stale_reasons()`
                to see *why* each one is stale.
            since: ISO-8601 timestamp; return only records whose
                `updated_at` is strictly after it. This is the "what
                changed" half of the freshness signal — `project_version()`
                cheaply answers *whether* anything changed, and this answers
                *what*. Note the units differ deliberately: `since` filters
                on a timestamp because records do not store the version they
                were written at, so a version cannot be used here.
            limit: Maximum number of records to return, applied after
                sorting.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            Matching records, sorted by `created_at` descending.

        Raises:
            NoProjectError: No project could be resolved.
        """
        resolved_project = self.resolve_project(project)
        all_records = self._list_all_records(resolved_project)
        records = all_records
        if rec_type is not None:
            records = [r for r in records if r.rec_type == rec_type]
        if status is not None:
            records = [r for r in records if r.status == status]
        if tag is not None:
            records = [r for r in records if tag in r.tags]
        if persona is not None:
            records = [r for r in records if r.persona == persona]
        if unfunded:
            records = [
                r
                for r in records
                if r.rec_type in ("ADR", "MDR")
                and not any(link.relation == _GOVERNING_RELATION for link in r.links)
            ]
        if stale:
            # `_compute_stale_reasons` resolves every target status from
            # `all_records` -- the one partition Query already run above --
            # rather than a second Query or a dr_get per link/prose ref.
            stale_refs = self._compute_stale_reasons(all_records)
            records = [r for r in records if r.ref in stale_refs]
        if since is not None:
            since_dt = self._parse_dt(since)
            records = [r for r in records if self._parse_dt(r.updated_at) > since_dt]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def stale_reasons(self, project: str | None = None) -> dict[str, list[str]]:
        """Explain why each stale record in a project is stale.

        Companion to ``list(stale=True)``: that method filters to the
        stale records, this one names the reason for every stale record in
        the project (regardless of any other filter), so a caller can look
        up the reason for each ref it already has. Per spec section 4, a
        bare stale flag is not actionable — this is the pointer.

        Args:
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            Mapping of ref -> list of human-readable reasons that record
            is stale. Refs with no stale references are omitted entirely.

        Raises:
            NoProjectError: No project could be resolved.
        """
        resolved_project = self.resolve_project(project)
        records = self._list_all_records(resolved_project)
        return self._compute_stale_reasons(records)

    def _compute_stale_reasons(self, records: list[Record]) -> dict[str, list[str]]:
        """Compute why each record in an already-fetched list is stale.

        Per spec section 4 ("Stale references"): a record is stale if (a)
        it carries a link whose target is "superseded" or "deprecated" --
        note this is a *different* condition than `unfunded`, which only
        fires on a missing "implements-bet" link; a stale record still has
        its governing link, it just points at a dead bet -- or (b) its
        `body_md` prose contains a well-formed ref (``<TYPE>-<seq04>``)
        pointing at a record that is "superseded", "deprecated", or does
        not exist at all. Unlike links, a missing prose-ref target *is*
        flagged: prose has no forward-reference convention to protect,
        and a paragraph explaining a decision in terms of a bet that never
        existed is just as misleading as one for a bet that is dead.

        Resolves every target status from `records` -- already fetched by
        one partition Query -- rather than issuing a `dr_get` per link or
        prose ref, per spec section 4's stated scale (tens-to-hundreds of
        records per project).

        Args:
            records: Every record in a project partition (as returned by
                `_list_all_records`).

        Returns:
            Mapping of ref -> list of human-readable reasons. Refs with no
            stale references are omitted entirely.
        """
        status_by_ref = {r.ref: r.status for r in records}
        reasons: dict[str, list[str]] = {}
        for record in records:
            record_reasons: list[str] = []
            for link in record.links:
                target_status = status_by_ref.get(link.ref)
                if target_status in ("superseded", "deprecated"):
                    record_reasons.append(
                        f'link "{link.relation}" -> {link.ref}, which is '
                        f"{target_status}"
                    )
            for match in _PROSE_REF_PATTERN.finditer(record.body_md):
                prose_ref = match.group(0)
                if prose_ref == record.ref:
                    continue
                target_status = status_by_ref.get(prose_ref)
                if target_status in ("superseded", "deprecated"):
                    record_reasons.append(
                        f"body_md prose ref to {prose_ref}, which is "
                        f"{target_status}"
                    )
                elif target_status is None:
                    record_reasons.append(
                        f"body_md prose ref to {prose_ref}, which does not exist"
                    )
            deduped = list(dict.fromkeys(record_reasons))
            if deduped:
                reasons[record.ref] = deduped
        return reasons

    # ------------------------------------------------------------------
    # dr_update
    # ------------------------------------------------------------------

    def update(
        self, ref: str, patch: dict[str, Any], project: str | None = None
    ) -> tuple[Record, list[str]]:
        """Apply a wholesale patch to a record's mutable attributes.

        A key present in `patch` replaces that attribute wholesale — there
        is no merging or appending for lists or maps. Keys absent from
        `patch` are untouched.

        Args:
            ref: Record ref, e.g. "ADR-0007".
            patch: Mapping of attribute name to new value. Allowed keys:
                title, body_md, fields, tags, links, deciders.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the updated `Record`, a list of non-blocking
            warning strings — e.g. the soft-immutability warning when
            editing fields/body_md/title on an "accepted" record).

        Raises:
            InvalidRefError: `ref`, or a link ref within `patch["links"]`,
                is not well-formed.
            ProtectedAttributeError: `patch` touches a protected attribute
                (status, status_history, reviews, seq, ref, id, project,
                rec_type, created_at, supersedes, superseded_by).
            ValueError: `patch` contains a key that is neither protected
                nor a recognized mutable attribute.
            NoProjectError: No project could be resolved.
            RecordNotFoundError: No record exists for that ref.
        """
        record = self.get(ref, project)

        for key in patch:
            if key in _PROTECTED_ATTRS:
                raise ProtectedAttributeError(key, _PROTECTED_ATTRS[key])
            if key not in _MUTABLE_ATTRS:
                # A caller who writes {"delegation": ...} meant fields.delegation.
                # Saying only "unknown key" makes them go hunting; naming the
                # correction is the difference between a typo and a lost hour.
                if key in _TEMPLATE_FIELD_NAMES.get(record.rec_type, frozenset()):
                    raise ValueError(
                        f'"{key}" is a key inside `fields`, not a top-level '
                        f"attribute of a {record.rec_type}. Did you mean "
                        f'{{"fields": {{"{key}": ...}}}}? Patchable top-level '
                        f"attributes: {sorted(_MUTABLE_ATTRS)}."
                    )
                raise ValueError(
                    f'Unknown patch key "{key}". Patchable attributes: '
                    f"{sorted(_MUTABLE_ATTRS)}."
                )
        if "links" in patch:
            self._validate_links(patch["links"])

        data = record.model_dump()
        data.update(patch)
        if "fields" in patch:
            # `fields` MERGES; every other mutable attribute replaces wholesale.
            # The asymmetry is deliberate: replacing a list wholesale is
            # predictable, but replacing a nested map wholesale means changing
            # one key requires re-transmitting every other key verbatim, and a
            # single dropped key silently destroys it on an accepted record.
            # That footgun nearly cost a founding bet in real use.
            # An explicit None value deletes the key.
            merged = dict(record.fields)
            for field_key, field_value in (patch["fields"] or {}).items():
                if field_value is None:
                    merged.pop(field_key, None)
                else:
                    merged[field_key] = field_value
            data["fields"] = merged
        data["updated_at"] = self._now()
        updated = Record(**data)
        self._put_record(updated)
        # Bump the freshness counter: an edit to an accepted bet is exactly the
        # change a stale briefing would miss, so it must move the version.
        self._upsert_project_registry(updated.project)

        warnings: list[str] = []
        if record.status == "accepted" and set(patch) & {"fields", "body_md", "title"}:
            warnings.append(
                f'{ref} is "accepted". Editing fields/body_md/title in place is '
                "allowed, but consider dr_supersede instead to preserve the "
                "decision history."
            )
        logger.info("record.updated", ref=ref, patched_keys=list(patch), warnings=warnings)
        return updated, warnings

    # ------------------------------------------------------------------
    # dr_revise
    # ------------------------------------------------------------------

    def revise(
        self,
        ref: str,
        body_md: str,
        note: str,
        project: str | None = None,
    ) -> tuple[Record, list[str]]:
        """Replace body_md while a record's decision stands but its stated
        reasoning does not.

        The gap this closes: `dr_supersede` is too heavy when the decision
        itself hasn't changed (only the reasoning has), and `dr_update` is
        too quiet -- it rewrites `body_md` in place with only a warning,
        erasing what the record used to say. `dr_revise` appends
        `{at, note, previous_body_md}` to `revisions` -- preserving the
        exact prior text, the whole point -- before replacing `body_md`.

        Legal on any status, including "accepted", and unlike `update()`
        it never emits the accepted-record supersede warning: this IS the
        right tool for revising an accepted record's stated reasoning, not
        a workaround that needs flagging.

        Args:
            ref: Record ref, e.g. "ADR-0023".
            body_md: The new body text.
            note: Required, non-empty explanation of why the reasoning
                changed even though the decision did not. A revision with
                no note is indistinguishable from a plain edit.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the updated `Record`, an empty list of warnings --
            `dr_revise` never warns; the empty list is returned for shape
            consistency with the other mutating operations).

        Raises:
            InvalidRefError: `ref` is not well-formed.
            MissingRevisionNoteError: `note` is missing or blank.
            NoProjectError: No project could be resolved.
            RecordNotFoundError: No record exists for that ref.
        """
        if not note or not note.strip():
            raise MissingRevisionNoteError()

        record = self.get(ref, project)
        now = self._now()
        record.revisions.append(
            Revision(at=now, note=note, previous_body_md=record.body_md)
        )
        record.body_md = body_md
        record.updated_at = now
        self._put_record(record)
        self._upsert_project_registry(record.project)
        logger.info("record.revised", ref=ref, note=note)
        return record, []

    # ------------------------------------------------------------------
    # dr_set_status
    # ------------------------------------------------------------------

    def set_status(
        self,
        ref: str,
        status: Status,
        note: str | None = None,
        project: str | None = None,
    ) -> tuple[Record, list[str], list[str]]:
        """Transition a record's status, appending to `status_history`.

        Never touches `body_md`. Per spec section 4, when an IDR moves to
        "rejected" or "deprecated", the ADRs/MDRs linked to it via
        "implements-bet" are no longer governed. This method reports those
        refs as orphans — it does not cascade the status change.

        Args:
            ref: Record ref, e.g. "IDR-0001".
            status: The target status.
            note: Optional free-text note for the status_history entry.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the updated `Record`, the list of orphaned ADR/MDR
            refs — empty unless this was a governing IDR moving to
            "rejected"/"deprecated" — and a list of warning strings, which
            on a transition to "accepted" may include placeholder-lint
            warnings (spec section 4, "Placeholders and unmeasurable
            criteria") and, for an IDR carrying "requires-attestation"
            links, the commitment-gate warning (spec section 4, "The
            commitment gate")).

        Raises:
            InvalidRefError: `ref` is not well-formed.
            InvalidTransitionError: `status` is not reachable from the
                record's current status.
            NoProjectError: No project could be resolved.
            RecordNotFoundError: No record exists for that ref.
        """
        record = self.get(ref, project)
        allowed = ALLOWED_TRANSITIONS.get(record.status, set())
        if status not in allowed:
            raise InvalidTransitionError(record.status, status, allowed)

        now = self._now()
        record.status_history.append(StatusEvent(at=now, status=status, note=note))
        record.status = status
        record.updated_at = now
        self._put_record(record)
        self._upsert_project_registry(record.project)

        orphan_refs: list[str] = []
        warnings: list[str] = []

        # Complete a deferred supersession. dr_supersede links the pair but
        # leaves the predecessor's status alone, so that a register where
        # humans ratify everything never has a gap with no accepted record in
        # it. The flip lands here, when the replacement is actually accepted.
        if record.supersedes and status == "accepted":
            predecessor = self.get(record.supersedes, project)
            if predecessor.status != "superseded":
                predecessor.status_history.append(
                    StatusEvent(at=now, status="superseded", note=f"superseded by {ref}")
                )
                predecessor.status = "superseded"
                predecessor.superseded_by = ref
                predecessor.updated_at = now
                self._put_record(predecessor)
                warnings.append(
                    f"{predecessor.ref} is now superseded by {ref}, completing the "
                    "supersession deferred when the replacement was drafted."
                )
        # A rejected replacement releases its predecessor: nothing is replacing
        # it after all, so leaving the pointer would claim otherwise. The
        # rejected record keeps its own `supersedes` as a record of intent.
        elif record.supersedes and status == "rejected":
            predecessor = self.get(record.supersedes, project)
            if predecessor.superseded_by == ref and predecessor.status != "superseded":
                predecessor.superseded_by = None
                predecessor.updated_at = now
                self._put_record(predecessor)
                warnings.append(
                    f"{predecessor.ref} is no longer marked as being replaced — "
                    f"its pending replacement {ref} was rejected."
                )

        if record.rec_type == "IDR" and status in ("rejected", "deprecated"):
            orphan_refs = self._find_governed_by(record.project, ref)
            if orphan_refs:
                warnings.append(
                    f"{ref} funded {len(orphan_refs)} record(s) now orphaned — no "
                    f"longer governed by an active bet: {', '.join(orphan_refs)}. "
                    "This does not change their status; re-link them to another "
                    "IDR, deprecate them, or supersede them."
                )
        if status == "accepted":
            warnings.extend(self._placeholder_warnings(record))
            warnings.extend(self._attestation_warnings(record))
        logger.info(
            "record.status_changed",
            ref=ref,
            status=status,
            orphans=orphan_refs,
        )
        return record, orphan_refs, warnings

    def _find_governed_by(self, project: str, idr_ref: str) -> list[str]:
        """Find ADR/MDR refs linked to an IDR via "implements-bet".

        Args:
            project: Resolved project slug.
            idr_ref: The governing IDR's ref.

        Returns:
            Refs of ADRs/MDRs that link to `idr_ref` with relation
            "implements-bet".
        """
        records = self._list_all_records(project)
        return [
            r.ref
            for r in records
            if r.rec_type in ("ADR", "MDR")
            and any(
                link.relation == _GOVERNING_RELATION and link.ref == idr_ref
                for link in r.links
            )
        ]

    # ------------------------------------------------------------------
    # dr_review
    # ------------------------------------------------------------------

    def review(
        self,
        ref: str,
        verdict: Verdict,
        evidence: str,
        cause: Cause | None = None,
        note: str | None = None,
        project: str | None = None,
    ) -> tuple[Record, list[str]]:
        """Record a review outcome against a record's stated criteria.

        Appends to `reviews`; never changes `status` (see spec section 4:
        "Reviews are not status").

        Args:
            ref: Record ref, e.g. "IDR-0001".
            verdict: One of "met", "missed", "mixed", "too-early".
            evidence: Required prose: what was observed, from what source,
                at what sample.
            cause: Required when `verdict == "missed"` and the record is an
                IDR. One of "market", "delivery", "both" (see spec
                section 1).
            note: Optional free-text note.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the updated `Record`, a list of warning strings —
            currently always empty; reserved for future governance
            warnings).

        Raises:
            InvalidRefError: `ref` is not well-formed.
            MissingCauseError: `verdict == "missed"` on an IDR and `cause`
                is not supplied.
            NoProjectError: No project could be resolved.
            RecordNotFoundError: No record exists for that ref.
        """
        record = self.get(ref, project)
        if verdict == "missed" and record.rec_type == "IDR" and cause is None:
            raise MissingCauseError()

        now = self._now()
        record.reviews.append(
            Review(at=now, verdict=verdict, evidence=evidence, cause=cause, note=note)
        )
        record.updated_at = now
        self._put_record(record)
        self._upsert_project_registry(record.project)
        logger.info("record.reviewed", ref=ref, verdict=verdict, cause=cause)
        return record, []

    # ------------------------------------------------------------------
    # dr_due
    # ------------------------------------------------------------------

    def due(self, as_of: str | None = None, project: str | None = None) -> list[Record]:
        """List IDRs whose review_date has passed with no later review.

        Args:
            as_of: ISO-8601 (or plain date) timestamp to evaluate "has
                passed" against. Defaults to now.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            IDRs with `fields.review_date` on or before `as_of` and no
            `reviews` entry dated after `review_date`, sorted by
            `review_date` ascending (most overdue first).

        Raises:
            NoProjectError: No project could be resolved.
        """
        resolved_project = self.resolve_project(project)
        as_of_dt = self._parse_dt(as_of) if as_of else datetime.now(timezone.utc)
        # A retired bet has no review obligation: a superseded IDR handed its
        # obligation to the record that replaced it, and a rejected or
        # deprecated one is dead. Without this filter every supersede would
        # permanently add noise to the one report this register owes its
        # operator -- found by dogfooding, when the founding bet was superseded
        # on day one and its predecessor kept reporting as due forever.
        #
        # draft/proposed ARE still returned: a bet drafted, never decided, and
        # left past its own review date is a lapsed intention worth surfacing.
        _RETIRED = {"superseded", "deprecated", "rejected"}
        idrs = [
            r
            for r in self._list_all_records(resolved_project)
            if r.rec_type == "IDR" and r.status not in _RETIRED
        ]

        due_list: list[Record] = []
        for idr in idrs:
            review_date_raw = idr.fields.get("review_date")
            if not review_date_raw:
                continue
            review_date_dt = self._parse_dt(str(review_date_raw))
            if review_date_dt > as_of_dt:
                continue
            has_later_review = any(
                self._parse_dt(rev.at) > review_date_dt for rev in idr.reviews
            )
            if not has_later_review:
                due_list.append(idr)

        due_list.sort(key=lambda r: str(r.fields.get("review_date", "")))
        return due_list

    @staticmethod
    def _parse_dt(value: str) -> datetime:
        """Parse an ISO-8601 timestamp or plain date into an aware datetime.

        Args:
            value: An ISO-8601 timestamp (e.g. "2026-01-15T00:00:00+00:00"
                or with a "Z" suffix) or a plain date ("2026-01-15").

        Returns:
            A timezone-aware UTC datetime.
        """
        text = value.strip()
        if len(text) == 10:  # YYYY-MM-DD
            text += "T00:00:00+00:00"
        text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    # ------------------------------------------------------------------
    # dr_supersede
    # ------------------------------------------------------------------

    def adopt_successor(
        self,
        old_ref: str,
        successor_ref: str,
        project: str | None = None,
    ) -> tuple[Record, list[str]]:
        """Link an EXISTING record as the successor to `old_ref`.

        The companion to `supersede`, for the case where the replacement
        was already drafted standalone. `supersedes`/`superseded_by` are
        structural and cannot be patched via `update`, so without this a
        supersession drafted now and accepted later could never be linked
        at all — the relationship would survive only in prose and status
        notes, which no query can follow.

        Same deferred semantics as `supersede`: the pair is linked
        immediately, and `old_ref` flips to "superseded" when the successor
        is accepted (or right now, if it already is).

        Also repairs the prose-workaround state: an `old_ref` already
        marked "superseded" but carrying a null `superseded_by` simply gets
        its link filled in.

        Args:
            old_ref: Ref of the record being superseded.
            successor_ref: Ref of the existing record that replaces it.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the successor `Record`, warnings).

        Raises:
            InvalidRefError: Either ref is malformed, or they are the same
                record.
            RecordNotFoundError: Either record does not exist.
            NoProjectError: No project could be resolved.
        """
        resolved_project = self.resolve_project(project)
        if old_ref == successor_ref:
            raise InvalidRefError(f"{old_ref} cannot supersede itself.")

        old = self.get(old_ref, resolved_project)
        successor = self.get(successor_ref, resolved_project)
        now = self._now()
        warnings: list[str] = []

        if successor.supersedes and successor.supersedes != old_ref:
            warnings.append(
                f"{successor_ref} already supersedes {successor.supersedes}; "
                f"repointing it at {old_ref}."
            )
        if old.superseded_by and old.superseded_by != successor_ref:
            warnings.append(
                f"{old_ref} was already marked as replaced by "
                f"{old.superseded_by}; repointing it at {successor_ref}."
            )

        successor.supersedes = old_ref
        successor.updated_at = now
        self._put_record(successor)

        old.superseded_by = successor_ref
        old.updated_at = now
        if successor.status == "accepted" and old.status != "superseded":
            old.status_history.append(
                StatusEvent(at=now, status="superseded", note=f"superseded by {successor_ref}")
            )
            old.status = "superseded"
        elif old.status == "superseded":
            warnings.append(
                f"{old_ref} was already \"superseded\" with no recorded successor — "
                f"link repaired to {successor_ref}."
            )
        else:
            warnings.append(
                f"{old_ref} keeps its \"{old.status}\" status until "
                f"{successor_ref} is accepted."
            )
        self._put_record(old)
        self._upsert_project_registry(resolved_project)

        logger.info("record.successor_adopted", old_ref=old_ref, new_ref=successor_ref)
        return successor, warnings

    def supersede(
        self,
        old_ref: str,
        new_record: dict[str, Any],
        project: str | None = None,
    ) -> tuple[Record, list[str]]:
        """Create a superseding record and link it to the old one.

        Creates the new record and sets `supersedes`/`superseded_by` both
        ways via a single `TransactWriteItems` call. The old record KEEPS
        its status until the replacement is accepted — see the deferred-flip
        note in the body. Use `adopt_successor` when the replacement
        already exists.

        Args:
            old_ref: Ref of the record being superseded.
            new_record: New record content: must include "title"; may
                include "rec_type" (defaults to `old_ref`'s type),
                "body_md", "fields", "tags", "links", "deciders", "status"
                (defaults to "proposed").
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the newly created `Record`, a list of non-blocking
            governance warning strings for the new record).

        Raises:
            InvalidRefError: `old_ref`, or a link ref in `new_record`, is
                not well-formed.
            ValueError: `new_record` is missing "title".
            NoProjectError: No project could be resolved.
            RecordNotFoundError: `old_ref` does not exist.
            TableMissingError: The DynamoDB table does not exist.
            CredentialsError: AWS credentials are missing or expired.
        """
        resolved_project = self.resolve_project(project)
        old = self.get(old_ref, resolved_project)

        if not new_record.get("title"):
            raise ValueError('new_record must include "title".')

        rec_type: RecType = new_record.get("rec_type", old.rec_type)
        links_obj = self._validate_links(new_record.get("links") or [])
        seq = self._allocate_seq(resolved_project, rec_type)
        ref = f"{rec_type}-{seq:04d}"
        now = self._now()
        new_status: Status = new_record.get("status", "proposed")

        new = Record(
            id=str(ULID()),
            project=resolved_project,
            rec_type=rec_type,
            seq=seq,
            ref=ref,
            title=new_record["title"],
            status=new_status,
            persona=PERSONA_BY_TYPE[rec_type],
            created_at=now,
            updated_at=now,
            deciders=new_record.get("deciders") or [],
            tags=new_record.get("tags") or [],
            links=links_obj,
            supersedes=old_ref,
            superseded_by=None,
            status_history=[
                StatusEvent(at=now, status=new_status, note=f"created (supersedes {old_ref})")
            ],
            reviews=[],
            fields=new_record.get("fields") or {},
            body_md=new_record.get("body_md", ""),
        )

        # DEFERRED FLIP. The old record gets its `superseded_by` link now, so
        # the chain is machine-followable immediately, but it KEEPS its status
        # until the replacement is actually accepted. Under default-deny every
        # record is human-ratified, so flipping on creation would leave a gap
        # with no accepted record at all -- the old one retired and the new one
        # only proposed. dr_set_status(new_ref, "accepted") performs the flip.
        #
        # The exception is a replacement created already-accepted: there is
        # nothing left to wait for, so flip in the same transaction.
        _flip_now = new_status == "accepted"
        old_status_event = StatusEvent(
            at=now,
            status="superseded" if _flip_now else old.status,
            note=(
                f"superseded by {ref}"
                if _flip_now
                else f"replacement {ref} proposed; supersedes on its acceptance"
            ),
        ).model_dump()

        # NOTE: self._resource.meta.client carries boto3's DynamoDB resource
        # auto-transform hooks (the same ones that let Table.put_item accept
        # native Python types instead of wire-format AttributeValues). Items
        # and expression values below are therefore given as plain Python
        # values, not manually serialized -- manually serializing here would
        # double-encode them through those hooks.
        transact_items = [
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": self._to_item(new),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": {
                        "pk": self._project_pk(resolved_project),
                        "sk": self._record_sk(old.rec_type, old.seq),
                    },
                    "UpdateExpression": (
                        (
                            "SET #status = :status, #superseded_by = :superseded_by, "
                            if _flip_now
                            else "SET #superseded_by = :superseded_by, "
                        )
                        + "#updated_at = :updated_at, "
                        "#status_history = list_append(#status_history, :event)"
                    ),
                    "ConditionExpression": "attribute_exists(pk)",
                    "ExpressionAttributeNames": {
                        **({"#status": "status"} if _flip_now else {}),
                        "#superseded_by": "superseded_by",
                        "#updated_at": "updated_at",
                        "#status_history": "status_history",
                    },
                    "ExpressionAttributeValues": {
                        **({":status": "superseded"} if _flip_now else {}),
                        ":superseded_by": ref,
                        ":updated_at": now,
                        ":event": [old_status_event],
                    },
                }
            },
        ]

        self._call(self._resource.meta.client.transact_write_items, TransactItems=transact_items)
        warnings = self._create_warnings(new)
        logger.info("record.superseded", old_ref=old_ref, new_ref=ref)
        return new, warnings

    # ------------------------------------------------------------------
    # dr_search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        rec_type: RecType | None = None,
        all_projects: bool = False,
        project: str | None = None,
    ) -> list[tuple[Record, int]]:
        """Tokenized, OR-of-terms search over title, tags, body, fields.

        `query` is split on whitespace into terms; a record matches if it
        contains ANY term (case-insensitive substring, per term) anywhere
        in `title`, `tags`, `body_md`, or the string form of any `fields`
        value (so `user_evidence` is searchable). This is deliberately NOT
        a single 60-character literal substring match -- a multi-term
        query like "shared credentials runner service account deploy
        authentication SAML" must find a record containing "service
        account" even though the record never contains that exact
        seven-word phrase.

        Args:
            query: Search terms, split on whitespace (case-insensitive).
            rec_type: Filter to one record type.
            all_projects: If True, search across all projects via Scan
                instead of a single project's Query.
            project: Explicit project slug; falls back to `DR_PROJECT`.
                Ignored when `all_projects` is True.

        Returns:
            `(record, matched_term_count)` pairs for every record matching
            at least one term, ranked: an exact match on the full `query`
            phrase first, then by `matched_term_count` (the number of
            DISTINCT terms matched) descending, then by `created_at`
            descending. A single-term query has only one possible
            `matched_term_count` value (0 or 1) and its exact-phrase flag
            is identical to its term match, so ranking for that case
            collapses to plain `created_at` descending -- unchanged from
            before this method tokenized.

        Raises:
            NoProjectError: `all_projects` is False and no project could
                be resolved.
        """
        query_lower = query.lower().strip()
        terms = self._tokenize_query(query)
        if all_projects:
            items = self._scan_all(FilterExpression=Attr("sk").begins_with("REC#"))
            records = [self._from_item(item) for item in items]
        else:
            resolved_project = self.resolve_project(project)
            records = self._list_all_records(resolved_project)

        if rec_type is not None:
            records = [r for r in records if r.rec_type == rec_type]
        if not terms:
            return []

        scored: list[tuple[Record, int, bool]] = []
        for record in records:
            count = self._matched_term_count(record, terms)
            if count == 0:
                continue
            exact = self._matches_query(record, query_lower)
            scored.append((record, count, exact))

        # Three stable sorts, lowest priority first: the LAST sort wins ties
        # from the ones before it, so this produces exact-match DESC, then
        # matched_term_count DESC, then created_at DESC.
        scored.sort(key=lambda item: item[0].created_at, reverse=True)
        scored.sort(key=lambda item: item[1], reverse=True)
        scored.sort(key=lambda item: item[2], reverse=True)
        return [(record, count) for record, count, _exact in scored]

    @staticmethod
    def _tokenize_query(query: str) -> list[str]:
        """Split a search query into lowercase, whitespace-separated terms.

        Args:
            query: The raw search query.

        Returns:
            Lowercased terms in first-seen order, with duplicates removed
            -- ranking counts DISTINCT terms matched, so the term list
            itself must already be distinct.
        """
        terms: list[str] = []
        for term in query.lower().split():
            if term not in terms:
                terms.append(term)
        return terms

    @staticmethod
    def _record_search_haystacks(record: Record) -> list[str]:
        """Return every searchable string on a record, lowercased.

        Args:
            record: The record to build haystacks for.

        Returns:
            `title`, `body_md`, every tag, and the string form of every
            `fields` value -- the search surface `dr_search` documents --
            all already lowercased so callers do per-term work only once.
        """
        haystacks = [record.title, record.body_md, *record.tags]
        haystacks.extend(str(value) for value in record.fields.values())
        return [h.lower() for h in haystacks]

    @classmethod
    def _matched_term_count(cls, record: Record, terms: list[str]) -> int:
        """Count how many distinct query terms appear anywhere on a record.

        Args:
            record: The record to check.
            terms: Already-lowercased, already-deduplicated query terms
                (as returned by `_tokenize_query`).

        Returns:
            The number of terms for which at least one haystack contains
            that term as a substring.
        """
        haystacks = cls._record_search_haystacks(record)
        return sum(1 for term in terms if any(term in haystack for haystack in haystacks))

    @staticmethod
    def _matches_query(record: Record, query_lower: str) -> bool:
        """Check whether a record matches a lowercased search substring.

        Used for two purposes: `dr_search`'s exact-full-phrase ranking
        signal (called with the whole, un-tokenized query), and as the
        general single-substring matcher other call sites can reuse.

        Args:
            record: The record to check.
            query_lower: Already-lowercased search substring.

        Returns:
            True if `query_lower` appears in title, tags, body_md, or any
            `fields` value.
        """
        if query_lower in record.title.lower():
            return True
        if any(query_lower in tag.lower() for tag in record.tags):
            return True
        if query_lower in record.body_md.lower():
            return True
        return any(query_lower in str(value).lower() for value in record.fields.values())

    # ------------------------------------------------------------------
    # dr_projects
    # ------------------------------------------------------------------

    def projects(self) -> list[dict[str, Any]]:
        """List registry entries with per-type record counts.

        Works without `DR_PROJECT` — this is a cross-project read.

        Returns:
            A list of ``{"project": ..., "created_at": ..., "record_counts":
            {"ADR": n, "IDR": n, "MDR": n}}`` dicts, sorted by project slug.
        """
        items = self._query_all(KeyConditionExpression=Key("pk").eq("META#PROJECTS"))
        results: list[dict[str, Any]] = []
        for item in items:
            slug = item["sk"].removeprefix("PROJ#")
            results.append(
                {
                    "project": slug,
                    "created_at": item.get("created_at"),
                    "record_counts": self._record_counts(slug),
                }
            )
        results.sort(key=lambda p: p["project"])
        return results

    def _record_counts(self, project: str) -> dict[str, int]:
        """Count records by type within a project.

        Args:
            project: Resolved project slug.

        Returns:
            ``{"ADR": n, "IDR": n, "MDR": n}``.
        """
        counts = {"ADR": 0, "IDR": 0, "MDR": 0}
        for record in self._list_all_records(project):
            counts[record.rec_type] += 1
        return counts

    # ------------------------------------------------------------------
    # dr_escalate
    # ------------------------------------------------------------------

    def escalate(
        self,
        question: str,
        context: str,
        options: list[dict[str, Any]] | list[EscalationOption],
        recommendation: str,
        cost_of_delay: str,
        blocking: bool = False,
        governing_ref: str | None = None,
        raised_by: str | None = None,
        project: str | None = None,
    ) -> tuple[Escalation, list[str]]:
        """File a decision the operator owes.

        The one tool an agent may call unprompted (spec section 1): default-
        deny means an automated worker will routinely reach a point it is
        not authorized to pass, and this is where it goes instead of
        stopping silently or deciding anyway. Allocates an ESC seq
        atomically (same `ADD next_seq :one` counter mechanism as
        ADR/IDR/MDR, just its own per-project counter), writes the item
        with `status="open"`, and upserts the project registry.

        Never blocks on governance grounds — see "Warn, never block" in
        spec section 4. A blocked worker must always be able to file, so
        thin escalations (no options, no recommendation, no governing_ref)
        succeed and simply warn.

        Args:
            question: One sentence: what must be decided.
            context: Why this came up; what was being attempted.
            options: Alternatives as ``[{"option": ..., "consequence": ...,
                "reversible": bool}, ...]`` or already-built
                `EscalationOption` models. Fewer than 2 warns but does not
                block.
            recommendation: Which option the agent would take, and why.
                Empty warns but does not block.
            cost_of_delay: What happens if this waits. Empty warns but does
                not block.
            blocking: True if work is actually stopped; False if this is a
                flag for later.
            governing_ref: The IDR this work sits under, if any. Must be a
                well-formed ref if given; existence is not verified —
                forward references are legal, same as links. Omitting it
                warns but does not block.
            raised_by: Agent identity plus what it was doing when it
                stopped. Defaults to "unspecified" if omitted, so filing
                never fails for want of an identity string.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the created `Escalation`, a list of non-blocking
            governance warning strings).

        Raises:
            NoProjectError: No project could be resolved.
            InvalidRefError: `governing_ref` is set and malformed.
            TableMissingError: The DynamoDB table does not exist.
            CredentialsError: AWS credentials are missing or expired.
        """
        resolved_project = self.resolve_project(project)
        if governing_ref is not None:
            self._parse_ref(governing_ref)

        options_obj = [
            option if isinstance(option, EscalationOption) else EscalationOption(**option)
            for option in options
        ]

        seq = self._allocate_seq(resolved_project, "ESC")
        ref = f"ESC-{seq:04d}"
        now = self._now()
        escalation = Escalation(
            ref=ref,
            project=resolved_project,
            raised_by=raised_by or "unspecified",
            raised_at=now,
            question=question,
            context=context,
            options=options_obj,
            recommendation=recommendation,
            cost_of_delay=cost_of_delay,
            blocking=blocking,
            governing_ref=governing_ref,
            status="open",
            answered_at=None,
            resolution=None,
            produced_ref=None,
        )
        self._put_escalation(escalation)
        self._upsert_project_registry(resolved_project)
        warnings = self._escalate_warnings(escalation)
        logger.info(
            "escalation.filed",
            ref=ref,
            project=resolved_project,
            blocking=blocking,
            warnings=warnings,
        )
        return escalation, warnings

    # ------------------------------------------------------------------
    # dr_escalations
    # ------------------------------------------------------------------

    def escalations(
        self,
        status: EscalationStatus = "open",
        blocking_only: bool = False,
        unrecorded: bool = False,
        limit: int = 50,
        project: str | None = None,
    ) -> list[Escalation]:
        """List a project's escalation queue — the operator's inbox.

        Project-scoped ONLY: there is deliberately no `all_projects`
        parameter (spec sections 1 and 5, item 7b). An agent escalates
        within the project it is working in and the operator answers
        within that project; cross-project visibility is the scheduled
        sweep's job, not a tool call's. This keeps the read a single
        partition Query, never a Scan.

        Args:
            status: Filter to one escalation status. Defaults to "open".
                Ignored when `unrecorded` is True -- see below.
            blocking_only: If True, return only escalations with
                `blocking=True`.
            unrecorded: If True, the sweep for "no record, no decision":
                return escalations that are `status="answered"` AND
                `produced_ref is None` AND `no_record is False` -- answered,
                left no record, and not deliberately so. This overrides
                `status` (an unrecorded escalation is definitionally
                "answered"; passing a different `status` alongside it would
                just produce an always-empty result), but still composes
                with `blocking_only` exactly the way that filter composes
                with `status` normally.
            limit: Maximum number of escalations to return, applied after
                sorting.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            Matching escalations, ordered `blocking` DESC, then `raised_at`
            DESCENDING (newest first) — the same recency direction as
            `list()`, so the two listings in this store never disagree about
            what "first" means. Blocking items float to the top because that
            is what the flag is for; within each group it is plain
            newest-first. Age is visible in `raised_at` for a caller that
            cares about the longest-stalled item.

        Raises:
            NoProjectError: No project could be resolved.
        """
        resolved_project = self.resolve_project(project)
        all_escalations = self._list_all_escalations(resolved_project)
        if unrecorded:
            matched = [
                escalation
                for escalation in all_escalations
                if escalation.status == "answered"
                and escalation.produced_ref is None
                and not escalation.no_record
            ]
        else:
            matched = [
                escalation for escalation in all_escalations if escalation.status == status
            ]
        if blocking_only:
            matched = [escalation for escalation in matched if escalation.blocking]
        # blocking DESC (True before False), then raised_at DESC (newest first
        # within each blocking group) -- the SAME recency direction as list(),
        # so the two listings in this store never disagree about what "first"
        # means. Two passes, relying on Python's stable sort: order by recency,
        # then float the blocking items up without disturbing it.
        matched.sort(key=lambda escalation: escalation.raised_at, reverse=True)
        matched.sort(key=lambda escalation: not escalation.blocking)
        return matched[:limit]

    # ------------------------------------------------------------------
    # dr_answer
    # ------------------------------------------------------------------

    def answer(
        self,
        ref: str,
        resolution: str,
        produces: dict[str, Any] | None = None,
        no_record: bool = False,
        project: str | None = None,
    ) -> tuple[Escalation, Record | None, list[str]]:
        """Resolve an escalation.

        Sets `status="answered"`, stamps `answered_at`, and stores
        `resolution`. When `produces` is given, the resulting ADR/IDR/MDR
        is created and the escalation's `produced_ref` is set to it in the
        same `TransactWriteItems` call, so the record and the escalation
        update land together or neither does — following the same atomic
        pattern as `supersede()`. When `produces` is omitted, `produced_ref`
        stays `None`: "don't do that" is a complete answer that needs no
        record.

        "No record, no decision": when `produces` is omitted and
        `no_record` is False, the resolution now exists only inside this
        escalation, and nothing that reads the register (`dr_list`,
        `dr_search`, `dr_export`) will ever find it — an ADR/IDR/MDR
        answer made correctly can be as invisible to a later reader as one
        that was simply never written down. This warns, naming both
        remedies, but never blocks — per the register's "warn, never
        block" posture. Passing `no_record=True` records that skipping a
        record was a deliberate call, not an oversight, and suppresses the
        warning; `escalations(unrecorded=True)` is the sweep that finds
        everything that got neither.

        Args:
            ref: Escalation ref, e.g. "ESC-0007".
            resolution: The operator's decision, in their own words.
            produces: Optional `create()`-style kwargs (`rec_type`,
                `title`, and optionally `body_md`, `fields`, `tags`,
                `links`, `deciders`, `status`) used to create the resulting
                record in the same atomic write.
            no_record: True to confirm that this resolution deliberately
                needs no record — e.g. "don't do that" is complete on its
                own. Suppresses the no-record warning below. Ignored (has
                no effect either way) when `produces` is given.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the updated `Escalation`, the created `Record` or
            `None` if `produces` was omitted, a list of non-blocking
            warning strings — governance warnings for the created record
            when one was produced, or the no-record warning when neither
            `produces` nor `no_record` was given).

        Raises:
            InvalidRefError: `ref` is not well-formed, or a link ref within
                `produces["links"]` is not well-formed.
            NoProjectError: No project could be resolved.
            EscalationNotFoundError: No escalation exists for that ref.
            EscalationAlreadyResolvedError: The escalation's status is not
                "open".
            ValueError: `produces` is given without a "rec_type" or
                "title".
            TableMissingError: The DynamoDB table does not exist.
            CredentialsError: AWS credentials are missing or expired.
        """
        resolved_project = self.resolve_project(project)
        escalation = self._get_escalation(ref, resolved_project)
        if escalation.status != "open":
            raise EscalationAlreadyResolvedError(
                ref, escalation.status, escalation.resolution, escalation.answered_at
            )

        now = self._now()
        new_record: Record | None = None
        warnings: list[str] = []
        produced_ref: str | None = None
        transact_items: list[dict[str, Any]] = []

        if produces is not None:
            if not produces.get("rec_type"):
                raise ValueError('produces must include "rec_type".')
            if not produces.get("title"):
                raise ValueError('produces must include "title".')

            rec_type: RecType = produces["rec_type"]
            links_obj = self._validate_links(produces.get("links") or [])
            seq = self._allocate_seq(resolved_project, rec_type)
            record_ref = f"{rec_type}-{seq:04d}"
            record_status: Status = produces.get("status", "proposed")

            new_record = Record(
                id=str(ULID()),
                project=resolved_project,
                rec_type=rec_type,
                seq=seq,
                ref=record_ref,
                title=produces["title"],
                status=record_status,
                persona=PERSONA_BY_TYPE[rec_type],
                created_at=now,
                updated_at=now,
                deciders=produces.get("deciders") or [],
                tags=produces.get("tags") or [],
                links=links_obj,
                supersedes=None,
                superseded_by=None,
                status_history=[
                    StatusEvent(
                        at=now, status=record_status, note=f"created (resolves {ref})"
                    )
                ],
                reviews=[],
                fields=produces.get("fields") or {},
                body_md=produces.get("body_md", ""),
            )
            produced_ref = record_ref
            warnings = self._create_warnings(new_record)
            transact_items.append(
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": self._to_item(new_record),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            )
        elif not no_record:
            # "No record, no decision": produces was omitted and no_record
            # was never set, so this resolution is about to exist ONLY as
            # prose inside this escalation -- nothing that reads the
            # register will ever find it. Warns, never blocks (see the
            # docstring above); names both remedies so the fix is a single
            # follow-up call either way.
            warnings.append(
                f"{ref} was answered with no produced record and no_record "
                "is not set. The decision now exists only inside this "
                "escalation -- nothing that reads the register (dr_list, "
                "dr_search, dr_export) will ever find it. Two remedies: "
                "pass produces=... on the dr_answer call that resolves an "
                "escalation, so the record is created and linked in the "
                "same atomic write (an escalation answers only once, so "
                "this cannot be added after the fact -- a record still "
                "worth writing now needs its own dr_create, referencing "
                f'"{ref}" in its body); or, when no record is genuinely '
                "needed, pass no_record=True on that same dr_answer call "
                "to record the omission as deliberate rather than an "
                "oversight."
            )

        # See the NOTE in supersede() above: plain Python values here, not
        # hand-serialized AttributeValues -- the resource-layer transform
        # hooks on self._resource.meta.client handle that, and manual
        # serialization would double-encode.
        transact_items.append(
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": {
                        "pk": self._project_pk(resolved_project),
                        "sk": self._escalation_sk(self._parse_escalation_ref(ref)),
                    },
                    "UpdateExpression": (
                        "SET #status = :status, #answered_at = :answered_at, "
                        "#resolution = :resolution, #produced_ref = :produced_ref, "
                        "#no_record = :no_record"
                    ),
                    "ConditionExpression": "attribute_exists(pk)",
                    "ExpressionAttributeNames": {
                        "#status": "status",
                        "#answered_at": "answered_at",
                        "#resolution": "resolution",
                        "#produced_ref": "produced_ref",
                        "#no_record": "no_record",
                    },
                    "ExpressionAttributeValues": {
                        ":status": "answered",
                        ":answered_at": now,
                        ":resolution": resolution,
                        ":produced_ref": produced_ref,
                        ":no_record": no_record,
                    },
                }
            }
        )

        self._call(
            self._resource.meta.client.transact_write_items, TransactItems=transact_items
        )

        updated_escalation = escalation.model_copy(
            update={
                "status": "answered",
                "answered_at": now,
                "resolution": resolution,
                "produced_ref": produced_ref,
                "no_record": no_record,
            }
        )
        logger.info(
            "escalation.answered", ref=ref, produced_ref=produced_ref, no_record=no_record
        )
        return updated_escalation, new_record, warnings

    # ------------------------------------------------------------------
    # dr_withdraw
    # ------------------------------------------------------------------

    def withdraw(
        self,
        ref: str,
        reason: str,
        duplicate_of: str | None = None,
        project: str | None = None,
    ) -> tuple[Escalation, list[str]]:
        """Close an open escalation as moot -- no decision was needed.

        The escalation queue has three honest endings (spec section 1,
        "Escalation item"): open, answered, and withdrawn. Before this
        method existed, "withdrawn" was a declared status nothing could
        ever set -- `answer()` was the only exit from "open", so an
        escalation that was never really a decision (already resolved
        elsewhere, overtaken by events, a duplicate of another escalation)
        had to be run through `answer()` with a throwaway resolution just
        to clear it. That made it indistinguishable from a genuine
        decision that leaked without a record, which corrupted the
        `escalations(unrecorded=True)` sweep this register relies on to
        catch real leaks -- in production, 25 of 26 answered escalations
        produced no record, and some unknown share of those were actually
        moot items with nowhere else to go.

        `withdraw()` sets `status="withdrawn"`, stamps `withdrawn_at`, and
        stores `withdrawn_reason`. A withdrawn escalation owes no record,
        so `escalations(unrecorded=True)` -- which only ever matches
        `status="answered"` -- never includes it.

        This is NOT the way to close something you decided. If a decision
        was actually made, use `answer()` (with `produces` when the
        decision is worth keeping). `withdraw()` means no decision was
        needed at all.

        Args:
            ref: Escalation ref, e.g. "ESC-0007".
            reason: Required, non-empty explanation of why the question
                became moot. A withdrawal with no reason is
                indistinguishable from an abandoned escalation -- may be
                brief when `duplicate_of` is given, since the link itself
                carries the meaning.
            duplicate_of: Ref of another escalation this one duplicates,
                if any. Unlike record links, existence IS verified here --
                the point is redirecting a reader to where the decision
                actually lives, so a forward reference to nothing would
                defeat that. Must be a well-formed "ESC-\\d{4}" ref, must
                not equal `ref` (no self-reference), and must resolve to
                an existing escalation.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            A tuple of (the updated `Escalation`, a list of non-blocking
            warning strings -- currently only the duplicate-of-a-withdrawn
            -duplicate chain warning, which names the ultimate target).

        Raises:
            InvalidRefError: `ref` is not well-formed; `duplicate_of` is
                given and is not well-formed; or `duplicate_of` equals
                `ref` (self-reference).
            MissingWithdrawalReasonError: `reason` is missing or blank.
            NoProjectError: No project could be resolved.
            EscalationNotFoundError: No escalation exists for `ref`, or
                `duplicate_of` is given and no escalation exists for it.
            EscalationAlreadyResolvedError: The escalation's status is not
                "open" -- naming the current status and, if answered, its
                resolution. An answered escalation recorded a decision
                that was actually made; withdrawing it would erase that.
        """
        if not reason or not reason.strip():
            raise MissingWithdrawalReasonError()

        resolved_project = self.resolve_project(project)
        escalation = self._get_escalation(ref, resolved_project)
        if escalation.status != "open":
            raise EscalationAlreadyResolvedError(
                ref,
                escalation.status,
                escalation.resolution,
                escalation.answered_at,
                action="withdrawn",
            )

        warnings: list[str] = []
        validated_duplicate_of: str | None = None
        if duplicate_of is not None:
            self._parse_escalation_ref(duplicate_of)
            if duplicate_of == ref:
                raise InvalidRefError(
                    f"{ref} cannot be marked as a duplicate of itself."
                )
            target = self._get_escalation(duplicate_of, resolved_project)
            validated_duplicate_of = duplicate_of
            if target.status == "withdrawn" and target.duplicate_of is not None:
                ultimate_ref = self._resolve_duplicate_target(target, resolved_project)
                warnings.append(
                    f"{duplicate_of} is itself withdrawn as a duplicate of "
                    f"{ultimate_ref}. Pointing {ref} at {duplicate_of} means a "
                    "reader has to hop twice to reach where the decision "
                    f"actually lives, and the second hop may be dead -- point "
                    f"{ref} at {ultimate_ref} directly instead."
                )

        now = self._now()
        updated = escalation.model_copy(
            update={
                "status": "withdrawn",
                "withdrawn_at": now,
                "withdrawn_reason": reason,
                "duplicate_of": validated_duplicate_of,
            }
        )
        self._put_escalation(updated)
        self._upsert_project_registry(resolved_project)
        logger.info(
            "escalation.withdrawn",
            ref=ref,
            duplicate_of=validated_duplicate_of,
            warnings=warnings,
        )
        return updated, warnings

    def _resolve_duplicate_target(self, escalation: Escalation, project: str) -> str:
        """Follow a chain of `duplicate_of` links to its final target.

        Args:
            escalation: The escalation to start following the chain from
                (typically the immediate `duplicate_of` target of a new
                withdrawal).
            project: Resolved project slug.

        Returns:
            The ref of the last escalation in the chain -- the first one
            encountered that is not itself withdrawn as a duplicate.
        """
        current = escalation
        seen = {current.ref}
        while current.status == "withdrawn" and current.duplicate_of is not None:
            if current.duplicate_of in seen:
                break  # defensive only: a cycle should be unreachable in practice,
                # since duplicate_of can only ever point at an already-existing
                # escalation at the time it is set.
            seen.add(current.duplicate_of)
            current = self._get_escalation(current.duplicate_of, project)
        return current.ref
