# Spec: Decision Registers MCP Server ("triad-dr")

**Goal:** A local MCP server (stdio) that gives Claude Code and other MCP clients a durable, namespaced register of decision records across three types — **ADR** (Architecture, the Builder), **IDR** (Investment, the Investor), **MDR** (Marketing, the Seller) — backed by a single DynamoDB table. One developer, many projects; the active project is selected by environment variable so tool calls stay short.

The three personas are the product, not decoration. §1 defines them and is the section to read first: it is what makes a record land in the right register, and what a calling model needs to use the tool correctly.

---

## 1. The three personas (read this first)

Every record belongs to exactly one persona. The personas are not job titles — one operator holds all three — they are three distinct kinds of accountability, and the register exists to keep them from blurring.

### Investor (IDR) — allocates

Decides what gets funded, for how long, and on what terms. Owns the bet, the resources committed, the success criteria, the kill criteria, and the review date.

The Investor does not decide *how*. The Investor decides *whether, how much, and until when*. Every ADR and MDR traces back to a governing IDR — the register is a hierarchy with the bet on top, not three co-equal circles.

### Seller (MDR) — knows the market, and owns the user

The register's only channel to the outside world and its oracle for what the market wants. Owns audience and segmentation, positioning, channel motion, pricing narrative, launch experiments — **and the user experience: what a real person must be able to do, and whether they could.**

Voice of customer is a Seller function. User research, usability evidence, jobs-to-be-done, win/loss, support signal, activation and churn reasons are all Seller instruments, and their findings belong in MDRs. **A "UDR" is not a separate record type — it is an MDR.** The Seller writes it, the Seller is accountable for it.

There is deliberately no fourth "user" or "UX" persona. The user is not a constituency to be balanced against the market; the user **is** the market, observed up close. Splitting them produces the familiar pathology where the research deck and the positioning deck describe two different customers and nobody reconciles them. One persona owns both, so there is one story and one accountable party.

### Builder (ADR) — delivers

Owns architecture, technology selection, data model, operational posture, and the technical shape of the experience.

**The Builder delivers the user experience the Seller defines.** The Seller states what the person must be able to do and how it must feel; the Builder decides what makes that true in software and is accountable for whether the built thing matches the stated intent. Where the Builder disagrees that the intent is buildable within the bet, that is an ADR with `alternatives_considered` — not a silent substitution.

When the experience needs resource the Builder doesn't have — design work, a research round, recruiting participants — that is an Investor decision: a new IDR, or an amendment to the governing one.

### How the loop closes

1. **Investor** writes an IDR: the bet, what it costs, what success looks like, what kills it, when it gets reviewed.
2. **Seller** writes MDRs: who it's for, what they must be able to do, how it will be positioned and taken to market, what evidence will be gathered and how.
3. **Builder** writes ADRs: how it gets built, each linked to the governing IDR.
4. At `review_date` the Investor tests the IDR's success criteria against the Seller's evidence and records the result with `dr_review` — met, missed, mixed, or too early — with the evidence attached.
5. **A missed criterion is named before it is assigned.** `dr_review` requires a `cause` on a missed IDR, because two very different failures look identical at review time:
   - **`market`** — the intent was delivered as specified and the market rejected it. Seller accountability: the thesis about the audience was wrong, and the Seller owns bringing voice of customer back until the product meets the market. "We shipped five variants and nobody found the export button" is this — a missed criterion, not a missing register. The resulting change is a new MDR, and whatever it costs is a new or superseding IDR.
   - **`delivery`** — the built thing did not match the experience the MDRs specified. Builder accountability, and the bet is **untested, not disproven**. Killing it here throws away a thesis nobody ever actually put in front of a person. The resulting change is an ADR.
   - **`both`** — some of each. The bet stays untested until the delivery gap closes.

   Charging a delivery failure to the Seller is the most expensive mistake this register exists to prevent, which is why the tool will not record a missed IDR without the distinction.

This is why the evidence must be written down at the same grain as the decision. A bet with success criteria and no recorded review is an opinion with a date on it.

### Who writes

**No bet, no work.** Every ADR and MDR traces to a governing IDR. Work with no bet behind it is ungoverned and the register says so.

Maintenance is not an exemption from this — it is a bet. Each project carries a **standing operations IDR** covering upkeep: dependency bumps, build health, infrastructure, the chores. It is written like any other bet, with an appetite and a kill criterion ("2 hrs/month; if it exceeds 6, stop patching and pin"), and it comes up for review like any other. Chores are governed by it and link to it. This keeps the maintenance tax visible and periodically re-examined rather than invisible and unbounded.

