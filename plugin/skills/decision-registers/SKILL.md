---
name: decision-registers
description: Use when working in a repo wired to the triad-dr decision registers — deciding which register a decision belongs in, writing or reviewing an investment bet (IDR), recording an architecture decision (ADR) or a market/user decision (MDR), judging a bet at its review date, or checking what work is ungoverned. Triggers on "record this decision", "write an ADR", "what's our bet here", "is this funded", "review the bet", "which register", "decision record", "IDR", "MDR", "kill criteria", "review date", "what am I allowed to decide".
---

# Decision Registers

**triad-dr v0.9.0.** If you are working from cached guidance or a briefing that cites an older version, re-read this skill before acting — the rules below have changed between versions, and `CHANGELOG.md` in the plugin root says how. Version is bumped on every release for exactly this reason.

Three registers, one hierarchy. Every record belongs to exactly one persona, and one operator holds all three. The registers exist to keep those three kinds of accountability from blurring into each other.

## The personas

**Investor — IDR — allocates.** Decides what gets funded, for how long, and on what terms. Owns the bet, the resources committed, the success criteria, the kill criteria, the review date. The Investor does not decide *how*; the Investor decides *whether, how much, and until when*.

**Seller — MDR — knows the market, owns the user.** The register's only channel to the outside world. Owns audience, positioning, channel motion, pricing narrative, launch experiments — **and the user experience**: what a real person must be able to do, and whether they could. Voice of customer, user research, usability evidence, jobs-to-be-done, win/loss, churn reasons are all Seller instruments and their findings go in MDRs.

**Builder — ADR — delivers.** Owns architecture, technology selection, data model, operational posture, and the technical shape of the experience. The Builder delivers the experience the Seller defines.

There is no fourth persona. **A user-research finding is an MDR, not an ADR and not a separate record type.** Putting it in an ADR is a category error — it hands the Builder accountability for a market question.

## The rules that matter

**No bet, no work.** Every ADR and MDR traces to a governing IDR via an `implements-bet` link. Work with no bet behind it is ungoverned, and `dr_list(unfunded=true)` will keep surfacing it. If someone asks you to record an ADR and there is no accepted IDR that funds it, say so before writing the record — that is the finding, not an obstacle to route around.

Maintenance is not an exemption. Each project carries a **standing operations IDR** covering upkeep — dependency bumps, build health, infrastructure, chores — written like any other bet, with an appetite and a kill criterion. Chores link to it.

**You do not write the register on your own initiative.** A record is created when the operator asks for it, as the product of a working session. You draft what was decided together; they own it. Never file a record in the background, never decide unilaterally that something merits an ADR, never batch-generate records to "catch up." A register nobody reads is worthless, and volume is how it gets there.

**Authority is default-deny, and only an IDR grants it.** Absent an explicit grant in an accepted IDR's `delegation` field, every record is human-owned. Grants are scoped to the bet and expire with it — supersede or deprecate the IDR and the delegation dies. The complete current authority set for a project is `dr_list(rec_type="IDR", status="accepted")`. Read it before assuming you may accept anything.

**When you hit the edge of that authority, escalate — do not decide.** See "Escalation" below. This is the single most important behavior in this document.

**However you put a decision to the operator — filed as an escalation or asked live in session — frame it the same way.** The brief under "Escalation" is the format for both, and its ceiling is fifteen lines.

**Warn, never block.** The tools do not refuse writes on governance grounds. An unfunded ADR, an MDR with no evidence plan — these warn and succeed. Surface the warning to the operator in plain language rather than swallowing it; the warning is the product.

**Superseding beats editing.** Once a record is `accepted`, prefer `dr_supersede` over `dr_update`. The point of the register is the trail, and editing an accepted record erases it.

The status flip is **deferred**: the pair is linked immediately, and the predecessor stays as it is until the replacement is accepted. No gap with nothing accepted in it. If the replacement is rejected instead, the predecessor is released.

If the replacement already exists — drafted standalone before you decided it was a supersession — use **`dr_adopt_successor(old_ref, successor_ref)`**. Never try to patch `supersedes`/`superseded_by` with `dr_update`; they are structural and rejected there, and a supersession recorded only in prose is one no query can follow.

## The lifecycle

`draft → proposed → accepted → (superseded | deprecated)`, and `proposed → rejected`.

Reviews are not status. `dr_review` records what happened against the stated criteria; deciding what to *do* about it is a separate deliberate act. A missed criterion does not auto-deprecate anything — persevering with a revised bet is a legitimate response, and the tool does not get to make that call.

**Killing a bet orphans what it funded.** When an IDR goes to `rejected` or `deprecated`, the ADRs and MDRs linked to it lose their funding. The tool returns those refs; it does not cascade the status change, because re-homing that work under a different bet is a decision. `superseded` is different — a superseded bet hands its work to the record that replaced it.

