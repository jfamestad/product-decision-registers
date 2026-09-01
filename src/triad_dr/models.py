"""Pydantic data models for the decision registers store.

Defines the three record types (ADR, IDR, MDR), their shared lifecycle,
and the small value objects (links, status events, reviews) that compose
a `Record`. See `decision-registers-mcp-spec.md` section 4 for the
authoritative data model this module implements.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RecType = Literal["ADR", "IDR", "MDR"]
"""The three decision record types: Architecture, Investment, Marketing."""

Status = Literal["draft", "proposed", "accepted", "rejected", "superseded", "deprecated"]
"""Shared status lifecycle across all record types."""

Verdict = Literal["met", "missed", "mixed", "too-early"]
"""Outcome of a `dr_review` against a record's stated criteria."""

Cause = Literal["market", "delivery", "both"]
"""Attribution for a missed IDR verdict.

market: the intent was delivered as specified and the market rejected it.
    Seller accountability; the thesis is disproven.
delivery: the built thing did not match the experience the MDRs specified.
    Builder accountability; the bet is untested, not disproven.
both: some of each. The bet stays untested until the delivery gap closes.
"""

Persona = Literal["builder", "investor", "seller"]
"""The accountable persona for a record, derived from its rec_type."""

EscalationStatus = Literal["open", "answered", "withdrawn"]
"""Lifecycle of an escalation queue item (spec section 1, "Escalation item")."""

PERSONA_BY_TYPE: dict[RecType, Persona] = {
    "ADR": "builder",
    "IDR": "investor",
    "MDR": "seller",
}
"""Maps each record type to its owning persona (spec section 1)."""

ALLOWED_TRANSITIONS: dict[Status, set[Status]] = {
    "draft": {"proposed"},
    "proposed": {"accepted", "rejected"},
    "accepted": {"superseded", "deprecated"},
    "rejected": set(),
    "superseded": set(),
    "deprecated": set(),
}
"""Shared status lifecycle (spec section 4).

draft -> proposed -> accepted -> (superseded | deprecated); proposed -> rejected.
"""


class Link(BaseModel):
    """A cross-type, same-project reference from one record to another.

    Attributes:
        ref: Target record ref, e.g. "IDR-0001". Must match
            ``^(ADR|IDR|MDR)-\\d{4}$``. Existence is not verified — forward
            references are legal.
        relation: Free-text relation label, e.g. "implements-bet".
    """

    ref: str
    relation: str


class StatusEvent(BaseModel):
    """One entry in a record's machine-written `status_history`.

    Attributes:
        at: ISO-8601 timestamp of the transition.
        status: The status transitioned to.
        note: Optional free-text note supplied with the transition.
    """

    at: str
    status: Status
    note: str | None = None


class Review(BaseModel):
    """One entry in a record's machine-written `reviews` list.

    Attributes:
        at: ISO-8601 timestamp of the review.
        verdict: Outcome against the record's stated criteria.
        evidence: Required prose describing what was observed, from what
            source, at what sample.
        cause: Required when verdict is "missed" on an IDR; see `Cause`.
        note: Optional free-text note.
    """

    at: str
    verdict: Verdict
    evidence: str
    cause: Cause | None = None
    note: str | None = None


class Revision(BaseModel):
    """One entry in a record's machine-written `revisions` list (`dr_revise`).

    Distinct from `dr_supersede` (the decision itself changes -- a new
    record) and from a plain `dr_update` (silently rewrites prose with only
    a warning): a revision is for when the decision stands but the reasoning
    written down for it no longer does, e.g. an ADR that justified itself
    with a benefit a later ADR removed.

    Attributes:
        at: ISO-8601 timestamp of the revision.
        note: Required, non-empty explanation of why the reasoning changed.
            A revision with no explanation is indistinguishable from a
            plain edit -- explaining why is the entire point of this tool
            over `dr_update`.
        previous_body_md: The record's `body_md` exactly as it read before
            this revision replaced it. This is what makes a revision able
            to show what the reasoning used to be.
    """

    at: str
    note: str
    previous_body_md: str


