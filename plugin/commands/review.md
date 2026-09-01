---
description: Judge a bet at its review date against what was actually written down. Records the verdict and its cause; deciding what to do about it stays a separate act.
---

# /triad-dr:review

A bet with success criteria and no recorded review is an opinion with a date on it. This command closes that.

---

## 1. Find what's due

Call `dr_due`. If the operator named a specific ref, use that instead.

If nothing is due, say so in one line and stop. Do not go looking for something to review.

## 2. Restate the criteria before discussing them

For each bet, read back **verbatim** from the record, before any conversation about how it went:

- the bet, in the one sentence it was written in
- the success criteria, as written
- the kill criteria, as written
- what was committed as investment, and what the review date was

This ordering is deliberate and you must not collapse it. The failure mode is judging the bet against what you now wish you had written — criteria drift to meet the outcome, and every bet passes. Put the original text on the screen first.

## 3. Establish what actually happened

Ask what was observed. Then interrogate the evidence:

- **What is the source?** A number from analytics, a payment, a message from a named person. Your own summary of your own work is not evidence, and neither is an agent's report on the code an agent wrote.
- **What is the sample?** "Users found it confusing" is not a finding. "Six of eight in the August round failed to find export" is.
- **Was it measured, or is it an impression?** Both are admissible; label which.

If the honest answer is that nobody looked, that is a `too-early` verdict with the evidence "not measured" — record it as such rather than guessing. An unmeasured bet that gets a verdict anyway is the failure this whole apparatus exists to prevent.

## 4. Verdict

One of `met`, `missed`, `mixed`, `too-early`.

## 4a. Read the bet's `stage` before judging the verdict

A **spike** and a **commitment** fail differently, and treating them alike is how a useful negative result gets recorded as a loss.

- **Spike** — disposable by design. Missing is *informative*: you bought an answer and got one. The default next action is close-and-learn, not persevere-or-kill, and the answer usually feeds a commitment-stage bet. Do not let a spike that returned "no" be recorded as a failure.
- **Commitment** — a miss is a real loss and forces a deliberate persevere / supersede / kill call in step 7.

If the record has no `stage`, say so and infer from the shape — appetite-as-kill-criterion means spike.

## 5. If missed on an IDR — name the cause

The tool will not record a missed IDR without this, and the distinction is the most consequential judgment in the review:

- **`market`** — it was built as specified, put in front of people, and they didn't want it. The thesis about the audience was wrong. Seller accountability. The next record is an MDR.
- **`delivery`** — the built thing didn't match what the MDRs specified. Builder accountability, and **the bet is untested, not disproven.** Say this out loud: killing the bet here discards a thesis nobody ever actually tested. The next record is an ADR, and the bet likely deserves to run again once the gap closes.
- **`both`** — some of each; treat the thesis as untested until delivery is fixed.

Ask directly: *was the thing we shipped actually the thing we designed?* If it wasn't, this is `delivery` no matter how disappointing the numbers were.

**Go get the bet's `cause_evidence` field and use it.** It names the observation that was supposed to settle this exact question, chosen back when nobody knew how it would turn out. Read it, then go and look at what it points to. Judging cause from outcome numbers alone will land on `market` essentially every time — that's the bias the field exists to counteract.

If `cause_evidence` is empty, say so plainly: **this bet cannot distinguish its own failure modes**, so any cause you assign is a guess. Record the honest verdict, note the gap, and make the successor bet answer the question before it starts. That admission is worth more than a confident `market` that nobody can check.

## 6. Record it

Call `dr_review` with the ref, verdict, evidence prose (source and sample included), and cause where required.

**This does not change the record's status, and that is intentional.**

## 7. Then, separately — what do you want to do?

Only after the review is recorded, ask. The options, plainly:

- **Persevere.** The bet stands, unchanged. Legitimate, and needs a new review date — otherwise it has quietly become permanent.
- **Supersede.** A revised bet replaces this one. `dr_supersede`. Note whether the revision *narrows* the commitment or *raises* it; raising it after a miss is doubling down and deserves to be said out loud before it's done.
- **Kill.** `dr_set_status` to `deprecated`. The tool will return the ADRs and MDRs this bet funded — they are now orphaned, and re-homing or retiring them is a follow-up decision, not something to do silently.
- **Nothing yet.** Also fine. The review is recorded either way.

Do not pick for them, and do not let a `missed` verdict slide into a status change automatically. Recording what happened and deciding what to do are two acts, and the register keeps them apart on purpose.