## Escalation — the one thing you file on your own initiative

Everywhere else in this document, you draft only what the operator asked for. The escalation queue is the exception, and it exists because default-deny means you will routinely reach a point you are not authorized to pass. You need somewhere to go. Stopping silently is worse — silence looks exactly like progress until someone checks — and deciding anyway is worst of all.

**You do not make important decisions.** Not ones you're confident about, not ones where the answer seems obvious, not ones where escalating feels like an interruption. File an `ESC` and move on to something else, or stop.

**When to escalate**

- The action is a **one-way door** — irreversible, or expensive to undo.
- It exceeds what the governing IDR's `delegation` field grants you.
- The options differ in a way that changes the bet.
- **It's genuinely ambiguous and the stakes are material.** Ambiguity is a reason to escalate, not a reason to pick.

**When to just decide** — and then record it: reversible, low-stakes, mechanical. The naming-a-variable tier.

**When in doubt, escalate.** The two failure modes are not symmetric. Escalating too much produces a slower inbox, which is visible and self-correcting — the operator widens a grant. Escalating too little produces an important decision made by something with no accountability, found much later, already built on. Take the cheap failure.

**The bar for a good escalation: an un-initiated leader could read it once and decide.** The same bar applies when you put a decision to the operator live in session instead of filing it. The channel differs; the framing does not.

This is stricter than it sounds. You have a whole session of context the operator does not. The characteristic failure is a question that only makes sense to the asker — "should I use approach B?" is unanswerable to anyone who wasn't watching. Write it for someone walking in cold: no session history, no repo knowledge, deciding from a phone without opening anything.

**What goes in:**

- **The decision**, in one sentence, phrased so that "A" or "B" is a complete answer.
- **The background**, not just the question. What is being built, and what forced the choice *now*.
- **Options with consequences** — two or three, each with what it costs and whether it is reversible.
- **Your recommendation**, and the reason. You did the work; say what you'd do.
- **The cost of delay.** "Nothing breaks until Friday" is a fine and useful answer.
- **The governing bet**, when there is one.
- **`blocking`** — is work actually stopped right now, or is this a flag for later? These are different emergencies and the queue sorts on it. Escalations only; asking live is its own kind of stopped.

**What stays out**, because noise costs the operator the same attention the decision does:

- The investigation narrative — what you tried, what failed, what you read. If a finding changes the choice it belongs in the options; if it doesn't, it doesn't appear anywhere.
- A third option you don't mean. Two real ones beat three where one is filler.
- Hedging around the recommendation, and caveats that apply to every option equally — a caveat that doesn't discriminate between options can't inform the choice.
- Anything already in the register. Cite the ref; don't restate it.

**Fifteen lines is the ceiling.** If it doesn't fit, the decision isn't framed yet — frame it before you raise it. A brief the operator has to reconstruct is one they will put down.

**One at a time.** If several are queued, list them by one-line title, say which one is actually blocking work, and raise them individually. Five at once is a decision about which decision to make first, which is not the one you meant to ask.

Call `dr_escalate(...)` to file. Answering is the operator's job via `/triad-dr:answer`.

**Then keep it current.** The escalation is yours until it is closed: check the queue before filing a duplicate, withdraw it yourself with `dr_withdraw(ref, reason)` if the question becomes moot, and when you come back to the work, confirm the answer landed and produced a record. An escalation nobody closed is a decision the operator will be asked to make twice.

## Four patterns that are easy to get wrong

**Spike ≠ commitment.** A spike is disposable by design — its kill criterion is its appetite, and a spike that misses is informative, not a loss. A commitment is not meant to be thrown away, and a miss is a real loss. One record covering both, under one kill criterion, will always mishandle its own failure because the two have opposite defaults. Split them: the spike answers a question, and its answer is the input to the commitment. IDRs carry `stage: spike | commitment`.

**The commitment gate.** The three personas are independent, except for the one decision that isn't: "the business goes all in." A launch is an Investor call that is only sound if the Builder has attested it's operationally ready and the Seller has attested acquisition and path-to-value hold. Name them as preconditions — `links: [{ref, relation: "requires-attestation"}]` — and accepting the IDR warns for each attestation not itself accepted. It doesn't block; it makes committing early a thing that got written down rather than reconstructed.

**Cause evidence is designed in, or it doesn't exist.** See below for why `market` vs `delivery` matters. The trap is that it cannot be judged from outcome data — a missed number only says the number was missed, which reads as the market declining. So every IDR carries `cause_evidence`: what observation will distinguish "they saw the intended thing and didn't want it" from "they never saw it." Decided at bet time, before anyone knows the answer. **A bet without it can only ever be charged to the market.**

