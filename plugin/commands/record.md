---
description: Capture a decision from this session as an ADR or MDR, under the bet that funds it. Drafts from what actually happened, not from imagination.
---

# /triad-dr:record

The operator has decided something in this session and wants it durable. Your job is to file it in the right register, under the right bet, using what actually happened here.

---

## 1. Which register

Ask yourself first; confirm with the operator if it's genuinely ambiguous.

- **How it gets built** — architecture, technology, data model, operational posture → **ADR**
- **Who it's for, how it's positioned, how it reaches them** → **MDR**
- **What a person must be able to do, whether they could, what we saw when they tried** → **MDR**
- **Whether to fund something, how much, until when** → neither. That's `/triad-dr:bet`.

The common error is filing a user-research finding as an ADR. It isn't one. A finding about what users need is a Seller finding even when a Builder discovered it, and misfiling it hands the Builder accountability for a market question.

## 2. Find the governing bet

Call `dr_list(rec_type="IDR", status="accepted")`. Identify which bet funds this work and confirm it with the operator.

**If no accepted IDR covers it, stop and say so.** No bet, no work — the absence is the finding. Offer `/triad-dr:bet` to write one, or ask whether this belongs under the standing operations IDR if it's upkeep. Do not file the record anyway with a link you invented; an unfunded record is a real signal and `dr_list(unfunded=true)` exists to surface it.

**If the operator isn't present to answer that, `dr_escalate` — do not pick a bet.** Guessing which bet funds a piece of work is exactly the kind of important decision agents don't make, and a wrong `implements-bet` link is worse than a missing one, because `dr_list(unfunded=true)` will never surface it again.

If a bet exists but is still `proposed`, say so — the record can be created (forward references are legal), but the work it authorizes isn't funded yet.

## 3. Draft from the session, not from imagination

This is the part that makes the record worth reading later. Pull from what actually happened in this conversation: the alternatives that were genuinely weighed, the constraint that actually decided it, the thing that was tried and abandoned.

Do not generate plausible-sounding content to fill a template. An `alternatives_considered` listing options nobody considered is worse than an empty one — it manufactures a deliberation that didn't happen, and a later reader will trust it.

If a field has no honest content, leave it thin and say which.

**ADR fields:** `context`, `decision`, `consequences`, `alternatives_considered`

**MDR fields:** `audience` (name buyer and user separately where they differ), `jobs_to_be_done`, `value_delivered`, `time_to_value`, `positioning`, `channel_motion`, `evidence_plan` (how and when evidence gets collected — method, sample, timing), `user_evidence` (what was actually observed, with source and sample), `experiment`, `success_metric`

**Ask what the customer gets, and when.** `value_delivered` is the benefit a real person receives *in this phase* — not the eventual vision. `time_to_value` is how long they wait between their first commitment (a signup, a payment, an hour of attention) and receiving it.

Ask both explicitly; they will not surface on their own. **A phase that asks for commitment while deferring all value is a common and dangerous shape** — "sign up now, the product arrives later" — and it stays invisible unless someone asks. "No benefit this phase, they wait until Phase 1" is a legitimate answer and belongs in writing, where the governing bet's review can weigh it, rather than being discovered when the list stops growing.

For an MDR with no evidence yet: that's normal, hypotheses start there. But fill `evidence_plan`, because the alternative is finding out at the governing bet's review date, by which point the build is already paid for.

## 4. Write it

Call `dr_create` with the type, a title in the imperative ("Use DynamoDB single-table design", not "Database decision"), the fields, `body_md` for the reasoning prose, and a link `{ref: "IDR-000N", relation: "implements-bet"}`.

Default `status="proposed"`. Accepting is the operator's act unless the governing IDR's `delegation` field explicitly grants it — check before assuming, and quote the grant if you rely on it.

Report the ref and surface every warning the tool returns, in plain language. The warnings are the product; do not swallow them.
