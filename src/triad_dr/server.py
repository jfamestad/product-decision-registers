"""MCP tool layer for the decision registers store ("triad-dr").

Exposes the seventeen `dr_*` tools defined in `decision-registers-mcp-spec.md`
section 5, plus `dr_withdraw` (the second honest exit from an open
escalation, alongside `dr_answer`) and two IDR-0003 events-queue tools
(`dr_events_pending`, `dr_events_ack`), over stdio via the `mcp` package's
`MCPServer`. Every
record/escalation tool here is a thin adapter: validate/marshal arguments,
call the matching `Store` method, shape the JSON response, and surface
whatever warnings `Store` already computed. No business logic — sequencing,
status-lifecycle validation, governance warnings, and the kill-cascade all
live in `triad_dr.store.Store` and are used as-is. The two events-queue
tools are the same kind of thin adapter over `triad_dr.events_queue.EventQueue`.

**stdout is the MCP protocol channel.** This module never calls `print()`.
Logging is pinned to stderr at package-import time by `triad_dr.logging`
(triggered by importing the parent `triad_dr` package, which every import of
this module does); this file relies on that rather than reconfiguring it.

**Errors become messages, never stack traces.** Store methods raise the
typed exceptions in `triad_dr.errors` (all subclasses of `TriadDRError`) with
an actionable, pre-written message. Every tool here catches
`(TriadDRError, ValueError)` around its `Store` call and re-raises as
`mcp.server.mcpserver.exceptions.ToolError`, which the MCP request handler
turns into an `is_error=True` result carrying that message — never a raw
traceback. Anything else (a genuine bug) is left to propagate as an
`UnexpectedToolError`, which deliberately withholds its detail from the
client and logs the traceback server-side instead.

Dependency injection only: `build_server(store)` takes an already-constructed
`Store` and closes over it when registering each tool. There is no module-
level global `Store` — `main()` builds one real one for the console script,
and tests build their own against a moto-mocked table.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from triad_dr import __version__
from triad_dr.errors import CredentialsError, TableMissingError, TriadDRError
from triad_dr.events_queue import EventQueue
from triad_dr.models import (
    PERSONA_BY_TYPE,
    Cause,
    Escalation,
    EscalationStatus,
    RecType,
    Record,
    Status,
    Verdict,
)
from triad_dr.store import Store

logger = structlog.get_logger(__name__)

DEFAULT_TABLE_NAME = "triad-decision-registers"

# dr_export's internal listing when no single `ref` is given: large enough to
# be "all of them" at this register's tens-to-hundreds-of-records scale (spec
# section 9), without hardcoding a smaller default limit meant for dr_list.
_EXPORT_LIST_LIMIT = 10_000

_GOVERNING_RELATION = "implements-bet"

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

_CAUSE_ROUTING: dict[str, str] = {
    "market": (
        "Seller accountability: the intent was delivered as specified and the "
        "market rejected it. The audience thesis is disproven. Next record: a "
        "new MDR bringing voice of customer back, funded by a new or "
        "superseding IDR."
    ),
    "delivery": (
        "Builder accountability: the built thing did not match the experience "
        "the MDRs specified. The bet is UNTESTED, not disproven — nobody ever "
        "actually put the intended thing in front of a person. Killing the bet "
        "here would discard an untested thesis. Next record: an ADR that "
        "closes the delivery gap."
    ),
    "both": (
        "Some of each: the bet stays untested until the delivery gap closes, "
        "same as a pure delivery miss."
    ),
}

_TEMPLATE_FIELDS: dict[str, list[str]] = {
    "ADR": ["context", "decision", "consequences", "alternatives_considered"],
    "IDR": [
        "bet",
        "thesis",
        "investment",
        "success_criteria",
        "kill_criteria",
        "review_date",
        "delegation",
    ],
    "MDR": [
        "audience",
        "jobs_to_be_done",
        "positioning",
        "channel_motion",
        "evidence_plan",
        "user_evidence",
        "experiment",
        "success_metric",
    ],
}

_TEMPLATE_HINTS: dict[str, str] = {
    "context": "Why this decision is needed now; the situation forcing a choice.",
    "decision": "The decision itself, stated plainly and imperatively.",
    "consequences": "What this makes easier, harder, or newly possible.",
    "alternatives_considered": (
        "Options considered and why they lost — including any disagreement "
        "with a Seller-stated intent that turned out not to be buildable "
        "within the bet."
    ),
    "bet": "One sentence: what is being funded.",
    "thesis": "Why we believe this bet pays off.",
    "investment": "Time/money committed — the appetite.",
    "success_criteria": "What 'this worked' looks like, concretely and testably.",
    "kill_criteria": "What would tell us to stop.",
    "review_date": "ISO-8601 date this bet's success_criteria gets tested against evidence.",
    "delegation": (
        "What authority, if any, agents hold for work under this bet. Omit "
        "for none — that is the default. Delegation dies if this IDR is "
        "superseded or deprecated."
    ),
    "audience": "The segment — name buyer and user separately where they differ.",
    "jobs_to_be_done": "What the person is hiring this to do.",
    "positioning": "How this is framed against alternatives.",
    "channel_motion": "How it reaches the audience.",
    "evidence_plan": (
        "How and when user_evidence gets collected — method, sample, timing. "
        "E.g. '8-person prototype round, week of Sep 15, before build starts'."
    ),
    "user_evidence": (
        "What was actually observed, with source and sample. E.g. '6 of 8 in "
        "the August round failed to find export.' Observation, not assertion."
    ),
    "experiment": "What is being tried and how it will be measured.",
    "success_metric": "The number that says this worked.",
}

# ----------------------------------------------------------------------
# Tool descriptions — the highest-value content in this module. Each is
# read by a calling model with no other context, so it has to convey the
# persona rules, the propose/accept/supersede flow, and the review/cause
# routing on its own (spec section 5's closing checklist). Passed via
# `description=` to `@server.tool(...)` rather than left as the function's
# plain `__doc__`, which the SDK uses verbatim (including source
# indentation) — keeping them here as flush-left constants avoids leaking
# raw whitespace into what the model reads, and keeps the "product" content
# in one place. The functions below keep ordinary Google-style docstrings
# for humans reading the source.
# ----------------------------------------------------------------------

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "dr_create": """
Create a new decision record — an ADR, IDR, or MDR — and return it in full.

Which type owns what (get this right; it is the whole point of the register):
- IDR (Investor): the bet. What gets funded, for how long, on what terms, what
  success/kill looks like, and when it gets reviewed. Every ADR and MDR must
  trace back to a governing IDR via links=[{"ref": "IDR-000N", "relation":
  "implements-bet"}] — no bet, no work. Routine chores are not exempt: a
  project's standing operations IDR governs maintenance, so even a dependency
  bump links to a real bet.
- MDR (Seller): the market AND the user. Audience, positioning, channel
  motion, pricing narrative, launch experiments — and also every usability
  finding, user-research result, jobs-to-be-done note, win/loss reason,
  support signal, and activation/churn reason. There is no separate "UDR" or
  user-experience record type. If it is a finding about what a real person
  could or couldn't do, it is an MDR — filing it as an ADR is a category
  error even when the finding concerns a UI element the Builder shipped.
- ADR (Builder): architecture, technology selection, data model, operational
  posture, and the technical shape of the experience the Seller specified in
  an MDR. If the Seller's stated intent is not buildable within the bet, that
  disagreement belongs in this ADR's alternatives_considered field — not a
  silent substitution of something easier to build.

fields keys expected per type (fields is free-form storage; these are the
keys a later reader — human or model — will expect to find):
- ADR: context, decision, consequences, alternatives_considered
- IDR: bet, thesis, investment, success_criteria, kill_criteria, review_date,
  delegation (agent authority granted under this bet — omit for none)
- MDR: audience, jobs_to_be_done, positioning, channel_motion, evidence_plan,
  user_evidence, experiment, success_metric
Call dr_template(rec_type) first if you want the full skeleton with hints.

Self-referencing this record: refs are assigned at write time, so a ref you
type while drafting is a guess — and under concurrent sessions it is often
wrong, because another session may claim that number mid-draft. Write the
literal {{ref}} in body_md or in any fields value and it is replaced with
the real assigned ref on creation. Never hand-write your own ref into a
heading; if body_md's first heading names a ref that is not the one
assigned, this tool warns you that it guessed wrong.