**What the customer gets, and when.** `value_delivered` and `time_to_value` on every MDR — the benefit received *in this phase*, and the wait between a person's first commitment and receiving it. A phase that asks for commitment while deferring all value is a common and dangerous shape, and nothing surfaces it unless you ask. "No benefit this phase" is a legitimate answer; leaving it unasked is not.

## Carrying "no bet, no work" to the work items

The rule stops at the decision layer unless something carries it down. A spotless register sits happily above a backlog full of tasks no bet funds — the same failure, one layer down, where a tracker full of tickets looks like progress.

The register never learns about the tracker; the tracker references the register:

- **`bet::IDR-0003` required on every work item**, enforced at creation where the tracker supports required fields.
- **`adr::ADR-0007` / `mdr::MDR-0005` as warranted** — the constraints travel with the ticket, which is what "build it right the first time" looks like mechanically.
- **Query by label, never sync.** Killing a bet makes that same query its orphan list, so the kill cascade reaches the tracker with no new machinery.

One direction only. Two systems of record drifting apart is worse than one plus a convention.

## Decisions are published, not just stored

Every decision record change publishes to an SNS topic, so work can be routed from a decision rather than discovered by someone remembering to look. Three event types: `record.created`, `record.status_changed`, and `review.recorded`. Content edits do not publish — a patch to a draft nobody has read is not news. Escalations do not publish either, though `event_type` exists in the message attributes so they can be added later without touching a subscriber.

Messages carry filterable attributes — `project`, `rec_type`, `persona`, `status`, `event_type` — so a subscriber scopes itself with an SNS filter policy rather than receiving everything and discarding. The body is a summary, deliberately without `body_md`; a consumer that needs the prose calls `dr_get`, and `body_md_excluded: true` says the omission was by design.

**SNS is fan-out, not buffering.** An agent that is not running when a decision lands never sees it. Anything that must not miss decisions subscribes through an SQS queue, not directly to the topic.

**One queue per subscriber.** SQS delivers each message to exactly one receiver, so two workers sharing a queue split the events rather than each seeing all of them — and neither notices, because both report success on the half they got. Queues are declared in the CDK stack's subscriber list, each scoped server-side by an SNS filter policy over `project`, `rec_type`, `persona`, `status`, `event_type`.

Writing a worker that consumes this stream? Load the **`decision-subscriber`** skill — it covers the at-least-once contract, idempotency under redelivery, and the authority boundary an event does not cross.

To act on what has been published interactively, use **`/triad-dr:apply`** — it drains pending events and carries them down to work items, labelling and commenting where the consequence is mechanical and escalating where it is not. Do not invoke it on your own initiative: it changes work items, and that is not autonomy the register grants.

## Your cached read goes stale, and acting on it wastes real work

Concurrent sessions write to the same register. A briefing taken at the start of a session is a snapshot, and by the time a worker acts on it, records may have been superseded by someone else. This has already cost a full runbook written against a topology another session had made obsolete.

- **Every tool response carries `project_version`**, a counter that bumps on every write to that project. Briefed at v47 and a later call returns v52? Something changed — re-read before acting on anything you cached.
- **`dr_list(since="<timestamp>")`** answers what changed, once the version tells you something did.
- **Before briefing a worker off a cached read, re-check.** The cost of one extra call is nothing against the cost of building the wrong thing.
- **Run `dr_list(stale=true)` as part of any status read**, not only when you suspect a problem. Opt-in checks don't get run.

## An escalation is open until you close it, and you own the ones you raised

Filing an escalation is not the end of your responsibility for it. A queue full of items that were quietly resolved elsewhere is worse than an empty one, because the operator sits down to work it and finds a fifth of it is fiction — and stops trusting the rest.

**Every escalation has exactly three honest endings:**

- **`answered`** — a decision was made. It should produce a record. See "No record, no decision" below.
- **`withdrawn`** — no decision was needed. The question became moot: resolved elsewhere, overtaken, or a duplicate of another escalation. `dr_withdraw(ref, reason)`, and `duplicate_of=` when it is the same underlying decision as another item.
- **still `open`** — genuinely still waiting on the operator.

**Do not run a moot escalation through `dr_answer` to clear it.** That records a decision that was never made, with no record behind it, and it becomes indistinguishable from a real decision that leaked. That is exactly how a production queue ended up with 25 of 26 answered escalations producing nothing. Answer means decided; withdraw means unnecessary.

**You may withdraw an escalation you raised.** This is the second thing you may do to the register unprompted, and it follows from the first: if you may file a question on your own initiative, you may retract it when you find the answer yourself or the question stops mattering. State the reason plainly — "resolved by ADR-0031", "duplicate of ESC-0007", "the branch this blocked was abandoned".