**Agents do not write the register on their own initiative.** A record is created when the operator asks for it, as the product of a working session — the agent drafts what was decided together, the operator owns it. There is no autonomous record creation, no background filing, no agent deciding on its own that something merits an ADR. The register is a human artifact that an agent helps write, which is what keeps its volume bounded and its contents worth reading.

**Delegation is granted in IDRs, and only in IDRs.** Authority is default-deny: absent an explicit grant, every record is human-owned. An IDR may delegate specific authority to agents for the work it funds (`fields.delegation`), scoped and bounded. Grants expire with the bet — supersede or deprecate the IDR and the delegation dies with it. The complete current authority set for a project is `dr_list(rec_type="IDR", status="accepted")`, readable by the operator and by the agent.

### Carrying "no bet, no work" down to the work items

The rule stops at the decision layer unless something carries it further. A register can be immaculate while the backlog fills with tasks no bet funds — the same failure, one layer down, where it is easier to hide because a tracker full of tickets looks like progress.

The register deliberately does **not** learn about your tracker. No sync, no mirroring, no issue IDs stored in DynamoDB: two systems of record drifting apart is worse than one system with a convention. Instead the tracker references the register by ref, and you query by label.

The documented convention:

- **`bet::IDR-0003` is required on every work item.** Where the tracker supports required fields on creation — GitHub issue forms, Jira required fields — put it there. Enforcing at authoring time costs nothing and prevents the bad state rather than reporting it.
- **`adr::ADR-0007` and `mdr::MDR-0005` as warranted**, naming the records that constrain how the item is built or what experience it must deliver. This is what "build it right the first time" looks like mechanically: the constraints travel with the ticket.
- **Query by label, never sync.** `bet::IDR-0003` answers "what work does this bet fund." When a bet is killed, the same query is exactly its orphan list, and the kill cascade reaches the tracker with no new machinery.
- **Scope the prefix.** `bet::` rather than `bet:` avoids collision with a tracker's own namespacing conventions.

Keeping the reference one-directional means there is nothing to reconcile: the register is authoritative for decisions, the tracker is authoritative for work, and a label is a pointer rather than a copy.

### The escalation queue

Default-deny means an automated worker will routinely reach a point it is not authorized to pass. It needs somewhere to go. Without one it will either stop silently — and silence is indistinguishable from progress until you look — or decide anyway, which is the failure the whole apparatus exists to prevent.

So: an **escalation queue**, `ESC-0007`, one per project.

**Project-scoped for reads as well as writes.** An agent escalates within the project it is working in, and the operator answers within that project — there is no global inbox. This matches the register's existing posture (`DR_PROJECT` resolves everything, the server never guesses a namespace) and keeps session start to a single partition Query rather than a table scan.

The cost is real and worth naming: **a worker blocked in a project you don't open this week stays blocked, silently.** In-band tooling cannot fix that — it can only fire when you are already there. That gap belongs to the scheduled sweep (§ GitHub governance), which runs on its own clock with its own credentials and can read across partitions to push what an in-session hook structurally cannot pull.

**It is not a fourth register.** An escalation is not a decision; it is a decision that is owed. Making it a record type would break the one-record-one-persona invariant and would pollute every count and ratio with items that were never decisions. There is no fourth persona either — the Operator's *output* is a record in one of the three registers, and this is the Operator's *inbox*, not a fourth kind of accountability. Three registers of decisions made; one queue of decisions owed.

**This is the one place agents write on their own initiative**, and it is a deliberate exception to the rule above. Everywhere else an agent drafts only what the operator asked for. Here it files without being asked, because escalating is the opposite of acting without authority — the whole point is that raising a hand must be cheaper than proceeding. An agent blocked from escalating will proceed.

**The bar: escalate one-way doors, decide two-way doors — and when genuinely ambiguous, escalate.** Escalate when the action is irreversible, when it exceeds what the governing IDR delegates, when the options differ in a way that changes the bet, or when the call is ambiguous and the stakes are material. **Agents do not make important decisions.** Only the reversible, low-stakes, mechanical tier gets decided in-flight — the naming-a-variable tier — and even then it gets recorded.

**The two failure modes are not symmetric, and the bar is set deliberately loose because of it.** Over-escalating produces a slower inbox, which is visible, annoying, and self-correcting — you widen a delegation grant. Under-escalating produces an important decision made by something with no accountability, discovered later, already load-bearing in the product. Wrong-way ambiguity is cheap; wrong-way silence is not. **When in doubt, escalate.**