status defaults to "proposed". This is a solo-operator register with no
approval gate: dr_create never blocks. It does warn, though, and the
warnings matter — an ADR/MDR with no implements-bet link warns (link it, or
dr_list(unfunded=True) will keep surfacing it), and an MDR with neither
user_evidence nor evidence_plan warns (a hypothesis with no plan to test it
means review_date is the first and most expensive trip-wire it will hit).
""".strip(),
    "dr_get": """
Fetch one full decision record — every stored attribute: fields, body_md,
links, status_history, reviews — by its human ref (e.g. "ADR-0007") or, if
you only have the internal identity, by id=<ULID>. Provide exactly one of
ref/id. Use this when you need the whole record; use dr_list or dr_search
when you need to find candidates first.
""".strip(),
    "dr_list": """
List decision records in a project as summary rows (ref, rec_type, persona,
title, status, tags, project, timestamps, and unfunded for ADR/MDR rows),
newest-created first.

Filter by rec_type to see one persona's output: rec_type="IDR" for the
Investor's active bets, rec_type="ADR" or "MDR" for one function's records.
There is deliberately no persona= filter — persona is derived 1:1 from
rec_type, so filtering by rec_type already gets you there, and a second
parameter over the same axis would just be two ways to spell one query. Pass
persona and it is silently ignored; it is never wired to a Store filter.

unfunded=true surfaces ADRs/MDRs with no "implements-bet" link back to a
governing IDR — work that never had a bet, or lost one when its IDR was
rejected or deprecated. Nothing else flags this for you, so it is worth
checking periodically.

stale=true surfaces records with a stale reference: a link whose target is
now "superseded"/"deprecated", or a ref (e.g. "IDR-0003") appearing in
body_md prose that points at a record that is "superseded", "deprecated",
or does not exist at all. This is a different problem than unfunded — a
stale record still has its governing link, it just points at a dead bet,
which unfunded=true will never catch. Each row returned under stale=true
carries a stale_reasons list naming exactly which ref made it stale,
whether that ref was a link or prose, and the target's status (or
"missing") — never a bare flag with nothing to act on.

Every response carries project_version — a counter that bumps on every write
to this project. If you briefed off a read taken at a lower version, your
cached picture is stale: re-read before acting on it. Concurrent sessions
write to the same register, and acting on a superseded topology has already
cost real rebuilt work. since="<ISO timestamp>" then answers what actually
changed, returning only records updated after that moment. The two are
different units on purpose: the version cheaply answers whether anything
changed, since answers what.

dr_list(rec_type="IDR", status="accepted") also doubles as the complete
current delegation set for a project: read each result's fields.delegation
to see the whole of what is currently authorized for agents to do
unsupervised. Delegation is granted only in IDRs, and only while they are
active — supersede or deprecate one and its delegation is gone too.
""".strip(),
    "dr_update": """
Partially update a record's mutable attributes: title, body_md, fields,
tags, links, deciders. Keys you omit from patch are left untouched.

`fields` MERGES; everything else replaces wholesale. This asymmetry is
deliberate. {"tags": ["a"]} sets tags to exactly ["a"] — replacing a list
wholesale is predictable. But `fields` is a map of independent semantic
keys, and replacing it wholesale would mean changing one key requires
re-transmitting every other key verbatim, where a single dropped key
silently destroys it on an accepted record. So:

  {"fields": {"review_date": "2026-11-01"}}

changes only review_date and leaves bet, thesis, investment,
success_criteria, kill_criteria and delegation exactly as they were. To
DELETE a fields key, pass it explicitly as null.

Note the common mistake: fields keys are not top-level patch keys.
{"delegation": ...} is rejected — delegation lives inside fields, so the
correct call is {"fields": {"delegation": ...}}. The error names the
correction when it recognizes the key.

status, status_history, reviews, seq, ref, id, project, rec_type,
created_at, supersedes, and superseded_by cannot be patched here — use
dr_set_status, dr_review, or dr_supersede instead. Attempting one of them is
a rejected call that names the right tool to use.

If the record is already "accepted", editing title/body_md/fields in place
still succeeds — this tool never blocks — but the response warns that
dr_supersede is usually the better move: accepted decisions are meant to be
superseded when they change, with both versions kept and linked, not
silently rewritten in place. Use dr_update on an accepted record for typos
and small clarifications; use dr_supersede when the decision itself changes;
use dr_revise when the decision itself still stands but the reasoning
written down for it does not — dr_update would silently erase what the
record used to say, where dr_revise keeps it readable in a revisions entry.
""".strip(),
    "dr_revise": """
Replace body_md for the specific case where a record's decision still holds
but the reasoning written down for it no longer does — e.g. ADR-0023
justified itself with a benefit that ADR-0024 later removed. The decision
did not change; only the stated "why" did.

Three tools, one line each, to draw the boundary clearly:
- dr_update: typos and small clarifications. Silently rewrites in place
  (with a warning on an accepted record); nothing is kept of what it
  replaced.
- dr_revise (this tool): the decision stands, the stated reasoning does
  not. Appends {at, note, previous_body_md} to a machine-written revisions
  list — preserving the exact prior text is the whole point — then
  replaces body_md and bumps updated_at.
- dr_supersede: the decision itself changes. A new record is created,
  linked both ways, and the old one is (eventually) marked superseded —
  both versions kept as independent records.

note is REQUIRED and must be non-empty: a revision with no explanation of
why the reasoning changed is indistinguishable from a plain edit, and
explaining why is the entire reason to reach for this tool over dr_update.
An empty or missing note is a rejected call, not a warning.

Legal on any status, including "accepted" — and unlike dr_update, this
never emits the accepted-record supersede warning. That warning exists to
steer dr_update's silent in-place rewrite toward dr_supersede; dr_revise is
already the tool built for exactly this case, so the warning does not fire
here.

dr_export renders revisions into their own "## Revisions" section, the same
treatment it already gives status_history and reviews.
""".strip(),
    "dr_set_status": """
Move a record through its lifecycle: draft -> proposed -> accepted ->
(superseded | deprecated), and proposed -> rejected. Any other transition is
rejected, and the error names the states actually reachable from the
record's current status. Every transition appends one entry to
status_history with a timestamp and your optional note; body_md is never
touched by this tool.

Rejecting or deprecating an IDR has a consequence worth knowing before you
call this: every ADR/MDR linked to that IDR via "implements-bet" instantly
loses its funding — the work it was authorized under no longer has a bet
behind it. (Superseding an IDR does not do this — a superseded bet hands its
work to the record that replaced it.) This call tells you exactly which refs
just got orphaned in its response, but it deliberately does not touch their
status for you: some of that work may deserve a new home under a different
bet rather than being killed outright, and that re-homing decision belongs
to a human. dr_list(unfunded=true) keeps surfacing orphans until you
re-link, deprecate, or supersede each one.

Moving a record to "accepted" runs two more checks, both warn-only — the
transition always succeeds:
- Placeholder lint: fields and body_md are scanned for unfilled
  placeholders — [X]/[Y]-style brackets, TBD, TODO, ???, FIXME,
  <name>-style angle brackets — and the response warns naming each one and
  exactly where it sits (which field, or body_md). An accepted bet
  committing "$[X]/month" has no real appetite behind it; this is how that
  gets caught instead of discovered later.
