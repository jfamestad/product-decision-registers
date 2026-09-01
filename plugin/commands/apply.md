---
description: Drain pending decision events and apply them to work items — label, comment, and flag work that just gained or lost its funding. Mechanical consequences only; anything consequential escalates.
---

# /triad-dr:apply

Decisions land in the register and then nothing happens until something applies them. This is that something: it reads what changed and carries it down to the work items.

**You apply mechanical, reversible consequences. You do not make decisions.** Labelling an issue and commenting on it is reversible and visible. Closing an issue, reassigning it, deleting a branch, or telling someone their work is cancelled is not — that escalates. The register's rule holds here exactly as everywhere else.

---

## 1. Get the pending events

**Preferred — the queue.** If `DR_EVENTS_QUEUE_URL` is set, receive from it. SQS buffers while nobody is listening, so a decision made overnight is still there in the morning. Delete each message only after it has been applied; a message that fails mid-apply must return to the queue rather than vanish.

**Fallback — the register.** With no queue configured, use `dr_list(since="<last applied timestamp>")` and treat `project_version` as the change detector. Say plainly that you are in fallback mode, because it has a real gap: it sees the current state of records, not the sequence of transitions, so a bet accepted and then superseded between two runs reads as one event instead of two.

If nothing is pending, say so in one line and stop.

## 2. Apply, by event type

**`record.status_changed`, IDR → `accepted`** — work is now funded.
Find work items labelled `bet::IDR-000N`. Comment that the governing bet is accepted, with the ref. If any were blocked pending funding, say which are now clear. This is the good case and needs no permission.

**`record.status_changed`, IDR → `deprecated` or `rejected`** — work just lost its funding. **This is the one that matters.**
Find every open work item labelled `bet::IDR-000N`. For each: add a label marking it unfunded, and comment naming the bet and when it died. Then **stop and report** — list them for the operator and ask what each should become. Do not close them, do not reassign them, do not tell anyone to stop. Some of that work deserves re-homing under a different bet, and that is a decision, not a consequence.

**`record.status_changed`, ADR or MDR → `accepted`** — a constraint is now binding.
Find items labelled `adr::ADR-000N` / `mdr::MDR-000N` and comment that the record they are built against is accepted. If an item is already in progress and the newly-accepted record contradicts what is being built, that is worth flagging loudly — it is the "built it wrong because the decision changed underneath" case, and catching it here is the whole point of routing decisions to work.

**`record.status_changed` → `superseded`** — the successor carries the work.
Comment on items labelled with the old ref, naming the successor. Do **not** relabel automatically: repointing work at a new decision is a judgment about whether the new decision actually covers it.

**`record.created`** — usually no action. Note it in the summary. A new record in `proposed` is not yet a thing to act on, and acting on proposals is how you end up building things nobody accepted.

**`review.recorded`** — surface it, especially a `missed` verdict. Report the verdict, the cause, and what the cause routes to: `market` means the next record is an MDR, `delivery` means the bet is untested and the next record is an ADR.

## 3. When you cannot find the work

An event referencing a bet with no labelled work items is not an error — the bet may fund work that has not been created yet, or work tracked somewhere you cannot see. Report it as unmatched rather than silently succeeding. A run that reports "12 events applied" while matching nothing is worse than one that admits it found nothing.

## 4. Escalate rather than guess

`dr_escalate` when:

- A killed bet's orphaned work looks like it should be stopped, but stopping it is not obviously right.
- A newly-accepted ADR contradicts work already in flight and something has to give.
- An event names a project you are not wired to.
- The mapping between a decision and a work item is ambiguous — two candidate items, or an item spanning two bets.

Per the escalation bar: one-way doors and material ambiguity go to the operator. Labelling and commenting are two-way doors and are yours.

## 5. Report

Structured, and honest about what did not happen:

```
7 events, 5 applied, 1 unmatched, 1 escalated

  IDR-0003 accepted    → 3 items now funded (#41, #44, #47)
  IDR-0008 deprecated  → 2 items UNFUNDED (#38, #52) — awaiting your call
  ADR-0022 accepted    → #44 flagged: conflicts with work in flight
  MDR-0005 created     → no action (proposed)
  IDR-0011 accepted    → unmatched, no items carry bet::IDR-0011
                       → ESC-0007 raised: #52 may need stopping
```

Lead with what needs the operator: unfunded work and conflicts. The applied ones are the boring part.

---

**Do not run this unprompted.** It is invoked by the operator or by a scheduled trigger, not by an agent deciding on its own that the queue looks long. Applying decisions changes other people's work items, and doing that on your own initiative is exactly the autonomy the register withholds.