**A good escalation is answerable by an un-initiated leader.** This is the litmus test, and it is stricter than it sounds. The agent has an entire session of context the operator does not, and the characteristic failure of escalation is a question that makes sense only to the asker. Write the item so that someone walking in cold — no session history, no repo knowledge — can read it once and decide. That means the background, not just the question. Options with consequences, a recommendation, and the cost of waiting, such that it can be answered from a phone without opening the repo. The item shape below exists to force that.

### Escalation item

```
ref            "ESC-0007" (per-project sequence, own counter)
project        string
raised_by      agent identity + what it was doing when it stopped
raised_at      ISO-8601
question       one sentence: what must be decided
context        why this came up; what was being attempted
options        [{option, consequence, reversible: bool}]
recommendation which option the agent would take, and why
cost_of_delay  what happens if this waits — "nothing until Friday" is a fine answer
blocking       bool — is work actually stopped, or is this a flag for later
governing_ref  the IDR this work sits under, if any
status         "open" | "answered" | "withdrawn"
answered_at    ISO-8601
resolution     what the operator decided, in their words
produced_ref   the ADR/IDR/MDR this resolution created, if any
```

`blocking` is the field that earns its keep: a stopped worker and a note-for-later are different emergencies, and a queue that cannot tell them apart gets ignored at the same rate.

**Answering closes the loop.** A resolution may produce a record — that is the normal outcome, and `produced_ref` links them — but it does not have to. "Don't do that" is a complete answer and needs no ADR.

**Escalations are surfaced, not stored.** They join overdue reviews in `dr_due`'s territory: the session-start hook reports open blocking escalations *before* past-due bets, because someone is stopped right now. Unanswered escalations older than a few days are their own signal — either the delegation grants are too narrow, or the bar above is not being held.

**Watch the rate, and read it in the right direction.** Escalations per week is diagnostic, but given the deliberate asymmetry above, a high rate is the *expected* healthy state early on — it means the bar is being held and it tells you exactly which delegation grants to widen. The alarming reading is the other one: **zero escalations across weeks of autonomous work means agents are deciding things they shouldn't**, because real work produces genuine ambiguity and a quiet queue means it went somewhere other than here.

---

## 2. Assumptions & defaults (flip any of these before building)

| Choice | Default | Notes |
|---|---|---|
| Runtime | Python 3.12 + FastMCP (`mcp` package) + boto3 | TypeScript + `@modelcontextprotocol/sdk` is an acceptable swap; keep tool contracts identical |
| Transport | stdio (launched by Claude Code via `.mcp.json`) | No HTTP server in v1 |
| Distribution | Local checkout only — no PyPI publish in v1 | Wire with `uv run --directory <path>`. Licensing/open-sourcing deferred |
| Storage | One shared DynamoDB table, on-demand billing, all projects namespaced by partition key | Table name via env, default `triad-decision-registers` |
| Infra | CDK Python (`infra/`), wrapped by a Makefile | Table is CDK-owned; no hand-rolled create script |
| Auth | IAM via AWS SSO temp creds (`aws sso login`, `AWS_PROFILE`) | Single-user; no app-level auth in v1 |
| Source of truth | DynamoDB only | Markdown lives in Dynamo (`body_md`); `dr_export` renders files on demand. No automatic repo mirroring in v1 |
| Region | From standard AWS env/profile chain | |

---

## 3. Environment & namespacing (the core UX requirement)

- **`DR_PROJECT`** — the active project namespace (e.g. `example-project`, `acme-app`). Slug format: `[a-z0-9-]{2,40}`.
- **`DR_TABLE`** — table name override. Default `triad-decision-registers`.

**Safe-default behavior (required):** every project-scoped tool resolves the project as: explicit `project` argument → else `DR_PROJECT` → else **error**. The error must be actionable, e.g.:

> `No project set. Set DR_PROJECT in this server's env (recommended: per-repo .mcp.json) or pass project="..." explicitly. Known projects: example-project, acme-app.`

Never fall back to a default project. Never write to a guessed namespace.

**Recommended per-repo wiring** (document this in the README):

```json
// <repo>/.mcp.json
{
  "mcpServers": {
    "triad-dr": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/triad-dr", "triad-dr-mcp"],
      "env": { "DR_PROJECT": "example-project", "AWS_PROFILE": "<sso-profile>" }
    }
  }
}
```

Replace `/path/to/triad-dr` with the absolute path to your `triad-dr` checkout.

Switching repos switches the namespace with zero per-call ceremony. Cross-project **read** tools (`dr_projects`, `dr_search` with `all_projects=true`) work without `DR_PROJECT`.