- Commitment gate (IDRs only): if this IDR carries links with relation
  "requires-attestation" (the Builder/Seller preconditions for going all
  in — see dr_create's IDR guidance), every target's status is checked and
  the response warns naming every one that is not itself "accepted",
  including targets that do not exist at all. A solo operator may
  knowingly commit ahead of an attestation; the warning just makes sure
  that call is written down at the moment it happens.
""".strip(),
    "dr_review": """
Record an outcome against a record's own stated criteria — typically an IDR
at its review_date, or an MDR after an experiment, though this is permitted
on any record. evidence is required prose: what was actually observed, from
what source, at what sample size — not a restatement of the criteria being
judged. verdict is one of met | missed | mixed | too-early. This only
appends to the record's reviews list; it never changes status. Deciding what
to do about a bad review — persevere with a revised bet, supersede, or kill
— is a separate, deliberate dr_set_status or dr_supersede call.

On an IDR, the response echoes back that IDR's own success_criteria and
kill_criteria, so the verdict gets judged against what was actually written
down at bet time, not against memory.

If verdict="missed" on an IDR, cause is required — omitting it is a
rejected call, not a warning, because the three options route to
completely different next steps and misattributing one is close to the
single most expensive mistake this register exists to prevent:
- cause="market": the intent was built as specified and the market said no.
  Seller accountability — the audience thesis is disproven. Next record: a
  new MDR bringing voice of customer back, funded by a new or superseding
  IDR.
- cause="delivery": the built thing did not match the experience the MDRs
  specified. Builder accountability — and critically, the bet is UNTESTED,
  not disproven, because nobody ever actually put the intended thing in
  front of a person. Killing the bet here throws away a thesis that was
  never really tried. Next record: an ADR that closes the delivery gap.
- cause="both": some of each — the bet stays untested until the delivery
  gap closes, same as a pure delivery miss.
cause is accepted on any record and any verdict, but is only ever required
in the missed-IDR case above.
""".strip(),
    "dr_due": """
List every IDR whose fields.review_date has passed with no dr_review
recorded after that date — the one report this register owes its operator.
A bet with success_criteria and no review past its date is just an opinion
with a date attached to it; this is how that gets caught before it is
forgotten. Each MDR's evidence_plan exists to make this report a formality
rather than the first time anyone actually looks at outcomes — if an IDR
shows up here and its funded MDRs never had an evidence_plan, that is the
trip-wire that did not get set.
""".strip(),
    "dr_escalate": """
File a decision the operator owes. Returns ESC-000N plus a warnings list.

This is the ONE tool in this server you may call on your own initiative.
Everywhere else here, a record gets drafted only when the operator asks for
it — this tool is the deliberate exception, because raising a hand must be
cheaper than proceeding without authority. An agent that is blocked from
escalating will proceed anyway, and that is the exact failure this tool
exists to prevent.

Agents do not make important decisions. Not the ones that seem obvious in
the moment, not the ones where stopping to escalate feels like an unwelcome
interruption to a session that is otherwise going well. The bar: escalate
one-way doors, decide two-way doors — and when it is genuinely ambiguous
which one you are looking at, escalate. Escalate anything irreversible,
anything that exceeds what the governing IDR's fields.delegation actually
grants you, anything that would change the bet itself, and any material
ambiguity where the stakes are not trivial. Only the reversible, low-stakes,
mechanical tier — the naming-a-variable tier — gets decided in flight, and
even that is worth writing down afterward, just not through this tool.

The two failure modes are not symmetric, which is why the bar above is set
deliberately loose. Escalate too often and the cost is a slower inbox:
visible, mildly annoying, and self-correcting — the operator reads it and
widens a delegation grant. Escalate too rarely and the cost is an important
decision made by something with no accountability for it, discovered later,
already built on top of. When genuinely in doubt: escalate.

Write the item so an un-initiated leader can read it once and decide —
someone walking in cold, no session history, no repo open, deciding from a
phone. You have an entire session of context they do not; the characteristic
way an escalation fails is a question that only makes sense to whoever
already lived through the session — "should I use approach B?" is
unanswerable to anyone who was not watching you work. Supply the
background, not just the question:
- options: each alternative with its consequence and whether choosing it is
  reversible — this is what lets a stranger judge the tradeoff without
  opening the repo.
- recommendation: you did the work and have a view; say what you would do
  and why. An escalation that is only a question, with no recommendation
  attached, sits in the queue far longer than one that is not.
- cost_of_delay: what actually happens if this waits. "Nothing breaks
  before Friday" is a complete and useful answer — it tells the operator
  this can wait for a normal pass through the inbox instead of needing to
  be found right now.
- blocking: is a worker actually stopped right now, or is this a flag for
  later? These are different emergencies, and the queue sorts on this field
  to keep them apart.

Never blocks on thinness: fewer than two options, no recommendation, no
cost_of_delay, no governing_ref — each of these produces a warning, never a
rejection, because a worker that is genuinely stuck must always be able to
raise a hand, even with an incomplete case. File first; a thin escalation
that warns beats silence or an unaccountable decision every time.
""".strip(),
    "dr_escalations": """
List this project's escalation queue — the operator's inbox of decisions
owed, not decisions already made.

Every open escalation has exactly two honest ways out, and they are not
interchangeable. Use dr_answer when a decision was actually made — it sets
status="answered" and can produce a record via produces. Use dr_withdraw
when the question turned out to need no decision at all — already resolved
elsewhere, overtaken by events, or a duplicate of another escalation — it
sets status="withdrawn" instead. Running a moot item through dr_answer
just to clear it is what corrupted the unrecorded sweep below in
production: 25 of 26 answered escalations produced no record, and an
unknown share of those were never decisions in the first place, which made
them indistinguishable from genuine decisions that leaked without one.

Ordered blocking first, then newest within each group — the same recency
direction dr_list uses, so the two listings never disagree about what
"first" means. blocking=true items float to the top because a worker
stopped right now and a flag for later are different emergencies;
raised_at stays visible on every row so age is never hidden, it is just not
the primary sort key.

unrecorded=true is a different sweep, over ANSWERED escalations rather than
open ones: it returns escalations that were answered, produced no record
(produced_ref is null), and were never flagged no_record=true on
dr_answer — decisions the operator made correctly but that a later agent
reading the ADR/IDR/MDR set will never see, because they exist only as
prose inside a resolved escalation, and nothing else reads escalations. This
is "no record, no decision" made checkable: run it periodically the same
way you'd run dr_list(unfunded=true). It overrides the status filter (an
unrecorded item is definitionally answered) but still composes with
blocking_only. A withdrawn escalation never appears here — withdrawal
owes no record by definition, which is the whole reason it exists as a
separate exit from answer.

status also accepts "withdrawn" directly, same as "open" or "answered", if
you specifically want the moot-and-cleared items rather than the unrecorded
sweep.

Deliberately project-scoped in both directions, with no all_projects
parameter — do not add one. An agent escalates within the project it is
working in, and the operator answers within that project; this keeps the
read a single partition Query, never a Scan. Cross-project visibility is
the scheduled sweep's job, not something a tool call does.

Read the shape of this queue, not just its contents: a queue that stays
persistently non-empty for days is not a problem to fix by answering
faster. It usually means the governing IDRs' delegation grants are too
narrow for the work actually happening — the fix is widening them, not
just clearing the backlog.
""".strip(),
    "dr_answer": """
Resolve an open escalation BECAUSE A DECISION WAS MADE: sets
status="answered", stamps answered_at, and records resolution in the
operator's own words.

If nothing actually needed deciding — the question was already resolved
elsewhere, overtaken by events, or a duplicate of another escalation — use
dr_withdraw instead, not this tool with a throwaway resolution. That
substitution is exactly what corrupted the unrecorded sweep in production:
25 of 26 answered escalations produced no record, and because dr_withdraw
did not exist yet, moot items had nowhere to go but through dr_answer,
making them indistinguishable from genuine decisions that leaked without a
record. dr_answer means a decision was made; dr_withdraw means one wasn't
needed.

produces is optional, and it should usually stay that way — a resolution
often should not create a record. "Don't do that" is a complete answer and
needs no ADR/IDR/MDR behind it. Reach for produces only when the resolution
genuinely is a new decision worth keeping: pass rec_type/title (and
optionally body_md/fields/tags/links/deciders/status, the same shape as
dr_create) and the record is created and linked via produced_ref in the
same atomic write — the escalation update and the new record land together
or neither does.

"No record, no decision." A real deployment-credentials decision was made
correctly and then went missing: 25 of 26 answered escalations in one
project had produced_ref=null, and an agent reading the ADR set later
proceeded — wrongly, and reasonably given what it could see — on the wrong
authentication model, because the actual decision existed only as prose
inside a resolved escalation, which nothing reads. So: when produces is
omitted AND no_record is not set to true, the response warns that this
resolution now exists only inside this escalation, that nothing reading the
register (dr_list, dr_search, dr_export) will ever find it, and names both
remedies — pass produces on this same call, or pass no_record=true if a
record is genuinely not needed. This warns, it never blocks. Set
no_record=true whenever you are confident no record belongs here — that is
the intended, common case for a trivial "don't do that" — so the omission
reads as deliberate, not forgotten. escalations(unrecorded=true) is the
periodic sweep that finds everything answered with neither.

