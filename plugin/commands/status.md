---
description: Read-only view of the register — bets past review, ungoverned work, the authority currently delegated, and what the shape of the register says. Writes nothing.
---

# /triad-dr:status

Answer "where does this project actually stand" in fifteen seconds. **This command writes nothing.** No records, no status changes, no reviews.

Report in this order — debts first, inventory last.

---

## 1. Blocked workers

`dr_escalations(status="open", blocking_only=true)`. Report these **first**, above everything else — an agent is stopped right now waiting on a decision, which outranks a review that is merely late.

Then open non-blocking escalations, as a count with the oldest named.

If there are any, offer `/triad-dr:answer`. One line.

## 2. Bets past review

`dr_due`. These are bets that were supposed to be judged and weren't. Oldest first, with days overdue.

If there are any, offer `/triad-dr:review` and stop selling. One line.

## 3. Ungoverned and stale work

`dr_list(unfunded=true)`. ADRs and MDRs with no `implements-bet` link — work with no bet behind it.

Distinguish two cases, because they need different responses:

- Records created before a governing bet existed, which just need linking.
- Work that genuinely has no bet, which needs one written or the work stopped.

Then `dr_list(stale=true)` — records whose links or prose point at superseded, deprecated, or missing refs. **Run this every time, not just when you suspect a problem.** An opt-in check does not get run, and this is the one that catches an agent briefing a worker from a topology that has since been replaced. Report the reason alongside each row; a stale flag with no pointer is not actionable.

## 4. Current authority

`dr_list(rec_type="IDR", status="accepted")`. This is the complete set of what's funded and, via each record's `delegation` field, everything agents are allowed to decide on their own.

Report it as an inventory: what's funded, what it costs, when each comes due. Call out any accepted bet whose review date has no date, or a conditional one — a bet whose clock can't start will never come due, and `dr_due` cannot see it.

**Then cross-check authority against the escalation queue, and say the result out loud.** The queue has two pathological shapes and they point in opposite directions. Report whichever you see.

**A queue that stays full** is not a queue problem, it's an authority problem: the operator is being asked to make calls they could have granted away once.

> *14 blocking escalations, and every accepted IDR grants `delegation: None`. The fix is widening a grant, not answering faster.*

Widening one grant retires a recurring class of interruption. Answering fourteen retires fourteen items and none of the cause.

**A queue that empties without producing records is worse, and it looks like health.** Check `dr_escalations(status="answered", unrecorded=true)` — answered escalations that produced no record and weren't marked as deliberately needing none.

> *26 escalations answered, 25 produced no record. Those decisions exist only inside the escalations. Nothing that reads the register will find them.*

This is the failure the register is least able to see on its own. An answered escalation **is a decision** — if it produced no ADR, IDR or MDR, then an agent reading the register later concludes no decision was made and proceeds on the old assumption. That has already happened once here: a correctly-made decision about deployment credentials was invisible to an agent reading the ADR set, which then proceeded on the wrong authentication model, because the ADR genuinely did not contain the answer.

The register enforces *no bet, no work*. This is the other half — *no record, no decision* — and it has to be checked rather than assumed.

## 5. Shape

`dr_projects` for counts, or count from the listings.

Report the **ADR : IDR : MDR** ratio and say what it means. This is a leading indicator and worth stating plainly rather than burying:

- **ADRs far outnumbering IDRs and MDRs** is the expected drift, because build decisions are cheap to make and satisfying to record while market decisions require contact with the world and a wait. A register that is 90% ADRs is a well-documented codebase, not a governed product. Past roughly 5:1 against the other two combined, say so directly.
- **No MDRs at all** means nothing in this project has been checked against a person outside the room.
- **Nothing ever reaching `superseded` or `deprecated`** means the register only grows. A portfolio where no bet has ever been killed does not have strong judgment; it has a missing sensor. Say it.
- **Escalation rate, read backwards.** A busy queue is the healthy early state — the bar is being held, and it tells you which delegation grants to widen. The alarming reading is **zero escalations across a stretch of autonomous work**: real work produces genuine ambiguity, so a silent queue means those calls went somewhere other than here. Say that plainly when you see it.

## 6. What's next

One line, not a menu. The single most useful next action given what you just read — usually the oldest overdue review, or a bet that needs writing before work continues.

---

If the register is empty, say that and point at `/triad-dr:bet`. If the tools report no project configured, the repo isn't wired — say so and show the `.mcp.json` block from the `decision-registers` skill. Don't guess a project namespace.