**Credentials:** the server inherits the launching shell's environment. Run `aws sso login --profile <p>` before starting the client. Expired-SSO errors from boto3 must be caught and returned as an actionable message (`SSO session expired — run: aws sso login --profile <p>`), never a stack trace.

---

## 4. Data model

### Table

Single table, on-demand capacity, point-in-time recovery **enabled**.

| Item | PK | SK |
|---|---|---|
| Decision record | `PROJ#<project>` | `REC#<TYPE>#<seq04>` (e.g. `REC#ADR#0007`) |
| Escalation | `PROJ#<project>` | `ESC#<seq04>` (e.g. `ESC#0007`) |
| Sequence counter | `PROJ#<project>` | `CTR#<TYPE>` (`TYPE` includes `ESC`) |
| Project registry | `META#PROJECTS` | `PROJ#<project>` |

- **Sequence numbers** are per project per type, allocated with an atomic `ADD next_seq 1` on the counter item. Human ref is `<TYPE>-<seq04>` (`ADR-0007`, `IDR-0001`, `MDR-0003`) and is unique *within a project*.
- **Project registry** item is upserted on first write to a project (stores `project`, `created_at`, `record_counts` optional). Powers `dr_projects` without a scan.
- **No GSI in v1.** Listing is a `Query` on the project PK with filter expressions; per-project record counts will be in the tens. Note in README: add a GSI if cross-project status dashboards become a real need.

### Record attributes

```
id            ULID (stable internal id)
project       string
rec_type      "ADR" | "IDR" | "MDR"
seq           number
ref           "ADR-0007" (derived, stored for convenience)
title         string (imperative, e.g. "Use DynamoDB single-table design")
status        "draft" | "proposed" | "accepted" | "rejected" | "superseded" | "deprecated"
persona       derived from rec_type: builder | investor | seller (stored for display)
created_at    ISO-8601
updated_at    ISO-8601
deciders       string[] (default: [])
tags           string[]
links          [{ref, relation}]   // cross-TYPE, same-project only. e.g. MDR-0002 → IDR-0001 "implements-bet"
supersedes     ref | null
superseded_by  ref | null
status_history [{at, status, note?}]        // machine-written; see dr_set_status
reviews        [{at, verdict, evidence, cause?, note?}]  // machine-written; see dr_review. cause required when verdict="missed" on an IDR
fields         map (type-specific structured fields, see templates)
body_md        string (full markdown body; the canonical prose — never machine-mutated)
```

**No cross-project links.** A record may only link to refs within its own project. Links are stored opaquely — the server does not verify the target exists (forward references are legal) — but a `ref` that isn't a well-formed `<TYPE>-<seq04>` is rejected. Cross-project *reads* (`dr_search(all_projects=true)`) remain fine; cross-project *references* do not.

**The governing link.** Per §1, ADRs and MDRs should carry a link to the IDR that funds them (`relation: "implements-bet"`). Not enforced — forward references are legal and a record may be drafted before its bet exists — but `dr_create` on an ADR or MDR with no `implements-bet` link returns a warning naming the omission, and `dr_list` marks such records as unfunded.

**The commitment gate: `requires-attestation`.** The three personas are independent by design, which leaves no way to express the one decision that is not — "the business goes all in." A launch is an Investor decision that is only sound if the other two have said their part: the Builder that it is operationally ready, the Seller that acquisition and path-to-value hold. Rather than a new record type, an IDR names its preconditions as links:

```
links: [
  {ref: "ADR-0012", relation: "requires-attestation"},   # Builder: operationally ready
  {ref: "MDR-0005", relation: "requires-attestation"}    # Seller: acquisition + path to value
]
```

`dr_set_status(ref, "accepted")` on an IDR carrying `requires-attestation` links checks each target's status and **warns, naming every attestation that is not itself `accepted`** — including targets that do not exist yet. It does not block: a solo operator may knowingly commit ahead of an attestation, and the record of having done so is worth more than a refusal that gets worked around. What the gate buys is that "we went all in before the Builder said it was ready" is written down at the moment it happens instead of reconstructed afterward.

**Stale references.** A link whose target has been `superseded` or `deprecated` still resolves, so `unfunded=true` does not catch it — the record has a governing link, pointing at a dead bet. `dr_list(stale=true)` returns records linking to superseded or deprecated targets, and additionally flags refs appearing in `body_md` prose that point at superseded, deprecated, or nonexistent records. Prose refs rot silently and are the harder half: a link can be re-pointed, but a paragraph explaining a decision in terms of a bet that no longer exists just quietly misleads the next reader.

