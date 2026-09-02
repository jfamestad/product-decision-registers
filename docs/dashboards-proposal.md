# Role-based dashboards — proposal

**Status: proposal, unfunded.** Nothing here is built. Building it needs an IDR first.

## The problem

The register holds the right facts and makes them hard to see. `/triad-dr:status` is one linear read
for one moment; it answers "what's on fire" and nothing else. There is no view that answers *"as the
Investor, is this portfolio worth what it's costing?"* or *"as the Seller, what do we actually know
about anyone?"* — and those are the questions the three-persona model exists to keep separate.

A newcomer to this project reads the README and understands the model in theory. They cannot see it.
Role dashboards are the model made observable: the same records, cut three ways, each cut answering the
one question that persona is accountable for.

## Design rules (all four views)

1. **One question per dashboard**, stated at the top. If a panel doesn't help answer it, it belongs on
   another dashboard.
2. **Debts before inventory.** Same ordering as `/triad-dr:status`. What's overdue, unfunded, or
   uncollected comes before what exists.
3. **Every number carries its reading.** `ADR:IDR:MDR = 31:4:1` is trivia. "31:4:1 — a well-documented
   codebase, not a governed product" is a dashboard. The interpretations in `status.md` §5 are the
   content; the counts are just how they get computed.
4. **Every panel has an action.** A row you can't do anything about is a row that trains you to skim.
5. **Fifteen seconds to the headline**, drill-down underneath.

---

## 1. Investor — "Is this portfolio worth what it's costing, and what's due?"

| Panel | Source | Why it earns the space |
|---|---|---|
| **Bets due / overdue** | `dr_due` | The Investor's one non-delegable act is judging at the review date. Overdue count is the headline. |
| **Funded inventory** | `dr_list(rec_type="IDR", status="accepted")` | What's committed, stage (spike vs commitment), appetite, review date. Flag any accepted bet whose review date is absent or conditional — a clock that can't start never comes due and `dr_due` can't see it. |
| **What each bet is carrying** | count of `implements-bet` links per IDR | A bet with 20 ADRs and 0 MDRs is a build project wearing a bet's clothes. A bet with neither is funded and untouched. |
| **Delegation map** | `delegation` per accepted IDR, joined to escalations charged against it | The Investor's actual control surface. Read backwards: a full queue against `delegation: None` is an authority problem, not a queue problem. |
| **Verdict history + cause mix** | `reviews` across all IDRs | The highest-value Investor signal in the system. Repeated `delivery` causes mean the Builder is the constraint; repeated `market` means the theses are wrong. Nothing else distinguishes those. |
| **Cause-evidence debt** | accepted IDRs missing `cause_evidence` | Pre-review debt. A bet without it can only ever be charged to the market — the exact mistake the register exists to prevent. |
| **Kill rate** | status transitions to `rejected` / `deprecated` | Zero kills ever is a missing sensor, not good judgment. State it as a number. |

**Open question this raises:** the Investor persona genuinely holds a *portfolio*, but escalations and
listings are project-scoped by design. `dr_projects` gives counts across projects and nothing else. A
cross-project Investor roll-up either contradicts a stated design decision or needs a deliberate
exception. Worth an explicit call before building this one.

## 2. Seller — "Does anyone want this, and what do we actually know about them?"

| Panel | Source | Why it earns the space |
|---|---|---|
| **Days since outside contact** | most recent MDR carrying `user_evidence` | One number, deliberately embarrassing when it's large. "41 days since anything in this register came from outside the room." |
| **Evidence ledger** | `evidence_plan` vs `user_evidence` per MDR | Planned-but-never-collected is the Seller's debt column and currently invisible. |
| **Value and wait** | `value_delivered` / `time_to_value` per MDR, longest wait first | Surfaces the dangerous shape: a phase asking for commitment while deferring all value. The fields exist precisely so this can be seen; nothing reads them today. |
| **Audience map** | `audience` / `positioning` across accepted MDRs | Who we've committed to serving, in one place. Two MDRs quietly serving different audiences is a finding. |
| **Positioning drift** | supersession chains over MDRs | Whether the story is converging or thrashing. Reading the chain is the fastest available proxy for market learning. |
| **Attestations owed** | `requires-attestation` links pointing at MDRs | What the Seller owes a pending commitment gate, and whether it's accepted yet. |