class Record(BaseModel):
    """A single decision record (ADR, IDR, or MDR).

    See spec section 4 for the authoritative attribute list. `status_history`
    and `reviews` are machine-written (via `dr_set_status` / `dr_review`
    equivalents on the store); `body_md` is authored prose and is never
    machine-mutated.

    Attributes:
        id: Stable internal ULID.
        project: Project namespace slug.
        rec_type: One of "ADR", "IDR", "MDR".
        seq: Per-project, per-type sequence number.
        ref: Derived human ref, e.g. "ADR-0007".
        title: Imperative title, e.g. "Use DynamoDB single-table design".
        status: Current lifecycle status.
        persona: Owning persona, derived from rec_type.
        created_at: ISO-8601 creation timestamp.
        updated_at: ISO-8601 last-update timestamp.
        deciders: Names/identifiers of who decided.
        tags: Free-text tags.
        links: Cross-type, same-project links.
        supersedes: Ref of the record this one supersedes, if any.
        superseded_by: Ref of the record that superseded this one, if any.
        status_history: Machine-written transition log.
        reviews: Machine-written review log.
        revisions: Machine-written log of `dr_revise` calls -- each entry
            preserves the `body_md` text it replaced, alongside a required
            note explaining why the reasoning changed. See `Revision`.
        fields: Type-specific structured fields (see spec section 4 templates).
        body_md: Full markdown body; canonical prose, never machine-mutated
            except by `dr_revise` (which preserves what it replaces).
    """

    id: str
    project: str
    rec_type: RecType
    seq: int
    ref: str
    title: str
    status: Status
    persona: Persona
    created_at: str
    updated_at: str
    deciders: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None
    status_history: list[StatusEvent] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    revisions: list[Revision] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)
    body_md: str = ""


class EscalationOption(BaseModel):
    """One alternative offered in an escalation's `options` list.

    Attributes:
        option: The alternative itself, e.g. "Proceed with plan A".
        consequence: What happens if this option is chosen.
        reversible: Whether choosing this option is a two-way door.
    """

    option: str
    consequence: str
    reversible: bool


class Escalation(BaseModel):
    """A decision the operator owes (spec section 1, "The escalation queue").

    Not a fourth register — an escalation is not a decision, it is a
    decision that is owed. Filed by an agent that has hit the edge of its
    delegated authority (or genuine, material ambiguity) and cannot proceed
    without an operator call. See spec section 1 for the full rationale and
    the "answerable by an un-initiated leader" bar this item shape exists
    to force.

    Attributes:
        ref: Derived human ref, e.g. "ESC-0007". Per-project sequence,
            own counter (independent of ADR/IDR/MDR counters).
        project: Project namespace slug.
        raised_by: Agent identity plus what it was doing when it stopped.
        raised_at: ISO-8601 timestamp the escalation was filed.
        question: One sentence: what must be decided.
        context: Why this came up; what was being attempted.
        options: Alternatives, each with a consequence and reversibility.
        recommendation: Which option the agent would take, and why.
        cost_of_delay: What happens if this waits — "nothing until Friday"
            is a fine answer.
        blocking: True if work is actually stopped; False if this is a
            flag for later. The field that earns its keep — a stopped
            worker and a note-for-later are different emergencies.
        governing_ref: The IDR this work sits under, if any. Existence is
            not verified — forward references are legal, same as links.
        status: "open" | "answered" | "withdrawn".
        answered_at: ISO-8601 timestamp the resolution was recorded, if
            answered.
        resolution: The operator's decision, in their own words, if
            answered. "Don't do that" is a complete answer.
        produced_ref: The ADR/IDR/MDR the resolution created, if any.
            Omitted resolutions ("don't do that") need no record.
        no_record: True when the operator has deliberately confirmed that
            this resolution needs no record -- distinct from simply
            forgetting to write one. `produced_ref is None` alone cannot
            tell those two states apart; this flag is what can. Set via
            `dr_answer(no_record=True)`.
        withdrawn_at: ISO-8601 timestamp the withdrawal was recorded, if
            withdrawn. Set via `dr_withdraw`.
        withdrawn_reason: Why the question became moot, if withdrawn.
            Required and non-empty on every withdrawal -- a withdrawal
            with no reason is indistinguishable from an abandoned
            escalation, and telling those apart is the entire point of
            `dr_withdraw` existing as a real exit distinct from
            `dr_answer`.
        duplicate_of: Ref of another escalation this one duplicates, if
            any. Settable only via `dr_withdraw`. Unlike `links`, existence
            IS verified -- a duplicate must point at something real, since
            the point is redirecting a reader to where the decision
            actually lives.
    """

    ref: str
    project: str
    raised_by: str
    raised_at: str
    question: str
    context: str
    options: list[EscalationOption] = Field(default_factory=list)
    recommendation: str
    cost_of_delay: str
    blocking: bool = False
    governing_ref: str | None = None
    status: EscalationStatus
    answered_at: str | None = None
    resolution: str | None = None
    produced_ref: str | None = None
    no_record: bool = False
    withdrawn_at: str | None = None
    withdrawn_reason: str | None = None
    duplicate_of: str | None = None