### Status lifecycle (shared across all three types)

`draft → proposed → accepted → (superseded | deprecated)`; `proposed → rejected`.

Soft immutability rule: once `accepted`, edits to `fields`/`body_md`/`title` via `dr_update` are allowed but the tool response must include a warning suggesting `dr_supersede` instead. Status/tags/links edits are always fine. (Enforce with a warning, not a block — this is a solo-operator tool.)

Every transition appends one entry to the `status_history` attribute. `body_md` is authored prose and is never written to by the server; `dr_export` renders `status_history` and `reviews` into their own sections of the output file.

**Killing a bet kills what it funded.** When an IDR moves to `rejected` or `deprecated` from any status, the ADRs and MDRs linked to it by `implements-bet` are no longer governed — the work they authorized has lost its funding. (`superseded` is deliberately excluded: a superseded bet hands its work to the record that replaced it. And the rule is not restricted to bets that were ever `accepted` — forward references are legal, so an ADR may link to an IDR that gets rejected while still `proposed`, and that work is just as unfunded.) `dr_set_status` returns the affected refs and the tool response says plainly that they are now orphaned. It does not cascade the status change automatically: some of that work is still worth keeping under a different bet, and re-homing it is a decision, not a side effect. `dr_list(unfunded=true)` surfaces them until they are re-linked, deprecated, or superseded.

**Warn, never block.** No tool in this server refuses a write on governance grounds. Unfunded records, missing evidence, sub-minute acceptances, orphans after a kill — all of these produce a warning in the tool response and a flag in `dr_list`, and the write succeeds. The register's job is to make the state of things visible to an operator who can read; it is not an approval system that can be defeated by finding a cheaper write path. (Malformed and incomplete input is a different matter and is still an error: a bad ref format, an invalid status transition, a missing required argument such as `dr_review`'s `cause` on a missed IDR. The distinction is that these calls cannot be recorded coherently at all, not that the register disapproves of them.)

**Reviews are not status.** A missed success criterion does not automatically deprecate a record — persevering with a revised bet is a legitimate response, and the tool does not get to make that call. `dr_review` records what happened; deciding what to do about it is a separate, deliberate act via `dr_set_status`, `dr_supersede`, or a new IDR.

### Type templates (guidance, not schema enforcement)

The server does not reject missing fields, but tool descriptions must state the expected `fields` keys per type so the calling model fills them:

- **ADR (Builder):** `context`, `decision`, `consequences`, `alternatives_considered`
- **IDR (Investor):** `stage` (`spike` | `commitment` — see below), `bet` (one sentence), `thesis`, `investment` (time/money committed), `success_criteria`, `kill_criteria`, `cause_evidence` (see below), `review_date`, `delegation` (what authority, if any, agents hold for work under this bet — omit for none, which is the default)
- **MDR (Seller):** `audience` (segment; name buyer and user separately where they differ), `jobs_to_be_done` (what the person is hiring this to do), `value_delivered` and `time_to_value` (see below), `positioning`, `channel_motion`, `evidence_plan` (how and when `user_evidence` gets collected — method, sample, timing: "8-person prototype round, week of Sep 15, before build starts"), `user_evidence` (what was actually observed, with source and sample — "6 of 8 in the August prototype round failed to find export"; observation, not assertion), `experiment`, `success_metric`

**`stage` — a spike and a commitment must not share a record.** They have opposite default semantics on failure and a bet spanning both will always mishandle its own. A **spike** is disposable by design: its kill criterion is its appetite ("four hours, then stop regardless"), and a spike that misses is *informative*, not a loss — the default action at review is close-and-learn. A **commitment** is not meant to be thrown away: its kill criteria are about outcomes, and a miss is a real loss requiring a persevere / supersede / kill decision. Where one record covers exploratory build *and* a committed market window under a single kill criterion, split it: the spike answers a question, and its answer is the input to the commitment.

**`cause_evidence` — what will let you tell `market` from `delivery` at review.** §1 makes that distinction the most consequential judgment in the register, and it cannot be made from outcome data alone: outcomes only ever say "the number was missed," which reads as the market declining. Decide *at bet time* what observation will separate "they saw the intended thing and didn't want it" from "they never saw the intended thing." Session recordings, a funnel step that proves the feature was reached, an intercept asking non-converters what they expected, a working-vs-broken instrumentation check. **A bet that cannot answer this can only ever be charged to the market**, which is precisely the expensive mistake §1 exists to prevent. `dr_create` warns on an IDR with no `cause_evidence`.

