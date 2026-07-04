---
name: wiki-agents
description: Choose and run the vault's custom wiki agents — ingest, enhance, verify, quality-audit, contradiction-detect, search, challenge (red-team), connect, emerge, discover, and the Chief of Staff. Use when the user wants to improve/audit/verify wiki pages, red-team a decision, bridge domains, surface patterns, rank next steps, get a daily brief, or asks which agent fits a task.
---

# Wiki agents — picking and running

Agent definitions live in `.claude/agents/*.md` (source of truth) — native Claude Code subagents
you can invoke by name in an interactive session. Headless and batch runs go through
`wiki-agent.py`, which injects the agent body as a `claude -p` system prompt and builds the
per-agent tool allowlist. The agents are **orthogonal** — pick by what you have and what you want:

| You have… | You want to… | Use |
|---|---|---|
| A new file in `raw/sources/` | Add it to the wiki | `wiki-ingest` |
| An existing wiki page that's shallow/stale | Improve it in place (also loop mode: "next stub / random / keep going") | `wiki-enhancer` |
| A wiki page that may drift from its source | Verify against the source | `wiki-source-verifier` |
| A wiki page to structurally audit | Audit, no edits | `wiki-quality-reviewer` |
| Suspicion two pages disagree | Surface + analyze the conflict | `wiki-contradiction-detector` |
| A research question, no project context | Synthesized cited answer | `wiki-search` |
| A pending decision/idea | Red-team it against your own history | `wiki-challenge` |
| Two unrelated domains | Bridge them for novel ideas | `wiki-connect` |
| Recent activity, no named theme | Surface unnamed patterns | `wiki-emerge` |
| Loose ends, no clear next move | Rank next-direction candidates | `wiki-idea-discovery` (`discover`) |
| An opted-in project's due AGENDA tasks | Execute them overnight (scheduler-driven) | `wiki-project-runner` (`project-run --project <slug>`) |
| A question about a `projects/` project | Project-scoped cited answer | Launch any AI CLI from `projects/<slug>/` |

**Reads / writes:** ingest (raw+wiki → wiki) · enhance (raw+wiki → wiki) · quality/verify/contradict/
search and the thinking agents challenge/connect/emerge/discover (all read-only). **Handoff:** each
agent ends by recommending the next (quality → enhancer to apply fixes; contradict → verifier to
decide which side is right; the thinking agents → enhancer or `inventory new` to persist anything
worth keeping, since they never write). Read the `.claude/agents/*.md` files for exact handoff lists.

**Thinking agents (read-only "think with me" layer):** they reason over the vault and emit text
only — durable output is filed by the operator via the recommended handoff. `challenge --source
"<position>"` red-teams a decision against your own queries/log/superseded pages and the operator
profile; `connect --source "<A>" --page "<B>"` bridges two domains via the link graph; `emerge
[--source "<timeframe>"]` surfaces unnamed patterns from recent activity (default last 30 days);
`discover` ranks 3–5 next-direction candidates from inventory questions, orphans, and sparse pages.

## Invocations

```bash
python3 tools/agents/wiki-agent.py ingest --source raw/sources/x.pdf
python3 tools/agents/wiki-agent.py enhance --coverage
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/x.md [--cli claude --model sonnet --effort high]
python3 tools/agents/wiki-agent.py verify --source wiki/sources/x.md
python3 tools/agents/wiki-agent.py search --page "topic"
python3 tools/agents/wiki-agent.py contradict
python3 tools/agents/wiki-agent.py challenge --source "the decision/idea to red-team"
python3 tools/agents/wiki-agent.py connect --source "domain A" --page "domain B"
python3 tools/agents/wiki-agent.py emerge [--source "2 weeks"]
python3 tools/agents/wiki-agent.py discover
```

**Project runner (`project-run`)** is a **writer**, normally driven by the nightly scheduler — one
invocation per opted-in (`enabled: true`) project with a due `AGENDA.md` task. It grooms the Inbox,
executes clear+due tasks inside `projects/<slug>/` (applied-not-committed), files clarifications for
ambiguous ones, and prints a roll-up block. Run it by hand only to test:
`brain-wiki project-run --project <slug>` (needs the `project` sandbox profile, so launch via
`brain-wiki`, not bare `wiki-agent.py`). Resolve its clarifications with the `/wiki-project-clarify` skill;
manage agendas with `wiki.py project agenda …` (see `.claude/skills/wiki-projects/SKILL.md`).

**Models:** `sonnet` (default) / `haiku` / `opus` via `--model`. **Effort:** `low` / `medium` /
`high` (default) / `xhigh` via `--effort` (currently informational — the headless CLI inherits the
session effort level).

**Host vs sandbox:** `wiki-agent.py` refuses to run on the host — invoke wiki agents via
`brain-wiki <agent> …` and the Chief of Staff via `brain-cos` (other wrappers: `brain-claude`,
`brain-shell`).

**Chief of Staff** (`wiki-cos` / `brain-cos`): cross-project daily brief, project status, commitment
surface, and inbox triage. Read-only; advises, never writes. Modes: `--mode brief` (default),
`--mode status --project <slug>`, `--mode surface`, `--mode inbox`. The launcher gathers live
context (all `projects/*/TODO.md` open items, wiki log tail, inbox listing) and injects it before
invoking the agent. Always uses the reader profile.
