# Changelog

Version is bumped on every release so an agent working from cached guidance can tell it is out of date. The version appears at the top of `skills/decision-registers/SKILL.md`, in `.claude-plugin/plugin.json`, and in `.claude-plugin/marketplace.json`; the MCP server package in `pyproject.toml` ships in lockstep.

Format: newest first. Entries say what changed and, where it matters, what it cost to find out.

## 0.8.0

### `dr_withdraw` — the third exit, which was declared but unreachable

`"withdrawn"` was a declared `EscalationStatus` that **nothing in the codebase
could set**. It appeared in the type alias and one docstring and nowhere else,
so the only exit from an open escalation was `dr_answer`.

That is almost certainly what produced the reported 25-of-26. Clearing a moot
item — already resolved elsewhere, overtaken, a duplicate — meant recording
that a decision was made when none was. Those then became indistinguishable
from real decisions that leaked without a record, corrupting the
`unrecorded=True` sweep built to catch exactly those.

Three honest endings, all now reachable:

- **`answered`** — a decision was made. It should produce a record.
- **`withdrawn`** — no decision was needed. `dr_withdraw(ref, reason)`, with a
  **required** reason: a withdrawal with no explanation is indistinguishable
  from abandonment, which is the thing this exists to tell apart.
- **`open`** — genuinely still waiting.

`duplicate_of=` links an escalation to the one that supersedes it. Self-
reference is rejected (a production escalation's context claimed it duplicated
itself), the target must exist, and a duplicate-of-a-duplicate warns while
naming the ultimate target so a reader is not left following a dead chain.

Withdrawing an answered escalation is refused — that would erase a decision
that was actually made.

### Agents now maintain the queue, not just file to it

- **An agent may withdraw an escalation it raised.** The second thing it may do
  to the register unprompted, and it follows from the first: retracting your
  own question is not deciding anything. It may not withdraw someone else's,
  and may never answer one.
- Check the queue before filing a duplicate; on returning to escalated work,
  confirm the answer landed and produced a record.
- `/triad-dr:answer` withdraws moot and duplicate items during its cleaning
  pass instead of answering them, and closes by sweeping `unrecorded=true`.

## 0.7.0

### "No record, no decision" — the other half of "no bet, no work"

*Reported from real use: 25 of 26 answered escalations produced no record. A
correctly-made decision about deployment credentials was invisible to an
agent reading the ADR set, which then proceeded on the wrong authentication
model — because the ADR genuinely did not contain the answer.*

- **`dr_answer` warns when it produces no record**, naming what that costs: the
  decision now exists only inside the escalation, and nothing that reads the
  register will find it.
- **`no_record=True`** marks a genuinely record-less answer as deliberate.
  Without it, "decided none was needed" and "forgot" are indistinguishable
  afterwards, and only one of them is acceptable.
- **`dr_escalations(unrecorded=True)`** sweeps for the ones that slipped:
  answered, no record produced, not marked deliberate.

### `dr_revise` — when the decision stands but its reasoning does not