**You may not withdraw an escalation you did not raise, and you may never answer one.** Answering is a decision, and decisions are the operator's.

**Before filing, check the queue.** If an open escalation already asks your question, add to it or reference it rather than filing a second one. Two escalations for one decision means the operator answers it twice or answers one and abandons the other.

**When you return to work you escalated from, close the loop.** If the operator answered while you were away, apply the answer and confirm it produced a record. If the question died on its own, withdraw it. The queue reflecting reality is part of the job, not an afterthought.

## No record, no decision

The register enforces *no bet, no work*. This is its other half, and it is the one the register cannot see on its own.

An answered escalation **is a decision.** If it produced no ADR, IDR or MDR, that decision exists only as prose inside the escalation, and nothing that reads the register will find it — so an agent briefing itself from the records concludes no decision was made and proceeds on the old assumption. It has already cost real work here: a correctly-made decision about deployment credentials was invisible to an agent reading the ADR set, which then built on the wrong authentication model, because the ADR genuinely did not say otherwise.

So: **when you answer an escalation, produce the record**, unless the answer genuinely needs none — in which case say so explicitly with `no_record=True` rather than leaving it blank. "Decided no record was needed" and "forgot" look identical afterwards, and only one is acceptable.

`dr_escalations(status="answered", unrecorded=true)` sweeps for the ones that slipped.

## Self-referencing a record you haven't created yet

Refs are assigned at write time, so a ref you type while drafting is a guess — and under concurrency it is often wrong, because another session may claim the number mid-draft. This has happened repeatedly: three collisions in two days, with sessions losing the number they expected to a concurrent write.

**Write `{{ref}}` and it is substituted with the real ref on creation.** Never hand-write a ref into your own record's heading or prose, and never assume the next number is yours — there is no reservation, and the allocation happens at write time.

If you need a record's ref *before* you can write something else that references it — a tracker issue, a message to another agent — create the record first and read its assigned ref back. Do not guess and reconcile later.

## Stale references

A link to a `superseded` or `deprecated` record still resolves, so `unfunded=true` will not catch it — the record has a governing link, pointing at a dead bet. Use `dr_list(stale=true)`, which also flags refs in `body_md` prose. Prose refs are the harder half: a link can be re-pointed, but a paragraph reasoning about a bet that no longer exists just quietly misleads whoever reads it next.

## The cause distinction — the expensive one

When an IDR's success criteria are missed, two very different failures look identical at review time, and `dr_review` will not record a missed IDR without naming which:

- **`market`** — the intent was delivered as specified and the market rejected it. Seller accountability. The thesis about the audience was wrong. The next record is an MDR.
- **`delivery`** — the built thing did not match the experience the MDRs specified. Builder accountability, and **the bet is untested, not disproven.** Killing it here throws away a thesis nobody ever actually put in front of a person. The next record is an ADR.
- **`both`** — some of each; the bet stays untested until the delivery gap closes.

Charging a delivery failure to the Seller is the most expensive mistake this register exists to prevent.

## Which register, quickly

| The decision is about… | Register |
|---|---|
| Whether to fund this, how much, until when | IDR |
| Who it's for, how it's positioned, how it reaches them | MDR |
| What a user must be able to do, and whether they could | MDR |
| What we learned from watching real people use it | MDR |
| How it gets built, what technology, what data model | ADR |
| Operational posture, deployment, monitoring | ADR |
| Upkeep and chores | ADR, under the standing operations IDR |
| **Anything you are not authorized or not sure enough to decide** | **`ESC` — escalate, don't pick** |

## Commands

- `/triad-dr:bet` — write an IDR. The entry point; nothing else is legal without one.
- `/triad-dr:review` — judge a bet against what was actually written down.
- `/triad-dr:record` — capture an ADR or MDR from the session's work, under the bet that funds it.
- `/triad-dr:answer` — work the escalation queue: decisions agents are waiting on.
- `/triad-dr:apply` — drain published decision events and apply them to work items.
- `/triad-dr:status` — what's blocked, what's overdue, what's ungoverned, what the register's shape says.

## Wiring

The MCP server is per-repo, with the project namespace in the repo's `.mcp.json`:

```json
{
  "mcpServers": {
    "triad-dr": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/founding3", "triad-dr-mcp"],
      "env": { "DR_PROJECT": "<project-slug>", "AWS_PROFILE": "<sso-profile>" }
    }
  }
}
```

If a tool returns "No project set," the repo is not wired — say so and point at this block rather than guessing a project. The server never falls back to a default namespace and neither should you. If a tool reports expired credentials, the fix is `aws sso login --profile <profile>` in the operator's shell; you cannot run it for them, because it opens a browser.