**`value_delivered` and `time_to_value` — what the customer gets, and how long they wait.** The MDR template captures who it is for and what they are hiring it to do, and would otherwise never ask what they actually receive at this phase. State the benefit delivered *in this phase* and the elapsed time from a person's first commitment (a signup, a payment, an hour of their attention) to receiving it. **A phase that asks for commitment while deferring all value is a common and dangerous shape** — "sign up now, the product arrives later" — and it is invisible unless the template asks. If the honest answer is "no benefit this phase," that is a legitimate answer and belongs in writing, where the governing IDR's review can weigh it.

**Placeholders and unmeasurable criteria.** On transition to `accepted`, `dr_set_status` scans the record's `fields` and `body_md` for unfilled placeholders — `[X]`, `TBD`, `TODO`, `???`, `<...>` and similar — and warns naming each one and where it sits. An accepted bet committing `$[X]/month` has no appetite, and an appetite is the point. Separately, each success criterion should be measurable with an instrument that **already exists**: a gate requiring referrer-based attribution that the event schema has no field for is unmeasurable at bet time and will still be unmeasurable at review. The server cannot check this — it does not know your schema — so `/triad-dr:bet` asks per criterion, and the honest answer may be that instrumenting it is part of the investment.

`user_evidence` is the field that makes the Seller an oracle rather than an advocate. An MDR whose `user_evidence` is empty is a hypothesis, and `dr_create` says so in its response — it does not block, because hypotheses are the normal starting state.

`evidence_plan` is what keeps a hypothesis from staying one until review day. The governing IDR's `review_date` is a backstop, not a discovery method: by the time a criterion is judged, the build is paid for. A plan to put the thing in front of eight people beforehand costs a fraction of that and answers the same question. So `dr_create` warns when an MDR has **neither** `user_evidence` nor `evidence_plan` — evidence not yet gathered is normal, but no evidence and no plan to get any means the bet's only trip-wire is its most expensive one. Plans are prose; the server does not parse the timing or check it against `review_date`. That is the operator's judgment, and `dr_due` is the reminder to exercise it.

`dr_template(type)` returns a ready-to-fill markdown skeleton per type so records stay consistent.

---

## 5. MCP tools

All project-scoped tools accept optional `project` (overrides `DR_PROJECT`). Return shapes are JSON; include `ref`, `title`, `status` in every summary row.

1. **`dr_create(rec_type, title, body_md?, fields?, tags?, links?, deciders?, status="proposed", project?)`**
   Allocates seq atomically, writes record, upserts project registry. Returns full record incl. `ref`. Warns on a missing `implements-bet` link (ADR/MDR) and on an MDR carrying neither `user_evidence` nor `evidence_plan`; never blocks.
2. **`dr_get(ref, project?)`** — full record by ref (or by ULID via `id=` param).
3. **`dr_list(rec_type?, status?, tag?, unfunded?, limit=50, project?)`** — summaries, newest first. `unfunded=true` returns ADRs/MDRs with no `implements-bet` link.
   **One name per axis.** There is no `persona` filter: `persona` is 1:1 with `rec_type` and derived from it, so a second parameter over the same axis is two vocabularies for one query with two ways to spell every call. `persona` stays a stored display attribute and appears in summary rows and export frontmatter; `rec_type` is how you filter.
   **Sort:** the SK (`REC#<TYPE>#<seq04>`) orders lexically, so a reverse Query groups by type before time — an unfiltered list would rank a March MDR above an August ADR. Query the partition, then sort by `created_at` descending **client-side** before applying `limit`. Correct at the tens-of-records scale this targets; revisit only if a project passes ~1k records.
4. **`dr_update(ref, patch, project?)`** — partial update of mutable attrs; emits the accepted-record warning per §4; bumps `updated_at`.
   **Patch semantics (least surprise):** a key present in `patch` replaces that attribute wholesale — `{"tags": ["a"]}` sets tags to exactly `["a"]`. Keys absent from `patch` are untouched. No implicit merging or appending, for lists or maps. `status`, `status_history`, `reviews`, `seq`, `ref`, `id`, `project`, `rec_type`, `created_at`, `supersedes`, `superseded_by` are not patchable — use the dedicated tools; attempting them is an error naming the right tool.