*One ADR justified itself with a benefit a later ADR removed.* Supersede was
too heavy (the decision had not changed); update too quiet (it rewrites an
accepted record's prose with only a warning, erasing what it used to say).
`dr_revise` stamps a dated, explained revision and **preserves the prior text**.
The note is required — a revision without one is just a quieter edit.

The three-way line: `dr_update` for typos, `dr_revise` when the reasoning
changed, `dr_supersede` when the decision did.

### `dr_search` was not a keyword search at all

**A live bug with the worst failure mode a governance tool has.** The whole
query was matched as one literal substring, so a seven-word search looked for
that exact 60-character sequence and returned nothing while a record containing
the phrase sat right there. The description called it a "keyword match," which
is precisely what invites a multi-term query — and an agent trusting the empty
result concludes no decision exists.

Now tokenized: terms are OR-ed, results ranked by distinct terms matched with
an exact full-phrase match on top, and each row carries its `matched_terms`
count so a strong hit is distinguishable from an incidental one.

## 0.6.0

### A skill for agents that consume the stream

- **`decision-subscriber` skill added**, for workers woken by a decision rather
  than by a person. Three things it exists to prevent, none of which the
  interactive `/triad-dr:apply` path has to worry about:
  - **Ack before apply.** Acking an event you did not apply loses a decision
    permanently, with no error and no way to detect it afterwards. Apply first,
    ack only what landed, and leave anything that failed to redeliver.
  - **Non-idempotent actions under redelivery.** At-least-once means duplicates
    are normal. Setting a label twice is invisible; creating a branch or posting
    an unmarked comment twice is not. Tag what you write with the decision ref
    so the second delivery can find the first attempt.
  - **Treating an event as authorization.** An accepted bet arriving on the
    queue tells a worker something is funded; it grants no permission. Authority
    still comes only from an accepted IDR's `delegation` field. A subscriber may
    label, comment and flag — not close, reassign, or tell anyone their work
    stopped. Being woken by a decision makes you the messenger, not the decider.
- Notes that each subscriber needing full coverage wants **its own queue**: two
  workers sharing one queue split events between them rather than each seeing
  all of them — right for a work pool, wrong for two agents with different jobs.

## 0.5.0

### The subscriber half: a durable queue and tools to drain it

- **`DrEventsQueue`** — an SQS queue subscribed to the events topic with raw
  message delivery, 14-day retention, and its own DLQ after 3 failed receives.
  SNS is fan-out only: without a queue, an agent that is not running when a
  decision lands never sees it. Its URL is the `DrEventsQueueUrl` stack output.
- **`dr_events_pending` / `dr_events_ack`** — receive and acknowledge, as two
  separate calls on purpose. SQS is at-least-once, so a caller must be able to
  apply an event and only *then* delete it. Acking on receive would silently
  lose any event whose application failed, which is the one failure mode a
  routing layer must not have — IDR-0003's "no decision lost" criterion is not
  satisfiable by a pipeline that drops quietly. An unacked event redelivers
  after the visibility timeout; that is the feature, not a bug.

Verified end to end against deployed AWS: a record written and accepted
produced exactly two correctly-typed events through DynamoDB stream → Lambda →
SNS → SQS, with the status change carrying from/to, and the queue empty after
ack.

## 0.4.0

### Decisions are published, not just stored

- **`/triad-dr:apply` added.** Drains pending decision events and carries them
  down to work items. Applies only mechanical, reversible consequences —
  labelling and commenting — and escalates anything that is not. A killed bet's
  orphaned work gets flagged and reported, never closed or reassigned: some of
  it deserves re-homing under a different bet, and that is a decision rather
  than a consequence.
- Skill guidance for the event stream: the three published event types, the
  filterable message attributes, and why the payload excludes `body_md`.
- **SNS is fan-out, not buffering** — documented, because it is the thing that
  will bite. An agent not running when a decision lands never sees it, so
  anything that must not miss decisions subscribes through a queue rather than
  directly to the topic.
- Content edits and escalations deliberately do not publish. `event_type` is in
  the message attributes from the start so escalations can be added later
  without changing a single subscriber.

## 0.3.0

### Deferred supersede, and a supersession you can link after the fact

- **`dr_supersede` no longer flips the predecessor immediately.** The link lands
  at once so the chain is machine-followable, but the old record keeps its
  status until the replacement is `accepted`. Under default-deny every record is
  human-ratified, so flipping on creation left a gap with no accepted record at
  all — the old one retired, the replacement merely proposed. A replacement
  created already-accepted still flips in the same transaction.
- **A rejected replacement releases its predecessor.** `superseded_by` is
  cleared, since nothing is replacing it after all; the rejected record keeps
  its own `supersedes` as a record of intent.
- **`dr_adopt_successor(old_ref, successor_ref)` added.** *Reported from real
  use:* `supersedes`/`superseded_by` are structural and cannot be patched, so a
  supersession drafted standalone and accepted later could never be linked at
  all — one operator ended up with a record sitting at `superseded` with an
  empty cross-link, the relationship surviving only in prose and status notes
  that no query can follow. This links an existing record as the successor, and
  repairs exactly that state without disturbing the status.

### Freshness, fields merge, {{ref}}

- **`project_version`** on every `dr_list` response — a counter bumped by every
  write to the project. A worker briefed at v47 whose next call reports v52
  knows its cached read is stale. *Cost of not having it: two workers briefed
  from a superseded topology, one of whom wrote an entire runbook a newer
  record had already made obsolete.*
- **`dr_list(since="<ISO>")`** — what changed, once the version says something
  did. Different units on purpose: the version answers *whether*, `since`
  answers *what*.
- **`dr_update` merges `fields`** key-by-key; everything else still replaces
  wholesale. Changing one key no longer requires re-transmitting the other six
  verbatim, where a single dropped key silently destroyed it on an accepted
  record. Explicit null deletes a key. *This nearly cost a founding bet.*
- `{"delegation": ...}` now names the correction instead of "unknown patch key".
- **`{{ref}}`** in `body_md` or any `fields` value is substituted with the
  assigned ref on create, and a wrong ref hand-written into the first heading
  warns. Refs are assigned at write time, so a self-reference typed while
  drafting is a guess — and under concurrency it was wrong twice in one evening.

## 0.2.0

### Escalation queue reaches the operator's rituals

- `/triad-dr:answer` added — works the escalation queue, blocking items first, one at a time.
- `/triad-dr:answer` now cleans the queue before spending attention on it: age with precondition re-check, items already resolved by work done elsewhere, duplicates collapsed by what the decision *is* rather than how it was worded, and bundled items split before answering. Reports cleaned count against raw. *Found in use: roughly a fifth of a 16-item queue was already fiction.*
- The delegation diagnosis moved out of a tool docstring and into `/triad-dr:status` and `/triad-dr:answer`, stated as a number. A pile of blocking escalations against `delegation: None` is an authority problem, not a workload; widening one grant retires the class, answering fourteen retires fourteen items.

### Governance gaps closed from first production use

- **`requires-attestation`** link relation — the cross-persona commitment gate. An IDR names Builder and Seller attestations as preconditions; accepting warns for each one not itself accepted. Warns, never blocks.
- **IDR `stage`: `spike` | `commitment`** — the two have opposite defaults on failure, and one record spanning both under a single kill criterion always mishandles its own.
- **IDR `cause_evidence`** — what observation will distinguish `market` from `delivery` at review, decided at bet time. Without it every miss defaults to `market`, which is the expensive mistake the register exists to prevent.
- **MDR `value_delivered` and `time_to_value`** — what the customer gets at this phase and how long they wait. A phase that asks for commitment while deferring all value is a common shape and was previously invisible.
- **Placeholder lint on accept** — catches `[X]`, `TBD`, `TODO` and friends in `fields` and `body_md`, naming each location. Does not false-positive on markdown checkboxes.
- **`dr_list(stale=true)`** — records whose links or `body_md` prose point at superseded, deprecated, or missing refs. `unfunded=true` never caught these, because a link to a dead bet still resolves.
- **Work-item convention documented** — `bet::IDR-0003` required on every tracker item, `adr::` / `mdr::` as warranted, query by label and never sync. Carries "no bet, no work" down to the layer where it hides better.

### Fixes

- **`dr_due` no longer returns retired bets.** A superseded IDR handed its review obligation to the record that replaced it; a rejected or deprecated one is dead. Every supersede was permanently adding noise to the one report the register owes its operator. *Found by dogfooding within a minute of first real use.* `draft`/`proposed` still surface — an undecided bet past its own date is a lapsed intention worth seeing.
- `/triad-dr:status` runs `stale=true` every time rather than on suspicion. Opt-in checks do not get run, which is how a stale briefing reached two workers.

## 0.1.0

Initial release. Three registers (ADR / IDR / MDR), the escalation queue, 12 MCP tools, CDK-deployed DynamoDB backend, and four commands: `bet`, `review`, `record`, `status`.