Answering an already-resolved escalation is rejected, naming the prior
resolution and when it was made, rather than silently overwriting it —
each escalation is answered exactly once, so produces and no_record can
only be set on the one call that resolves it.

Volume is how a register becomes worthless: creating a record for every
resolution just to have one produces the same noise this register exists
to avoid. Produce a record when the decision itself is worth finding again
later; set no_record=true when the answer is only useful in the moment it
was given.
""".strip(),
    "dr_withdraw": """
Close an open escalation as moot: sets status="withdrawn", stamps
withdrawn_at, and records reason in your own words.

This is NOT the way to close something you decided. If a decision was
actually made, that is dr_answer — with produces when the decision is
worth keeping as a record. dr_withdraw means no decision was needed at
all: the question was already resolved elsewhere, overtaken by events, or
turns out to be a duplicate of another escalation. Before this tool
existed, the only way to clear a moot escalation was dr_answer with a
throwaway resolution, and that is exactly what corrupted the register's
"no record, no decision" sweep in production — 25 of 26 answered
escalations left no record, and moot items run through dr_answer for lack
of anywhere else to go became indistinguishable from genuine decisions
that leaked without one. A withdrawn escalation owes no record and never
appears in escalations(unrecorded=true), which is the whole reason this
tool exists as a separate, real exit from "open" rather than a flavor of
answering.