5. **`dr_set_status(ref, status, note?, project?)`** — validates transition against lifecycle; appends `{at, status, note}` to the `status_history` attribute. Does **not** modify `body_md`.
6. **`dr_review(ref, verdict, evidence, cause?, note?, project?)`** — records an outcome against the record's stated criteria. `verdict` ∈ `met | missed | mixed | too-early`. `evidence` is required prose: what was observed, from what source, at what sample. Appends `{at, verdict, evidence, cause, note}` to `reviews`. Does **not** change status (see §4).
   Intended for IDRs at `review_date` and MDRs after an experiment; permitted on any record. On an IDR, the response restates that record's `success_criteria` and `kill_criteria` so the verdict is judged against what was actually written down.
   **`cause` is required when `verdict="missed"` on an IDR.** `cause` ∈ `market | delivery | both`, defined in §1: `market` is Seller accountability and disproves the thesis; `delivery` is Builder accountability and leaves the thesis **untested**; `both` leaves it untested until the delivery gap closes. Omitting it is an incomplete call and errors, naming the three options and what each implies — this is a missing required argument, not a governance gate (see "Warn, never block" in §4). The response echoes the routing: a `delivery` miss says plainly that killing the bet here would discard an untested thesis. `cause` is accepted on any record but only required in this one case.
7. **`dr_due(as_of?, project?)`** — IDRs whose `fields.review_date` has passed with no `dr_review` entry after that date. The one report the register owes its operator: bets that were supposed to be judged and weren't.
7a. **`dr_escalate(question, context, options, recommendation, cost_of_delay, blocking=false, governing_ref?, project?)`** — file a decision the operator owes. The one tool an agent may call unprompted (§1). Returns `ESC-000N`.
   The description must hold the bar from §1: one-way doors and grant-exceeding actions, never mere uncertainty with a reversible default. It must also state that `options` with consequences plus a `recommendation` is what makes the item answerable in thirty seconds — an escalation that just asks a question will sit.
7b. **`dr_escalations(status="open", blocking_only=false, project?)`** — the operator's inbox for this project. Blocking items first, then newest — the same recency direction as `dr_list`, so the two listings never disagree about what "first" means. `blocking` floats items up because that is what the flag is for; age stays visible in `raised_at`. Deliberately **no `all_projects`**: escalations are project-scoped in both directions (§1), so this is a single partition Query and never a Scan. Cross-project visibility is the scheduled sweep's job, not a tool call's.
7c. **`dr_answer(ref, resolution, produces?, project?)`** — resolve an escalation. `resolution` is the operator's decision in their own words. `produces` optionally creates the resulting ADR/IDR/MDR in the same call and links it via `produced_ref`; omitting it is legitimate, because "don't do that" is a complete answer that needs no record.
8. **`dr_supersede(old_ref, new_record, project?)`** — creates the new record (same type unless specified) and sets `supersedes`/`superseded_by` both ways. Atomic via TransactWriteItems.
   **The status flip is deferred.** The link lands immediately so the chain is machine-followable, but the predecessor keeps its status until the replacement is `accepted` — at which point `dr_set_status` performs the flip. Under default-deny every record is human-ratified, so flipping on creation would leave a gap with no accepted record at all: the old one retired and the new one merely proposed. A replacement created already-`accepted` flips in the same transaction, since there is nothing to wait for. A replacement that is `rejected` releases its predecessor's `superseded_by`, while keeping its own `supersedes` as a record of intent.
8a. **`dr_adopt_successor(old_ref, successor_ref, project?)`** — link an **existing** record as the successor. `supersedes`/`superseded_by` are structural and not patchable via `dr_update`, so without this a supersession drafted now and accepted later could never be linked at all; the relationship would survive only in prose and status-history notes, which no query can follow. Same deferred semantics. Also repairs the workaround state: a predecessor already at `superseded` with a null `superseded_by` gets its link filled in, status untouched.
9. **`dr_search(query, rec_type?, all_projects=false, project?)`** — case-insensitive substring/keyword match over `title`, `tags`, `body_md`, and `fields` values (so `user_evidence` is searchable). Implementation: Query the project partition and match client-side (or Scan when `all_projects`). Document the scale ceiling; fine for hundreds of records.
10. **`dr_projects()`** — list registry items with per-type counts.
11. **`dr_template(rec_type)`** — markdown skeleton for that type.
12. **`dr_export(ref?, rec_type?, out_dir="docs/decisions", project?)`** — renders one or all records to `<out_dir>/<ref>-<slug>.md` relative to the CWD (frontmatter + `body_md` + rendered `## Status history` and `## Reviews` sections). Paths are trusted, not sandboxed — the developer is responsible for running the client in the right directory. Returns paths written.

**Tool descriptions are part of the product.** Write them so a model with no other context uses the register correctly. They must convey, at minimum:

