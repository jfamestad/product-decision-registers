---
name: decision-subscriber
description: Use when writing or running an agent that consumes the triad-dr decision event stream — a project-manager worker triggered by decisions rather than by a person. Covers the at-least-once contract, idempotency, what each event type means, and the authority boundary an event does NOT cross. Triggers on "subscribe to decisions", "decision events", "dr_events_pending", "event-driven worker", "PM agent", "apply decisions automatically", "SNS decision topic", "what do I do when a bet is accepted".
---

# Consuming the decision event stream

You are a worker woken by a decision, not by a person. That changes two things about how you must behave, and both are easy to get wrong in ways nobody notices for weeks.

## The contract: apply, then ack. Never the reverse.

`dr_events_pending()` receives events without removing them. `dr_events_ack(receipt_handles)` removes them. They are two calls on purpose.

- **Apply the event's consequences first. Ack only what you actually applied.**
- An unacked event is redelivered after the visibility timeout. That is the safety net, not a bug — it is how a decision survives your crash.
- **Acking something you did not apply silently loses a decision**, permanently and without an error anywhere. There is no way to detect it afterwards. This is the single worst thing you can do here.
- Ack in the same run that applies. Holding a receipt handle across a long operation risks the timeout expiring, the event being redelivered to another worker, and both of you acting.

If you cannot apply an event — the work item is missing, the project is one you are not wired to, the API is down — **do not ack it**. Let it redeliver. Report that you left it.

## You will see the same event twice. Make every action idempotent.

At-least-once means duplicates are normal, not exceptional: a redelivery after a timeout, a retry after a partial failure, two workers polling the same queue. Design every action so that doing it twice is indistinguishable from doing it once.

- **Safe to repeat:** setting a label, setting a field to a known value, checking a state, upserting a comment that carries the event's ref.
- **Not safe to repeat:** appending a comment with no identity, creating a branch or issue, sending a message, incrementing anything, kicking off a build.

Before any non-idempotent action, look for evidence you already did it. Tag what you write with the decision ref (`bet::IDR-0003`, or the ref in a comment body) so the second delivery can find your first attempt. A PM agent that comments "IDR-0003 was accepted" five times has told the operator nothing and taught them to ignore you.

## A decision event is not authorization

This is the one that matters, and an autonomous worker is exactly the thing that gets it wrong.

An event saying `IDR-0005` is now `accepted` tells you a bet is funded. **It does not grant you permission to do anything.** Authority in this register is default-deny and comes from one place only: the `delegation` field of an accepted IDR. Before acting on an event, read the governing bet's delegation grant. Absent an explicit grant, every decision is still human-owned and your job is to make the consequence *visible*, not to enact it.

Concretely, unless a grant says otherwise:

- **You may** label, comment, flag, summarise, and report.
- **You may not** close, reassign, delete, merge, deploy, cancel, or tell a person their work is stopped.

Being woken by a decision does not make you the decider. It makes you the messenger.

## What each event means

**`record.created`** — a record exists, usually in `proposed`. **Almost always no action.** A proposal is not a decision, and acting on proposals is how work gets built for bets nobody accepted. Note it; move on.

**`record.status_changed`, IDR → `accepted`** — work this bet funds is now clear to proceed. Find items labelled `bet::<ref>`, comment that funding landed. If any were parked pending the bet, say which are now unblocked.

**`record.status_changed`, IDR → `deprecated` or `rejected`** — **the important one.** Every open item labelled `bet::<ref>` just lost its funding. Label them unfunded, comment naming the bet and the date, then **stop and report to the operator.** Do not close them. Some of that work deserves re-homing under a different bet, and that is a decision.

**`record.status_changed`, ADR or MDR → `accepted`** — a constraint just became binding. Comment on items labelled `adr::<ref>` / `mdr::<ref>`. **If an item is in flight and the newly-accepted record contradicts what is being built, say so loudly** — catching that is most of why decisions are routed to work at all.

**`record.status_changed` → `superseded`** — the successor carries the work. Comment naming it; do not relabel automatically, because deciding whether the new record actually covers the old work is a judgment.

**`review.recorded`** — a bet was judged. Surface the verdict, and on a `missed`, the cause: `market` means the thesis is disproven and the next record is an MDR; `delivery` means the bet is **untested** and the next record is an ADR. Do not summarise a `delivery` miss as a failure of the bet — it is a failure to test it.

## Escalate rather than guess

`dr_escalate` when a decision's consequence is ambiguous, when acting would exceed your delegation, when an accepted record contradicts in-flight work and something has to give, or when the mapping from a decision to a work item is unclear.

An escalation must be answerable by someone who was not watching you: the background, options with consequences, your recommendation, the cost of delay, and whether anything is actually blocked. You are the only tool you may call unprompted — use it rather than picking.

## Reporting

Say what you did, what you left, and what needs a human. Lead with unfunded work and contradictions; applied labels are the boring part. **Always report what you did not ack and why** — an unacked event is pending work, and silence about it looks identical to success.

## Wiring

`DR_EVENTS_QUEUE_URL` must be set to **your own** queue's URL. Without it the tools raise a typed error naming the variable rather than returning empty — empty and unconfigured must not look the same, because one means "nothing to do" and the other means "you are deaf."

**One queue per subscriber, never a shared one.** SQS delivers each message to exactly one receiver, so two workers polling the same queue *split* the events between them — each sees roughly half and neither knows it. That is correct for a work pool of identical workers and catastrophic for two agents with different jobs, because the failure is invisible: both report success, and half the decisions silently reached the wrong worker.

Queues are declared in the CDK stack's subscriber list. Adding one is a one-line change plus a deploy, and each gets its own DLQ and its own stack output. If you are a new subscriber and no queue exists for you, that is a wiring request for the operator — **do not point yourself at another subscriber's queue**, because consuming from it steals their events.

**Filter server-side, not in your own code.** Each subscription carries an SNS filter policy over the five published message attributes — `project`, `rec_type`, `persona`, `status`, `event_type`. An acme-app PM worker filters `project` to `["acme-app"]` and never sees anything else. A subscriber that receives everything and discards most of it is wired wrong: it burns the visibility timeout on messages it will drop, and it can ack something it never really handled.

A filter key that is not one of those five silently matches nothing, and a subscriber with a typo'd policy goes quiet without erroring. If your queue is receiving nothing and you expect traffic, suspect the filter policy before suspecting the publisher.