reason is required and must be non-empty — a withdrawal with no reason is
indistinguishable from an escalation someone simply forgot about, and
telling those two apart is the entire point. When duplicate_of is given,
reason may be brief; the link itself carries the meaning ("see
duplicate_of" is a fine reason).

duplicate_of names another escalation this one duplicates, e.g. an agent
filed the same question twice, or two workers hit the same edge from
different angles. Unlike a record link, existence IS verified — it must be
a well-formed ESC-XXXX ref pointing at a real escalation, and it cannot
name itself. If the target you point at is itself withdrawn as a
duplicate of something else, the response warns and names that ultimate
target — a reader forced to hop through two duplicate links to find where
the decision actually lives is one hop from finding nothing, and the fix
is pointing at the ultimate target directly.

Refuses to withdraw an escalation that is not open, naming its current
status and, when it was answered, the resolution already on record — an
answered escalation recorded a decision that genuinely happened, and
withdrawing it would erase that decision rather than admit one was never
needed.
""".strip(),
    "dr_adopt_successor": """
Link an EXISTING record as the successor to another — the companion to
dr_supersede for when the replacement was already drafted standalone.

supersedes/superseded_by are structural and cannot be patched with
dr_update, so without this a supersession you draft now and accept later
can never be linked at all: the relationship survives only in prose and
status-history notes, which no query can follow. If you already worked
around that by writing "supersedes X on acceptance" into a record's body,
this is how you repair it — a predecessor sitting at "superseded" with an
empty superseded_by simply gets its link filled in, status untouched.

Same deferred semantics as dr_supersede: the pair is linked immediately so
the chain is machine-followable, and the predecessor keeps its own status
until the successor is accepted. If the successor is already accepted, the
flip happens now, because there is nothing left to wait for.

Use dr_supersede when you are writing the replacement fresh; use this when
it already exists.
""",
    "dr_supersede": """
Retire old_ref and replace it with a new record in one atomic operation:
creates new_record as a fresh record (same rec_type as old_ref unless
new_record.rec_type overrides it), sets supersedes/superseded_by both ways,
and flips old_ref's status to "superseded". new_record starts at
status="proposed" by default, same as dr_create, and only new_record.title
is required — everything else defaults the way dr_create's does.

Use this instead of dr_update whenever the decision itself is changing, not
just its prose — especially once old_ref is "accepted". Editing an accepted
record in place erases what was actually decided at the time it was
decided; superseding keeps both versions and the link between them, which
is the entire point of keeping a decision register instead of a wiki page.
dr_update will still let you patch an accepted record's title/body_md/
fields and will just warn — reserve that for typos and small
clarifications, not for changing the decision.
""".strip(),
    "dr_search": """
Tokenized keyword search across title, tags, body_md, and every value in
fields — including MDR.user_evidence, so this is the fastest way to check
what has already been observed about users before commissioning new
research.

query is split on whitespace into terms, and a record matches if it
contains ANY term, case-insensitively, as a substring — terms are OR-ed,
this is NOT one long literal phrase match. Search
"shared credentials runner service account deploy authentication SAML" and
a record containing the bare substring "service account" is found, even
though the record never contains that exact seven-word phrase — each of
the seven terms is checked independently. An empty result means none of
the terms appear anywhere in scope, not that the phrase as a whole failed
to match; if that surprises you, the decision may genuinely not be written
down yet, which for a governance register is itself the finding worth
having.

Results are ranked so a strong match sorts above an incidental one: a
record whose full query phrase appears verbatim ranks above everything;
otherwise, ranking is by matched_terms — the count of DISTINCT terms
matched — descending, then by created_at descending. A record matching
five of seven terms outranks one matching one. Every result row carries
matched_terms so you can tell a strong hit from a coincidence. A single-term
query behaves exactly as it always has: every match ties on matched_terms
and on the exact-phrase flag, so results fall back to created_at descending.

Defaults to the active project; set all_projects=true to search the whole
register (useful for "has anyone, on any project, already tried this"). This
is a Query/Scan with client-side matching, not a search index — fine at the
tens-to-hundreds-of-records-per-project scale this register targets, not
built for more.
""".strip(),
    "dr_projects": """
List every project namespace in the register with per-type record counts
(ADR/IDR/MDR). Works without DR_PROJECT set — this is the one tool that
reads across the whole register rather than one project's partition. Use it
to sanity-check which project a given .mcp.json is actually wired to, or to
find a project slug to pass explicitly to another tool's project= argument.
""".strip(),
    "dr_template": """
Return a ready-to-fill markdown skeleton for one record type, listing every
field that type's template calls for, with a one-line hint per field. Fill
it in and pass the field values straight through as dr_create's fields=
argument. For rec_type="MDR" the skeleton includes both evidence_plan (how
and when user_evidence will be gathered — method, sample, timing) and
user_evidence (what was actually observed, with source and sample) as
separate, both-expected fields — a plan is not evidence, and dr_create will
warn if a new MDR has neither.
""".strip(),
    "dr_export": """
Render decision records to markdown files on disk: one record (ref=...),
every record of one type (rec_type=...), or the whole project (neither
given). Each file is written to <out_dir>/<REF>-<slug>.md, relative to the
current working directory (out_dir defaults to "docs/decisions"), as
frontmatter (ref, id, project, rec_type, persona, title, status, tags,
links, fields, timestamps) followed by the record's body_md verbatim, then
rendered "## Status history", "## Reviews", and "## Revisions" sections
built from the record's own machine-written history (the last showing each
dr_revise's note alongside the exact text it replaced). This is a
point-in-time render, not a live mirror — DynamoDB stays the source of
truth, and re-running dr_export overwrites the files with the current
state. Paths are trusted, not sandboxed: run the client from the directory
you actually want files written into.
""".strip(),
    "dr_events_pending": """
Receive pending decision-record events from the durable subscriber queue
(DrEventsQueue, IDR-0003) WITHOUT deleting anything. Each returned event
carries a receipt_handle -- a loan, not a claim check. SQS is at-least-once
delivery and this tool never deletes on your behalf, on purpose: see
dr_events_ack, its required second half.

Do the work an event describes FIRST -- label a work item, comment on it,
whatever "apply" means for the caller -- and only THEN call dr_events_ack
with that event's receipt_handle. Calling dr_events_ack before the apply
has actually succeeded is the one mistake this tool pair exists to prevent:
delete is unconditional and irreversible, so acking an event you did not
really apply loses that decision permanently and silently -- no error, no
trace, just a consequence that never landed anywhere.

An event you never ack is not lost. It becomes invisible for a visibility
timeout (currently about 5 minutes) and then reappears in a later
dr_events_pending call. That redelivery is the intended behavior, not a
bug -- it is what makes "at least once" mean at least, not at most. Expect
to sometimes see the same event twice, e.g. after a crash mid-apply;
applying it again (or finding the label/comment is already there) is the
correct response, not an error.

max_messages caps how many are received in one call; SQS's own per-request
ceiling is 10, so asking for more makes additional polls rather than one
larger one. Order is best-effort, never guaranteed.
""".strip(),
    "dr_events_ack": """
Permanently delete events from the subscriber queue -- call this after, and
only after, you have actually applied each one. This is the required
second half of the dr_events_pending / dr_events_ack pair; see
dr_events_pending for why receiving and deleting are two separate calls
rather than one.

Pass the receipt_handle from a dr_events_pending response, not the event's
ref or id: the receipt_handle identifies this specific delivery, and it
changes on redelivery even for the same underlying event.

Returns the count actually deleted, which can be LESS than the number of
receipt_handles supplied -- a partial failure (an expired handle, or a
message already deleted by an earlier call) deletes what it can and
reports the rest as not deleted rather than claiming full success.
Whatever was not deleted remains on the queue and will be redelivered;
treat a short count as a signal to check, not as success for all of them.

Never call this for an event you have not actually applied. There is no
undo -- acking an unapplied event silently and permanently loses whatever
decision it carried.
""".strip(),
}


class NewRecordInput(BaseModel):
    """The replacement record body accepted by `dr_supersede`.

    Mirrors `dr_create`'s writable fields. `title` is the only required
    field; `rec_type` defaults to the superseded record's own type when
    left unset (`None`).
    """

    title: str
    rec_type: RecType | None = None
    body_md: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    links: list[dict[str, str]] = Field(default_factory=list)
    deciders: list[str] = Field(default_factory=list)
    status: Status = "proposed"


class ProducesInput(BaseModel):
    """The record `dr_answer` optionally creates atomically alongside a resolution.

    Mirrors `dr_create`'s writable fields. Unlike `dr_supersede`'s
    `NewRecordInput`, `rec_type` is required here — there is no prior
    record to inherit a type from.
    """

    rec_type: RecType
    title: str
    body_md: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    links: list[dict[str, str]] = Field(default_factory=list)
    deciders: list[str] = Field(default_factory=list)
    status: Status = "proposed"


# ----------------------------------------------------------------------
# Response shaping helpers
# ----------------------------------------------------------------------


def _record_dict(record: Record) -> dict[str, Any]:
    """Serialize a full `Record` to a JSON-safe dict for a tool response.

    Args:
        record: The record to serialize.

    Returns:
        The record's full attribute set as a plain dict (nested `Link`,
        `StatusEvent`, and `Review` models are recursively dumped too).
    """
    return record.model_dump()


def _escalation_dict(escalation: Escalation) -> dict[str, Any]:
    """Serialize a full `Escalation` to a JSON-safe dict for a tool response.

    Args:
        escalation: The escalation to serialize.

    Returns:
        The escalation's full attribute set as a plain dict (nested
        `EscalationOption` models are recursively dumped too).
    """
    return escalation.model_dump()


def _summary_row(record: Record) -> dict[str, Any]:
    """Build one dr_list/dr_search/dr_due summary row for a record.

    Always includes `ref`, `title`, `status` per spec section 5's preamble,
    plus enough context (`rec_type`, `persona`, `tags`, `project`,
    timestamps) to be useful without a follow-up `dr_get`. ADR/MDR rows also
    carry `unfunded`, matching spec section 4 ("dr_list marks such records
    as unfunded").

    Args:
        record: The record to summarize.

    Returns:
        A JSON-safe summary dict.
    """
    row: dict[str, Any] = {
        "ref": record.ref,
        "rec_type": record.rec_type,
        "persona": record.persona,
        "title": record.title,
        "status": record.status,
        "tags": record.tags,
        "project": record.project,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    if record.rec_type in ("ADR", "MDR"):
        row["unfunded"] = not any(
            link.relation == _GOVERNING_RELATION for link in record.links
        )
    return row


def _render_template(rec_type: str) -> dict[str, Any]:
    """Build the dr_template response for one record type.

    Args:
        rec_type: One of "ADR", "IDR", "MDR".

    Returns:
        A dict with `rec_type`, `persona`, the ordered `fields` list for
        that type, and a fillable `template_md` markdown skeleton
        (containing every field name and a one-line hint for each).
    """
    persona = PERSONA_BY_TYPE[rec_type]  # type: ignore[index]
    field_names = _TEMPLATE_FIELDS[rec_type]
    field_lines = "\n".join(f"- **{name}**: {_TEMPLATE_HINTS[name]}" for name in field_names)

    extra_note = ""
    if rec_type == "MDR":
        extra_note = (
            "\n\nBoth evidence_plan and user_evidence are expected fields — a "
            "plan is not evidence. It is normal for user_evidence to be empty "
            "on a fresh hypothesis; it is not normal for both to be empty "
            "with no plan to fix that."
        )
    elif rec_type == "IDR":
        extra_note = (
            "\n\nThis record is expected to receive a dr_review at "
            "review_date. Set delegation only if this bet grants agents "
            "authority to act under it; omit it otherwise."
        )

    template_md = (
        f"# {rec_type}-XXXX: <imperative title>\n\n"
        f"## Fields\n\n{field_lines}{extra_note}\n\n"
        "## Body\n\n<narrative prose — context, reasoning, anything that "
        "does not fit neatly into the fields above>\n"
    )
    return {
        "rec_type": rec_type,
        "persona": persona,
        "fields": field_names,
        "template_md": template_md,
    }


def _slugify(title: str) -> str:
    """Turn a record title into a filesystem-safe slug for dr_export.

    Args:
        title: The record's title.

    Returns:
        A lowercase, hyphen-separated slug; "untitled" if `title` has no
        alphanumeric characters at all.
    """
    slug = _SLUG_STRIP_RE.sub("-", title.lower()).strip("-")
    return slug or "untitled"


def _render_frontmatter(record: Record) -> str:
    """Render a record's frontmatter block for dr_export.

    Uses JSON encoding for list/dict-valued lines, which is also valid
    inline YAML flow syntax — this avoids taking a PyYAML dependency for a
    handful of scalar/list/dict fields.

    Args:
        record: The record to render.

    Returns:
        The `---`-delimited frontmatter block, including both delimiters.
    """
    lines = [
        "---",
        f"ref: {record.ref}",
        f"id: {record.id}",
        f"project: {record.project}",
        f"rec_type: {record.rec_type}",
        f"persona: {record.persona}",
        f"title: {json.dumps(record.title)}",
        f"status: {record.status}",
        f"tags: {json.dumps(record.tags)}",
        f"deciders: {json.dumps(record.deciders)}",
        f"supersedes: {json.dumps(record.supersedes)}",
        f"superseded_by: {json.dumps(record.superseded_by)}",
        f"created_at: {record.created_at}",
        f"updated_at: {record.updated_at}",
        f"links: {json.dumps([link.model_dump() for link in record.links])}",
        f"fields: {json.dumps(record.fields)}",
        "---",
    ]
    return "\n".join(lines)


def _render_export_markdown(record: Record) -> str:
    """Render one record to the full dr_export markdown document.

    Args:
        record: The record to render.

    Returns:
        Frontmatter, then `body_md` verbatim, then rendered "## Status
        history", "## Reviews", and "## Revisions" sections built from the
        record's machine-written history (spec section 7 item 3; the
        revisions section per Change 2 of the "no record, no decision"
        production feedback).
    """
    parts = [_render_frontmatter(record), "", record.body_md.rstrip(), "", "## Status history", ""]
    if record.status_history:
        for event in record.status_history:
            note = f" — {event.note}" if event.note else ""
            parts.append(f"- {event.at}: {event.status}{note}")
    else:
        parts.append("(none)")

    parts += ["", "## Reviews", ""]
    if record.reviews:
        for review in record.reviews:
            cause = f" (cause: {review.cause})" if review.cause else ""
            note = f" — {review.note}" if review.note else ""
            parts.append(f"- {review.at}: {review.verdict}{cause} — {review.evidence}{note}")
    else:
        parts.append("(none)")

    parts += ["", "## Revisions", ""]
    if record.revisions:
        for revision in record.revisions:
            parts.append(f"- {revision.at}: {revision.note}")
            parts.append("")
            parts.append("  Previous body_md:")
            parts.append("")
            previous_lines = revision.previous_body_md.splitlines() or [""]
            parts.extend(f"  > {line}" for line in previous_lines)
            parts.append("")
    else:
        parts.append("(none)")

    parts.append("")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Server construction
# ----------------------------------------------------------------------


def build_server(store: Store, event_queue: EventQueue | None = None) -> MCPServer:
    """Build the MCP server and register all twenty `dr_*` tools.

    Pure dependency injection: `store` and `event_queue` are captured by
    the tool closures defined in this function, never held in module-level
    mutable state. A fresh `Store` (e.g. one wired to a moto-mocked table
    in tests) produces a fresh, fully independent server.

    Args:
        store: The `Store` instance every dr_* record/escalation tool
            adapts.
        event_queue: The `EventQueue` `dr_events_pending` / `dr_events_ack`
            adapt. Injected for tests (a moto-mocked SQS queue) or custom
            configuration. When omitted, each of those two tools builds
            its own `EventQueue()` from `DR_EVENTS_QUEUE_URL` at call
            time rather than at server startup -- mirroring how `Store`
            failures surface per-call rather than blocking startup -- so a
            server with that env var unset still starts and serves the
            other sixteen tools.

    Returns:
        A configured `MCPServer`, ready for `.run("stdio")` or direct
        `.call_tool(...)` invocation (as used in tests).
    """
    server = MCPServer(
        name="triad-dr",
        version=__version__,
        instructions=(
            "Decision registers for one developer across many projects, in "
            "three types: IDR (Investor — the bet: what's funded, for how "
            "long, success/kill criteria, review_date), MDR (Seller — the "
            "market AND the user: audience, positioning, and every "
            "usability/voice-of-customer finding; there is no separate user "
            "record type), ADR (Builder — architecture and the technical "
            "shape of the experience the Seller specified). Every ADR and "
            "MDR must link to a governing IDR via an 'implements-bet' "
            "relation — no bet, no work. Records propose then get accepted; "
            "once accepted, prefer dr_supersede over editing in place. The "
            "project namespace comes from DR_PROJECT or an explicit "
            "project= argument — never guess one. Records are drafted only "
            "when the operator asks — dr_escalate is the sole exception: "
            "an agent may call it on its own initiative to file a decision "
            "it is not authorized to make, into the project's escalation "
            "queue (dr_escalations), which the operator resolves with "
            "dr_answer when a decision was made, or dr_withdraw when the "
            "question turned out to need no decision at all."
        ),
    )

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_create"])
    def dr_create(
        rec_type: RecType,
        title: str,
        body_md: str = "",
        fields: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        links: list[dict[str, str]] | None = None,
        deciders: list[str] | None = None,
        status: Status = "proposed",
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.create` for MCP.

        Args:
            rec_type: One of "ADR", "IDR", "MDR".
            title: Imperative title.
            body_md: Full markdown body.
            fields: Type-specific structured fields.
            tags: Free-text tags.
            links: Cross-type, same-project links.
            deciders: Names/identifiers of who decided.
            status: Initial status. Defaults to "proposed".
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The created record's full attributes plus a `warnings` list.

        Raises:
            ToolError: Wraps `NoProjectError`, `InvalidRefError`, a table-
                missing or credentials failure, or an invalid project slug.
        """
        try:
            record, warnings = store.create(
                rec_type=rec_type,
                title=title,
                body_md=body_md,
                fields=fields,
                tags=tags,
                links=links,
                deciders=deciders,
                status=status,
                project=project,
            )
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {**_record_dict(record), "warnings": warnings}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_get"])
    def dr_get(
        ref: str | None = None,
        id: str | None = None,  # noqa: A002 - matches Store.get_by_id's public param name
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.get` / `Store.get_by_id` for MCP.

        Args:
            ref: Record ref, e.g. "ADR-0007". Mutually exclusive with `id`.
            id: The record's internal ULID. Mutually exclusive with `ref`.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The record's full attributes.

        Raises:
            ToolError: Neither `ref` nor `id` was given, or wraps
                `InvalidRefError`, `NoProjectError`, `RecordNotFoundError`,
                or an infra failure.
        """
        if not ref and not id:
            raise ToolError(
                'dr_get requires either "ref" (e.g. "ADR-0007") or "id" '
                "(the internal ULID)."
            )
        try:
            record = store.get(ref, project=project) if ref else store.get_by_id(id, project=project)  # type: ignore[arg-type]
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return _record_dict(record)

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_list"])
    def dr_list(
        rec_type: RecType | None = None,
        status: Status | None = None,
        tag: str | None = None,
        unfunded: bool | None = None,
        stale: bool | None = None,
        since: str | None = None,
        limit: int = 50,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.list` (and, when `stale`, `Store.stale_reasons`) for MCP.

        Deliberately has no `persona` parameter (spec section 5 item 3):
        persona is derived 1:1 from `rec_type`, so it is not wired through
        to `Store.list`'s `persona` filter under any circumstance.

        Args:
            rec_type: Filter to one record type.
            status: Filter to one status.
            tag: Filter to records carrying this tag.
            unfunded: If True, return only ADRs/MDRs with no
                "implements-bet" link.
            stale: If True, return only records with a stale reference
                (spec section 4, "Stale references") and attach
                `stale_reasons` to each row.
            since: ISO-8601 timestamp; return only records updated strictly
                after it. The "what changed" half of the freshness signal.
            limit: Maximum number of records to return.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            `{"records": [...], "project_version": int}`, newest-created
            first. `project_version` bumps on every write to the project;
            a caller whose cached read was taken at a lower version knows
            it is stale without asking. When
            `stale` is True, each row also carries `stale_reasons`: a list
            of human-readable strings, each naming the offending ref,
            whether it came from a link or body_md prose, and the target's
            status (or that it does not exist).

        Raises:
            ToolError: Wraps `NoProjectError` or an infra failure.
        """
        try:
            records = store.list(
                rec_type=rec_type,
                status=status,
                tag=tag,
                unfunded=unfunded,
                stale=stale,
                since=since,
                limit=limit,
                project=project,
            )
            stale_reasons = store.stale_reasons(project=project) if stale else {}
            version = store.project_version(project=project)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        rows = [_summary_row(r) for r in records]
        if stale:
            for row in rows:
                row["stale_reasons"] = stale_reasons.get(row["ref"], [])
        return {"records": rows, "project_version": version}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_update"])
    def dr_update(ref: str, patch: dict[str, Any], project: str | None = None) -> dict[str, Any]:
        """Adapt `Store.update` for MCP.

        Args:
            ref: Record ref, e.g. "ADR-0007".
            patch: Mapping of attribute name to new value (wholesale
                replace). Allowed keys: title, body_md, fields, tags,
                links, deciders.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The updated record's full attributes plus a `warnings` list.

        Raises:
            ToolError: Wraps `InvalidRefError`, `ProtectedAttributeError`,
                an unknown patch key, `NoProjectError`,
                `RecordNotFoundError`, or an infra failure.
        """
        try:
            record, warnings = store.update(ref, patch, project=project)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {**_record_dict(record), "warnings": warnings}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_revise"])
    def dr_revise(
        ref: str, body_md: str, note: str, project: str | None = None
    ) -> dict[str, Any]:
        """Adapt `Store.revise` for MCP.

        Args:
            ref: Record ref, e.g. "ADR-0023".
            body_md: The new body text.
            note: Required, non-empty explanation of why the record's
                stated reasoning changed even though the decision did not.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The updated record's full attributes plus a `warnings` list
            (always empty — dr_revise never warns, including on an
            "accepted" record).

        Raises:
            ToolError: Wraps `InvalidRefError`, `MissingRevisionNoteError`,
                `NoProjectError`, `RecordNotFoundError`, or an infra
                failure.
        """
        try:
            record, warnings = store.revise(ref, body_md=body_md, note=note, project=project)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {**_record_dict(record), "warnings": warnings}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_set_status"])
    def dr_set_status(
        ref: str,
        status: Status,
        note: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.set_status` for MCP.

        Args:
            ref: Record ref, e.g. "IDR-0001".
            status: The target status.
            note: Optional free-text note for the status_history entry.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The updated record's full attributes, plus `orphaned_refs`
            (ADR/MDR refs that lost governance if this was an IDR moving to
            "rejected"/"deprecated") and a `warnings` list.

        Raises:
            ToolError: Wraps `InvalidRefError`, `InvalidTransitionError`,
                `NoProjectError`, `RecordNotFoundError`, or an infra
                failure.
        """
        try:
            record, orphan_refs, warnings = store.set_status(ref, status, note=note, project=project)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {**_record_dict(record), "orphaned_refs": orphan_refs, "warnings": warnings}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_review"])
    def dr_review(
        ref: str,
        verdict: Verdict,
        evidence: str,
        cause: Cause | None = None,
        note: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.review` for MCP.

        Args:
            ref: Record ref, e.g. "IDR-0001".
            verdict: One of "met", "missed", "mixed", "too-early".
            evidence: Required prose: what was observed, from what source,
                at what sample.
            cause: Required when `verdict == "missed"` and the record is an
                IDR. One of "market", "delivery", "both".
            note: Optional free-text note.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The updated record's full attributes plus a `warnings` list.
            When `ref` is an IDR, also includes that IDR's own
            `success_criteria`/`kill_criteria` so the verdict can be judged
            against what was written down. When `cause` is supplied, also
            includes `cause_routing` naming the accountable persona and the
            next record type.

        Raises:
            ToolError: Wraps `InvalidRefError`, `MissingCauseError`,
                `NoProjectError`, `RecordNotFoundError`, or an infra
                failure.
        """
        try:
            record, warnings = store.review(
                ref, verdict=verdict, evidence=evidence, cause=cause, note=note, project=project
            )
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

        result: dict[str, Any] = {**_record_dict(record), "warnings": warnings}
        if record.rec_type == "IDR":
            result["success_criteria"] = record.fields.get("success_criteria")
            result["kill_criteria"] = record.fields.get("kill_criteria")
        if cause is not None:
            result["cause_routing"] = _CAUSE_ROUTING[cause]
        return result

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_due"])
    def dr_due(as_of: str | None = None, project: str | None = None) -> dict[str, Any]:
        """Adapt `Store.due` for MCP.

        Args:
            as_of: ISO-8601 (or plain date) timestamp to evaluate "has
                passed" against. Defaults to now.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            `{"due": [summary_row, ...]}`, each row carrying `review_date`
            in addition to the usual summary fields, most overdue first.

        Raises:
            ToolError: Wraps `NoProjectError` or an infra failure.
        """
        try:
            records = store.due(as_of=as_of, project=project)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

        due_rows: list[dict[str, Any]] = []
        for record in records:
            row = _summary_row(record)
            row["review_date"] = record.fields.get("review_date")
            due_rows.append(row)
        return {"due": due_rows}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_escalate"])
    def dr_escalate(
        question: str,
        context: str,
        options: list[dict[str, Any]],
        recommendation: str,
        cost_of_delay: str,
        blocking: bool = False,
        governing_ref: str | None = None,
        raised_by: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.escalate` for MCP.

        Args:
            question: One sentence: what must be decided.
            context: Why this came up; what was being attempted.
            options: Alternatives as [{"option": ..., "consequence": ...,
                "reversible": bool}, ...]. Fewer than 2 warns but never
                blocks.
            recommendation: Which option the agent would take, and why.
                Empty warns but never blocks.
            cost_of_delay: What happens if this waits. Empty warns but
                never blocks.
            blocking: True if work is actually stopped right now; False if
                this is a flag for later.
            governing_ref: The IDR this work sits under, if any. Must be a
                well-formed ref if given; existence is not verified.
                Omitting it warns but never blocks.
            raised_by: Agent identity plus what it was doing when it
                stopped. Defaults to "unspecified" in the store if omitted.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The created escalation's full attributes plus a `warnings`
            list.

        Raises:
            ToolError: Wraps `NoProjectError`, `InvalidRefError`
                (malformed governing_ref), or an infra failure. Never
                raised for thin input — missing options, recommendation,
                cost_of_delay, or governing_ref only warn, per the
                escalation queue's "warn, never block" rule: a blocked
                worker must always be able to file.
        """
        try:
            escalation, warnings = store.escalate(
                question=question,
                context=context,
                options=options,
                recommendation=recommendation,
                cost_of_delay=cost_of_delay,
                blocking=blocking,
                governing_ref=governing_ref,
                raised_by=raised_by,
                project=project,
            )
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {**_escalation_dict(escalation), "warnings": warnings}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_escalations"])
    def dr_escalations(
        status: EscalationStatus = "open",
        blocking_only: bool = False,
        unrecorded: bool = False,
        limit: int = 50,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.escalations` for MCP.

        Args:
            status: Filter to one escalation status. Defaults to "open".
                Ignored when `unrecorded` is True.
            blocking_only: If True, return only escalations with
                blocking=True.
            unrecorded: If True, return escalations that are
                status="answered", produced_ref is null, and no_record is
                not true — the "no record, no decision" sweep. Overrides
                `status`; still composes with `blocking_only`.
            limit: Maximum number of escalations to return.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            `{"escalations": [escalation_dict, ...]}`, ordered blocking
            first, then newest within each group.

        Raises:
            ToolError: Wraps `NoProjectError` or an infra failure.
        """
        try:
            escalations = store.escalations(
                status=status,
                blocking_only=blocking_only,
                unrecorded=unrecorded,
                limit=limit,
                project=project,
            )
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {"escalations": [_escalation_dict(e) for e in escalations]}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_answer"])
    def dr_answer(
        ref: str,
        resolution: str,
        produces: ProducesInput | None = None,
        no_record: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.answer` for MCP.

        Args:
            ref: Escalation ref, e.g. "ESC-0007".
            resolution: The operator's decision, in their own words.
            produces: Optional record to create atomically alongside the
                resolution and link via produced_ref. Omit when the
                resolution needs no record — "don't do that" is complete
                on its own.
            no_record: True to confirm that no record is genuinely needed
                here — suppresses the no-record warning. Ignored when
                `produces` is given.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The updated escalation's full attributes, plus
            `produced_record` (the created record's full attributes, or
            `None` if `produces` was omitted) and a `warnings` list
            (governance warnings on the created record when one was
            produced, or the "no record, no decision" warning when
            neither `produces` nor `no_record` was given).

        Raises:
            ToolError: Wraps `InvalidRefError`, `NoProjectError`,
                `EscalationNotFoundError`, `EscalationAlreadyResolvedError`,
                a `produces` payload missing a required key, or an infra
                failure.
        """
        try:
            escalation, record, warnings = store.answer(
                ref,
                resolution=resolution,
                produces=produces.model_dump() if produces is not None else None,
                no_record=no_record,
                project=project,
            )
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {
            **_escalation_dict(escalation),
            "produced_record": _record_dict(record) if record is not None else None,
            "warnings": warnings,
        }

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_withdraw"])
    def dr_withdraw(
        ref: str,
        reason: str,
        duplicate_of: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.withdraw` for MCP.

        Args:
            ref: Escalation ref, e.g. "ESC-0007".
            reason: Required, non-empty explanation of why the question
                became moot. May be brief when `duplicate_of` is given.
            duplicate_of: Ref of another escalation this one duplicates,
                if any. Must be a well-formed ref, must not equal `ref`,
                and must resolve to an existing escalation.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The updated escalation's full attributes plus a `warnings`
            list (the duplicate-of-a-withdrawn-duplicate chain warning,
            when applicable).

        Raises:
            ToolError: Wraps `InvalidRefError`, `MissingWithdrawalReasonError`,
                `NoProjectError`, `EscalationNotFoundError`,
                `EscalationAlreadyResolvedError`, or an infra failure.
        """
        try:
            escalation, warnings = store.withdraw(
                ref, reason=reason, duplicate_of=duplicate_of, project=project
            )
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {**_escalation_dict(escalation), "warnings": warnings}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_adopt_successor"])
    def dr_adopt_successor(
        old_ref: str, successor_ref: str, project: str | None = None
    ) -> dict[str, Any]:
        """Adapt `Store.adopt_successor` for MCP.

        Args:
            old_ref: Ref of the record being superseded.
            successor_ref: Ref of the existing record that replaces it.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The successor record, plus `warnings` and `project_version`.

        Raises:
            ToolError: Wraps InvalidRefError, RecordNotFoundError,
                NoProjectError, or an infra failure.
        """
        try:
            successor, warnings = store.adopt_successor(old_ref, successor_ref, project=project)
            version = store.project_version(project=project)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {**_record_dict(successor), "warnings": warnings, "project_version": version}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_supersede"])
    def dr_supersede(
        old_ref: str, new_record: NewRecordInput, project: str | None = None
    ) -> dict[str, Any]:
        """Adapt `Store.supersede` for MCP.

        Args:
            old_ref: Ref of the record being superseded.
            new_record: The replacement record's content; only `title` is
                required.
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            The newly created record's full attributes plus a `warnings`
            list.

        Raises:
            ToolError: Wraps `InvalidRefError`, a missing title,
                `NoProjectError`, `RecordNotFoundError`, or an infra
                failure.
        """
        try:
            record, warnings = store.supersede(
                old_ref, new_record.model_dump(exclude_none=True), project=project
            )
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {**_record_dict(record), "warnings": warnings}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_search"])
    def dr_search(
        query: str,
        rec_type: RecType | None = None,
        all_projects: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Adapt `Store.search` for MCP.

        Args:
            query: Search terms, split on whitespace (case-insensitive);
                a record matches if it contains ANY term.
            rec_type: Filter to one record type.
            all_projects: If True, search across all projects.
            project: Explicit project slug; falls back to `DR_PROJECT`.
                Ignored when `all_projects` is True.

        Returns:
            `{"results": [summary_row, ...]}`, each row carrying
            `matched_terms` (the count of distinct query terms that
            matched), ranked: exact full-phrase match first, then
            `matched_terms` descending, then `created_at` descending.

        Raises:
            ToolError: Wraps `NoProjectError` (when `all_projects` is
                False) or an infra failure.
        """
        try:
            results = store.search(query, rec_type=rec_type, all_projects=all_projects, project=project)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        rows: list[dict[str, Any]] = []
        for record, matched_terms in results:
            row = _summary_row(record)
            row["matched_terms"] = matched_terms
            rows.append(row)
        return {"results": rows}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_projects"])
    def dr_projects() -> dict[str, Any]:
        """Adapt `Store.projects` for MCP.

        Returns:
            `{"projects": [{"project", "created_at", "record_counts"}, ...]}`.

        Raises:
            ToolError: Wraps a table-missing or credentials failure.
        """
        try:
            projects = store.projects()
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {"projects": projects}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_template"])
    def dr_template(rec_type: RecType) -> dict[str, Any]:
        """Render the markdown skeleton for one record type.

        Pure content generation — no `Store` call, no project required.

        Args:
            rec_type: One of "ADR", "IDR", "MDR".

        Returns:
            `{"rec_type", "persona", "fields", "template_md"}`.
        """
        return _render_template(rec_type)

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_export"])
    def dr_export(
        ref: str | None = None,
        rec_type: RecType | None = None,
        out_dir: str = "docs/decisions",
        project: str | None = None,
    ) -> dict[str, Any]:
        """Render one or more records to markdown files on disk.

        Args:
            ref: Export exactly this record. Mutually exclusive in effect
                with `rec_type` (if both are given, `ref` wins).
            rec_type: Export every record of this type. Ignored if `ref` is
                given; if neither is given, exports every record in the
                project.
            out_dir: Output directory, relative to the current working
                directory. Defaults to "docs/decisions".
            project: Explicit project slug; falls back to `DR_PROJECT`.

        Returns:
            `{"written": [path, ...]}`.

        Raises:
            ToolError: Wraps `InvalidRefError`, `NoProjectError`,
                `RecordNotFoundError`, or an infra failure.
        """
        try:
            if ref:
                records = [store.get(ref, project=project)]
            else:
                records = store.list(rec_type=rec_type, limit=_EXPORT_LIST_LIMIT, project=project)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for record in records:
            file_path = out_path / f"{record.ref}-{_slugify(record.title)}.md"
            file_path.write_text(_render_export_markdown(record), encoding="utf-8")
            written.append(str(file_path))
        return {"written": written}

    def _resolve_event_queue() -> EventQueue:
        """Return the injected `EventQueue`, or build one from env.

        Constructing lazily per call -- rather than once at server
        startup -- means a server that has never had DR_EVENTS_QUEUE_URL
        set can still start and serve the other sixteen tools; the
        failure (or success) only shows up when a caller actually reaches
        for dr_events_pending / dr_events_ack.

        Returns:
            `event_queue` if one was injected into `build_server`,
            otherwise a new `EventQueue()` built from
            `DR_EVENTS_QUEUE_URL`.

        Raises:
            EventsQueueNotConfiguredError: No `event_queue` was injected
                and `DR_EVENTS_QUEUE_URL` is unset.
        """
        return event_queue if event_queue is not None else EventQueue()

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_events_pending"])
    def dr_events_pending(max_messages: int = 10) -> dict[str, Any]:
        """Adapt `EventQueue.pending` for MCP.

        Args:
            max_messages: Maximum number of events to receive.

        Returns:
            `{"events": [pending_event_dict, ...]}`, each carrying its own
            `receipt_handle` for a later `dr_events_ack` call.

        Raises:
            ToolError: Wraps `EventsQueueNotConfiguredError` or
                `CredentialsError`.
        """
        try:
            queue = _resolve_event_queue()
            pending_events = queue.pending(max_messages=max_messages)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {"events": [event.model_dump() for event in pending_events]}

    @server.tool(description=_TOOL_DESCRIPTIONS["dr_events_ack"])
    def dr_events_ack(receipt_handles: list[str]) -> dict[str, Any]:
        """Adapt `EventQueue.ack` for MCP.

        Args:
            receipt_handles: Receipt handles of events already applied.

        Returns:
            `{"deleted": count}` -- `count` may be less than
            `len(receipt_handles)` on partial failure; see the tool
            description.

        Raises:
            ToolError: Wraps `EventsQueueNotConfiguredError` or
                `CredentialsError`.
        """
        try:
            queue = _resolve_event_queue()
            deleted = queue.ack(receipt_handles)
        except (TriadDRError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {"deleted": deleted}

    return server


# ----------------------------------------------------------------------
# Startup / console-script entry point
# ----------------------------------------------------------------------


def _check_table_on_startup(table_name: str, resource: Any) -> None:
    """Run one `DescribeTable` at startup and log the result — never raise.

    Per spec section 6: the server does not auto-create the table and does
    not refuse to start when it is missing. This logs one clear stderr line
    (via structlog, already pinned to stderr) so the operator knows to run
    `make deploy`; individual tool calls surface the same remediation when
    they hit the missing table themselves via `Store`.

    Args:
        table_name: The DynamoDB table name to check.
        resource: A boto3 DynamoDB ServiceResource to check against.
    """
    try:
        resource.meta.client.describe_table(TableName=table_name)
    except (
        UnauthorizedSSOTokenError,
        SSOTokenLoadError,
        TokenRetrievalError,
        NoCredentialsError,
    ):
        logger.error(
            "startup.credentials_check_failed",
            remediation=str(CredentialsError(os.environ.get("AWS_PROFILE"))),
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            logger.error(
                "startup.table_missing", remediation=str(TableMissingError(table_name))
            )
        else:
            logger.error("startup.table_check_failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - deliberate: startup must never crash the server.
        logger.error("startup.table_check_unexpected_error", error=str(exc))


def main() -> None:
    """Console-script entry point (`triad-dr-mcp`).

    Resolves the table name from `DR_TABLE` (default
    "triad-decision-registers", matching `cli.py`'s pattern), runs the
    startup table check, builds the `Store` and `MCPServer`, and serves
    over stdio until the client disconnects. Never exits on a missing
    table or bad credentials — those degrade to per-call errors instead.

    Returns:
        None. Blocks for the life of the stdio session.
    """
    table_name = os.environ.get("DR_TABLE", DEFAULT_TABLE_NAME)
    config = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 3})
    resource = boto3.resource("dynamodb", config=config)

    _check_table_on_startup(table_name, resource)

    store = Store(table_name=table_name, resource=resource)
    server = build_server(store)
    server.run("stdio")


if __name__ == "__main__":
    main()