## 3. Builder — "What are we committed to, and what's rotting?"

| Panel | Source | Why it earns the space |
|---|---|---|
| **Stale references** | `dr_list(stale=true)` with reasons | The one that prevents briefing a session off a topology that's been replaced. Prose refs called out separately — a link can be re-pointed, a paragraph just misleads. |
| **Current architecture** | accepted ADRs, grouped by area | This is the briefing an agent should read before touching anything. Grouped, it's a document; ungrouped, it's a list. |
| **Ungoverned build work** | `dr_list(unfunded=true)` filtered to ADR | Split the two cases: created-before-a-bet-existed (needs linking) vs genuinely unfunded (needs a bet or a stop). |
| **Supersession chains** | `supersedes` / `superseded_by` | What replaced what, and what's still live. |
| **Consequences ledger** | `consequences` fields, aggregated | The accumulated cost of past decisions. Written once, never re-read. Cheap to surface, disproportionately useful. |
| **Ops bet health** | the standing operations IDR | Chores are only legal under it. If it lapsed, every chore is ungoverned at once. |
| **Attestations owed** | `requires-attestation` links pointing at ADRs | Operational readiness the Builder owes a commitment gate. |

## 4. Alignment — the founding-three view

Not a fourth persona; it's the README made live, and the one that makes *this project* legible to
someone who just arrived.

- **The three milestones** — commit a bet / build a launchable product / onboard first customers — each
  with its state per persona. Where the project actually is, in the project's own vocabulary.
- **The three overlaps as live queues.** Decisions currently in flight that need two personas in the
  room: Builder ∩ Seller (feature priorities), Seller ∩ Investor (demand signals), Investor ∩ Builder
  (costs). Derivable from a decision's links and type; a record touching both sides sits in an overlap.
- **Register shape with its reading** — the ADR:IDR:MDR ratio, kill rate, escalation rate, each with the
  interpretation attached per `status.md` §5. Escalation rate read backwards: a busy queue is healthy
  early; a silent one across autonomous work means those calls went somewhere other than here.
- **Leakage** — `dr_escalations(status="answered", unrecorded=true)`. Decisions the register itself has
  lost track of. This is the failure it's least able to see, so it belongs on the view everyone looks at.

---

## Delivery — three options, one decision

**A. Terminal commands** (`/triad-dr:investor`, `:seller`, `:builder`, `:alignment`).
Cheapest by a wide margin — markdown command files, no code, no infra, ships today. Matches the existing
UX exactly. But nothing persists, nothing is shareable, and each read spends context.

**B. Rendered HTML, generated on demand** — `triad-dr dashboard --role investor` writes a self-contained
file. Shareable, glanceable, survives the session, right cadence for the Investor and Seller views
(weekly, not per-session). Needs a view-model layer plus a renderer; `dr_export` won't carry it (it
writes one markdown file per record). Stale the moment it's written.

**C. Live web app** on the existing stack (S3 + CloudFront + API Gateway + Lambda over the same table).
Always current, the natural home for the alignment view, the only option that makes the project legible
to someone who isn't running a Claude session. Also the only one that's a project rather than a feature.

**Recommendation: A for the Builder view, B for Investor / Seller / Alignment.** The Builder view is
consumed mid-session by agents that already live in the terminal; making it a file is a step backwards.
The other three are periodic and worth looking at away from the work. Build one view-model layer and two
renderers over it — terse to terminal, rich to HTML — so the reading rules live in one place and C stays
available later without a rewrite.

## Data gaps worth naming before committing

- **No spend tracking against appetite.** `investment` is a stated figure; nothing records consumption,
  so "appetite burn" can't be computed. Time-boxed appetites can be derived from elapsed days against
  the review date; money can't. Either accept a time-only burn bar or add a field.
- **Cross-project roll-up conflicts with project-scoping** (see Investor, above).
- **Overlap membership is inferred, not recorded.** The Venn overlaps are derivable from links and type,
  but nothing marks a decision as *needing two personas in the room*. Inference is probably good enough
  for v1; worth knowing it's inference.

## Next step

This is unfunded work — no bet, no work applies to the dashboards as much as to anything else. The next
action is an IDR with an appetite and a kill criterion, scoped to whichever slice survives the delivery
decision above.