- Which persona owns which type, and that **user experience, usability findings, and voice of customer go in MDRs** — there is no separate user record type, and putting a research finding in an ADR is a category error.
- That ADRs and MDRs link back to the governing IDR.
- The propose-then-accept flow, and that superseding beats editing an accepted record.
- That a bet with success criteria is expected to receive a `dr_review` at its `review_date`, and that every MDR should carry an `evidence_plan` so the review is a backstop rather than the first time anyone looks.
- That a missed IDR must name its `cause`, and what each option routes to: `market` → Seller, thesis disproven, next record is an MDR; `delivery` → Builder, thesis untested, next record is an ADR.

---

## 6. Bootstrap & ops

**Infrastructure is CDK Python in `infra/`, one stack, wrapped by a Makefile.** Nobody remembers CDK commands; the Makefile is the interface.

- Stack resources: the DynamoDB table (on-demand, PITR on, `RemovalPolicy.RETAIN` in every environment — losing decision records to a `cdk destroy` is unacceptable and the table is free to keep) and one IAM managed policy granting exactly the needed table actions scoped to the table ARN, for the developer's SSO permission set.
- Makefile targets, and nothing beyond what's needed: `install`, `deploy`, `diff`, `test`, `run`. Each is one word and prints what it did.
- README documents the whole path in order: `aws sso login` → `make deploy` → paste `.mcp.json` snippet → first `dr_create`. It opens with §1 — a reader who doesn't understand the three personas will file records in the wrong register and the tool is worth nothing.
- `CLAUDE.md` at the repo root: the same facts framed for an agent — layout, how to run tests, the project-namespacing rule, the "never guess a project" invariant, the no-cross-project-links rule, and the persona rules from §1 (user/UX belongs to the Seller; ADRs and MDRs trace to a governing IDR).
- On server startup: `DescribeTable`; if missing, do **not** auto-create — log one clear line telling the operator to run `make deploy`. Tool calls that fail on a missing table return the same remediation rather than a boto3 error. (Consistent with the "complain, don't guess" posture.)
- Structured logs to stderr only (stdio transport — stdout is protocol).
- No TTL, no deletes in v1. `dr_delete` intentionally omitted; wrong records get `rejected` or `superseded`. (Add a guarded delete later if truly needed.)

## 7. Testing & acceptance criteria

1. `moto`-based unit tests for: seq allocation under concurrent creates (no dup refs), status transition validation, supersede transaction, project resolution precedence (arg > env > error).
2. Error path test: no `DR_PROJECT`, no arg → error message contains remediation and known-projects list.
3. Round-trip test: create → export → file content contains frontmatter with `ref`, `status`, `persona`, plus `## Status history` and `## Reviews` sections rendered from their attributes.
4. `dr_list` ordering test: records created across all three types out of seq order return sorted by `created_at` descending, not by SK.
5. `dr_update` patch test: replacing `tags` replaces wholesale; patching a protected attribute (`status`, `reviews`, `seq`, …) errors and names the correct tool.
6. Link validation test: a malformed ref is rejected; a well-formed ref to a not-yet-existing record is accepted.
7. `dr_review` test: appends to `reviews` without changing `status`; a `missed` verdict on an IDR echoes that record's `success_criteria` and `kill_criteria` in the response.
8. `dr_review` cause test: `verdict="missed"` on an IDR without `cause` errors and the message names all three options; with `cause="delivery"` it succeeds and the response says the thesis is untested; the same omission on an MDR or a non-`missed` verdict succeeds.
9. `dr_due` test: an IDR with a past `review_date` and no later review appears; the same IDR after a `dr_review` does not.
10. Governance warning tests: `dr_create` for an ADR with no `implements-bet` link warns and still succeeds; `dr_list(unfunded=true)` returns it. An MDR with neither `user_evidence` nor `evidence_plan` warns and still succeeds; one with `evidence_plan` alone does not warn.
11. `dr_list` surface test: passing `persona=` is rejected as an unknown argument; `persona` is still present in every summary row.
12. Manual acceptance: from two different repos with different `.mcp.json` env values, `dr_list` returns disjoint sets with identical calls.

## 8. Non-goals (v1)

Multi-user auth/sharing, HTTP/AgentCore hosting, web UI, full-text search infrastructure, automatic markdown mirroring into repos, analytics, PyPI publication, cross-project links.

**A separate user/UX record type is not a v1 gap — it is a deliberate rejection.** Per §1, the user lens is a Seller function discharged through MDRs. Reopening it means reopening the persona model, not adding an enum value.

Everything else above is a plausible v2 item once the register has real records in it.
