# triad-dr plugin

Drives use of the decision registers — **ADR** (Builder), **IDR** (Investor), **MDR** (Seller).

The registers are storage. This plugin is the read path: the rituals that keep a register load-bearing instead of write-only, and a session-start nudge for bets that came due and were never judged.

## What's in it

| | |
|---|---|
| `/triad-dr:bet` | Write an IDR — a bet with an appetite, numbered criteria, and a date you have to honor. The entry point. |
| `/triad-dr:review` | Judge a bet against what was actually written down, with evidence and a named cause. |
| `/triad-dr:record` | Capture an ADR or MDR from the session's work, under the bet that funds it. |
| `/triad-dr:answer` | Work the escalation queue — decisions automated workers are stopped on. |
| `/triad-dr:apply` | Drain published decision events and apply them to work items. |
| `/triad-dr:status` | What's blocked, what's overdue, what's ungoverned, what's delegated, what the shape says. |
| `decision-registers` skill | The persona model, the rules, when to escalate rather than decide, and how to frame a decision so you can make it in one read. |
| SessionStart hook | Prints blocked workers and bets past their review date. Silent otherwise. |

## Two things it deliberately does not do

**It never writes a *record* on its own.** Records are created when the operator asks, as the product of a working session. No background filing, no catch-up batches. Volume is how a register becomes worthless.

The one exception is the **escalation queue**, which agents file to unprompted and by design. Authority is default-deny, so a worker will routinely reach something it isn't allowed to decide. Escalating is the opposite of acting without authority — an agent that can't raise a hand will either stop silently, which looks exactly like progress, or decide anyway. The bar is *one-way doors, and genuine ambiguity with real stakes*; when in doubt it escalates, because a slow inbox is a cheaper failure than an important decision made by something with no accountability.

**It never blocks.** Unfunded work, missing evidence, an accepted record being edited — all of these warn and succeed. The register makes state visible to someone who can read; it is not an approval system to be routed around.

## Install

A plugin directory has to be registered as a marketplace before it can be installed:

```
/plugin marketplace add /path/to/triad-dr/plugin
/plugin install triad-dr@triad-dr
```

Replace `/path/to/triad-dr` with the absolute path to your `triad-dr` checkout.

Restart the session afterwards so the commands, skill, and hook load.

Then wire the MCP server per repo, since the project namespace is per repo:

```json
// <repo>/.mcp.json
{
  "mcpServers": {
    "triad-dr": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/triad-dr", "triad-dr-mcp"],
      "env": { "DR_PROJECT": "<project-slug>", "AWS_PROFILE": "<sso-profile>" }
    }
  }
}
```

Replace `/path/to/triad-dr` with the absolute path to your `triad-dr` checkout. The repo ships `.mcp.json.example` with this same block — copy it to `.mcp.json` and fill in the placeholders.

The table has to exist first — `make deploy` from the repo root, once.

## The session-start hook

Reads `DR_PROJECT` from the environment and prints open blocking escalations, then bets past their review date — blocked workers first, since someone is stopped right now and an overdue review is merely late. It stays quiet when there's nothing to report, when the repo isn't a register repo, and when the CLI isn't reachable — and it always exits 0, with an 8-second wall-clock cap, so a stalled network or an expired SSO session can't hold up a session start.

It needs the `triad-dr` CLI. Either put it on `PATH` (run from the repo root, so `$(pwd)` resolves to your checkout):

```bash
uv tool install --from "$(pwd)" triad-dr-mcp
```

or point the hook at the checkout, again from the repo root:

```bash
export TRIAD_DR_HOME="$(pwd)"
```

(add that `export` to your shell profile with the path spelled out if you want it to persist beyond the current shell).

Without one of those it does nothing, silently, which is the correct behavior for a hook nobody has configured.

## The one rule worth knowing up front

**No bet, no work.** Every ADR and MDR traces to a governing IDR. Maintenance isn't an exemption — it's a standing operations IDR with an appetite and a kill criterion, reviewed like anything else. If you ask for a record and no bet covers it, the commands will tell you rather than inventing a link.
