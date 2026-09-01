---
description: Work the escalation queue — decisions automated workers are waiting on. Blocking items first. Answering may produce a record, or may just be "don't do that".
---

# /triad-dr:answer

Agents escalate what they are not authorized or not sure enough to decide. This is where the operator clears that queue. Someone may be stopped right now.

---

## 1. Pull the queue

`dr_escalations(status="open")`. Escalations are project-scoped — this is the queue for the repo you are in, and there is no cross-project view by design.

Order: **blocking first, then newest** — same recency direction as every other listing. A stopped worker outranks a flag-for-later regardless of age.

Age is not in the sort, so read it yourself: an escalation that has been `blocking` for days is a worse problem than a fresh one, and worth calling out even though it sits lower in the list.

If the queue is empty, say so in one line and stop. Do not go looking in other projects; if the operator asks what's blocked elsewhere, the honest answer is that it's visible from that repo or from the scheduled sweep, not from here.

## 1a. Check the queue is real before spending attention on it

An escalation queue rots. Items get resolved by work done elsewhere, the same decision gets raised twice from different tasks, and nobody tells the queue. In observed use roughly a fifth of a 16-item queue was already fiction. Sitting down to answer that is worse than not sitting down.

Before presenting anything:

- **Age.** Anything more than a couple of days old gets its preconditions re-checked, not just re-read. Say plainly which items are old.
- **Already resolved elsewhere.** For each item, check whether the decision has since been made — a record created, a status changed, work landed. **Withdraw it** with `dr_withdraw(ref, reason="resolved by ADR-0031")` rather than asking the operator to decide something already decided.
- **Duplicates.** Two escalations describing the same underlying decision get collapsed: answer one, and `dr_withdraw` the other with `duplicate_of=` pointing at it. Look at what the decision *is*, not how it was worded — the same call raised from two different tasks reads differently and is one decision.
- **Bundles.** An item carrying more than one decision — a mechanical step plus a judgment call, or a completed action plus an outstanding one — should be split before answering. Answer the part that is real and say what remains.

**Withdraw the moot ones; never answer them to clear the queue.** `dr_answer` records that a decision was made. Using it on something that was never a decision creates a resolved escalation with no record behind it, which is indistinguishable afterwards from a real decision that leaked — and that is precisely what made a production queue read as 25 of 26 answers producing nothing. Answer means decided. Withdraw means it turned out not to need deciding.

Report the cleaned count against the raw one. "16 open, 3 already resolved, 2 duplicates, so 11 real" is a materially different sitting than "16 open."

## 2. Present one at a time

Do not dump the queue. Each item was written to be decided in isolation, so present it that way, and present it as it was written for someone with no session context:

- the question, in one sentence
- the background needed to answer it — the agent supplied this; use it, don't paraphrase it away
- each option with its consequence, and whether it's reversible
- the agent's recommendation, and its reasoning
- the cost of delay
- the governing bet, if there is one

**Do not editorialize the recommendation before the operator has read it.** The agent did the work and stated a view; let that stand on its own. If you think the recommendation is wrong, say so *after* presenting it, as your own separate opinion, clearly labelled.

## 3. Read the item critically before presenting it

An escalation that fails its own bar wastes the operator's attention, and the fix is upstream. Flag it when:

- **It's unanswerable without session context.** The test is whether an un-initiated leader could decide from what's written. If not, say so and offer to go get what's missing rather than making the operator reconstruct it.
- **It's not actually a one-way door.** If every option is cheap and reversible and the stakes are low, this is something the agent should have decided and recorded. Answer it, then note that the delegation grant may be too narrow — that's the useful signal, and widening the grant fixes the class rather than the instance.
- **The options are not real.** Three options where two are obviously bad is a decision wearing a costume.

## 4. Record the answer

`dr_answer(ref, resolution, produces?)`.

`resolution` is the operator's decision **in their own words**. Do not smooth it into corporate prose — "no, that's a rabbit hole, ship the simple one" is a better record than a paragraph of reasoning they didn't say.

**Ask about `produces` every single time. Do not let it default silently.**

An answered escalation *is* a decision. If it produces no record, that decision exists only inside the escalation — and nothing that reads the register will ever find it. An agent briefing itself from the ADR set will conclude no decision was made and proceed on the old assumption. That has already happened: a correctly-made decision about deployment credentials was invisible to an agent reading the ADRs, which then proceeded on the wrong authentication model, because the ADR genuinely did not contain the answer.

The register enforces *no bet, no work*. This is the other half: **no record, no decision.**

- **Produces a record** when the answer establishes anything future work depends on — a technical direction (ADR), a market or user call (MDR), a change to what's funded (IDR). Draft it from the escalation's own context and the operator's own words, and link it. This is the common case, not the exception.
- **Produces nothing** when the answer is "don't do that," "not now," or "your recommendation is fine." Genuinely complete answers exist — but pass `no_record=True` so it's recorded as a deliberate call. Otherwise "decided no record was needed" and "forgot to write one" are indistinguishable afterwards, and only one of them is fine.

The test: *if someone reads the register in a month without this conversation, will they find this decision?* If not, it needs a record.

Volume is still how a register becomes worthless — but 25 answers producing 0 records is not restraint, it's leakage.

## 5. When the answer changes what's authorized

If the operator finds themselves answering the same class of question repeatedly, the real fix is the delegation grant in the governing IDR, not another answer. Say so, and offer `/triad-dr:bet` to supersede it with a wider grant. One IDR revision retires a recurring interruption.

**Say this before working the queue, not after, when the queue is long.** A dozen blocking escalations against accepted bets that all grant `delegation: None` is a diagnosis, not a workload — the grants are too narrow and the operator has become the bottleneck by construction. Answering them all restores throughput until tomorrow; widening one grant fixes the class. Lead with that rather than burying it under eleven answered items.

---

Answering is not the same as unblocking. If the escalation was `blocking`, say plainly which worker or task is now free to continue, so the operator knows the answer actually landed somewhere.

## Finally: sweep what leaked

Before finishing, run `dr_escalations(status="answered", unrecorded=true)`. That is every escalation answered without producing a record and without being marked as deliberately needing none — decisions that currently exist only inside an escalation, where nothing reading the register will find them.

For each, ask the operator: does this need a record written now, or was none genuinely needed? Write the ones that do, and mark the rest `no_record` so they stop surfacing. A backlog here is not tidiness — it is the register's own account of decisions it has lost track of.
