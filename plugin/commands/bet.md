---
description: Write an investment decision record (IDR) — a killable bet with an appetite, numbered criteria, and a date you have to honor. The entry point; no bet, no work.
---

# /triad-dr:bet

You are helping the operator put on the Investor hat, which is the one they least naturally wear. Your job is to make a vague intention into a bet that can lose.

**Do not create the record until every section below survives.** Draft, push back, then write. A bet you waved through is worse than no bet, because it will read as governance later.

---

## 0. Spike or commitment?

Ask before anything else, because it changes what every later section means.

- **Spike** — disposable by design. You are buying an answer to a question. Its kill criterion *is* its appetite: "four hours, then stop regardless." A spike that misses is informative, not a loss.
- **Commitment** — not meant to be thrown away. Its kill criteria are about outcomes, and a miss is a real loss that forces a persevere / supersede / kill decision.

**If the answer is "both," the record is wrong — split it.** An exploratory build and a committed market window under one kill criterion will always mishandle its own failure, because the two have opposite defaults: one is supposed to be discarded, the other isn't. The spike answers a question; its answer is the input to the commitment. Write the spike now and the commitment when the spike returns.

## 1. The bet, in one sentence

Ask for it. If it takes more than one sentence, it isn't a bet yet — it's a project. Split it or narrow it.

A bet names a thing you will do and an outcome you expect. "Improve onboarding" is not a bet. "Ship a three-step onboarding and get 40% of signups to first upload within a week" is.

## 2. Thesis

Why do you believe this? What would have to be true about the world? This is where the reasoning goes, so that a later review can find out whether the reasoning was wrong or just the execution.

## 3. Investment — an appetite, not an estimate

Time and money you are willing to spend, stated as a ceiling. The distinction matters: an estimate is a prediction that gets revised upward; an appetite is a limit that gets enforced.

Push for a number. "A few weeks" is not an appetite. "Twelve working days, and I stop at twelve whether or not it's done" is.

## 4. Success criteria — with numbers

What has to be observable for this to have worked. Every criterion needs a number and a way to observe it.

Interrogate each one:

- **Where does the number come from?** Name the source now, before the bet starts. A criterion measured by your own summary of your own work is not measurable. Analytics, revenue, a named person who replied — something outside the room.
- **Can it be measured with something that already exists?** This is the one that gets skipped. A gate requiring referrer-based attribution, when the event schema has no referrer field, is unmeasurable today and will be equally unmeasurable at review — it will just have consumed the bet first. Name the instrument for each criterion. If it doesn't exist yet, that's a legitimate answer, and building it is part of the investment in §3, not an afterthought.
- **Could this be satisfied without anyone outside actually caring?** Pageviews on a site you shared with yourself, a test-mode payment, your own account signing up. If the criterion admits those, tighten it.
- **Is it a proxy?** If so, say what it's a proxy for and whether you'd actually be happy hitting the proxy and missing the real thing.

## 5. Kill criteria — with numbers

What would make you stop. This is the section people skip, and skipping it is how a portfolio fills with bets that never resolve.

- Push for at least one criterion that fires on **time or cost**, not just on outcome. "If it takes more than 12 days" is a real kill criterion; "if users don't like it" is not.
- **Prefer criteria that fire without you looking.** A billing alarm, a scheduled report, a threshold something else already watches beats a condition you have to remember to check.
- If the operator genuinely cannot name a kill criterion, do not invent one for them. Write "none stated" and say plainly, once, that a bet with no kill criterion will not die on its own.

## 5a. Cause evidence — how you'll tell `market` from `delivery`

Ask directly: **at review, what observation will distinguish "they saw the intended thing and didn't want it" from "they never actually saw the intended thing"?**

This is the highest-leverage question in the whole command, and it has to be answered now rather than at review, because outcome data cannot answer it. A missed number only ever says the number was missed — which reads as the market declining, every time. **A bet that can't answer this can only ever be charged to the market**, and per the skill, charging a delivery failure to the Seller is the most expensive mistake this register exists to prevent. It discards a thesis nobody ever tested.

What counts: a funnel step proving the feature was actually reached, session recordings, an intercept asking non-converters what they expected, a working-vs-broken instrumentation check, a manual walkthrough of the shipped path against what the MDR specified.

If there is no answer, say so plainly — the bet is still writable, but it is now a bet whose failure will be uninterpretable, and that should be a conscious choice rather than a discovery made at review.

## 6. Review date — absolute, and startable

An ISO date. Then run two checks:

- **Is the clock startable?** A criterion like "if it stalls for 90 days post-launch" conditions on an event that hasn't happened, so the clock can never start and the bet can never come due. Reject conditional review dates. Pick a date on the calendar.
- **Is it soon enough to be informative?** If the review is further out than the appetite, the bet will consume its full budget before anyone asks whether it's working. Pull it in.

## 7. Delegation — default none

What, if anything, may agents decide on their own under this bet? The default is nothing: absent a grant here, every downstream record is human-owned.

If the operator wants to delegate, make it specific and bounded — what record types, what scope, what's excluded. "Agents may create and accept ADRs for reversible changes inside this repo; excludes schema, IAM, and anything with a bill." Grants expire when this bet does.

Ask, accept "none," and move on. Do not talk them into delegating.

---

## 8. Does this bet need attestations?

Only for the commitment-shaped ones — a launch, a public release, anything where the business goes all in. Those are Investor decisions that are only sound if the other two personas have said their part: the Builder that it's operationally ready, the Seller that acquisition and path-to-value hold.

If so, name the preconditions as links:

```
links: [{ref: "ADR-0012", relation: "requires-attestation"},
        {ref: "MDR-0005", relation: "requires-attestation"}]
```

Accepting the IDR will then warn for each attestation not itself accepted. It won't block — you may knowingly commit ahead of one — but "we went all in before the Builder said ready" gets written down as it happens rather than reconstructed later.

Skip this for a spike or a routine bet. Not every decision needs a gate, and a gate on everything is a gate on nothing.

## Before writing: scan for placeholders

Read back what you've drafted and check for `[X]`, `TBD`, `TODO`, `???`, or an unfilled bracket anywhere — especially in `investment`. An accepted bet committing `$[X]/month` has no appetite, and the appetite is the entire point of the record. Fill them or say out loud that you're leaving one open and why.

## Then write it

Call `dr_create` with `rec_type="IDR"`, `status="proposed"`, and `fields` containing `stage`, `bet`, `thesis`, `investment`, `success_criteria`, `kill_criteria`, `cause_evidence`, `review_date`, and `delegation` if any. Put the reasoning prose in `body_md`.

Report the `ref` and surface any warnings the tool returns verbatim.

**Then stop.** Creating is proposing. Accepting is a separate, deliberate act by the operator — say that the bet is `proposed` and ask whether they want to accept it now or sit with it. Do not call `dr_set_status` in the same breath as `dr_create` unless they explicitly ask you to; a bet that goes from written to accepted without a pause was never really weighed.
