---
name: wiki-agents
description: Choose and launch a custom wiki agent when the user asks which agent fits, names a wiki agent, or needs launcher, model, or effort guidance. This skill routes work; operation procedures remain in their dedicated skills.
---

# Wiki agents — picking and running

Canonical role definitions live in `.agents/roles/*.md`; generated adapters under
`.claude/agents/` and `.codex/agents/` expose them to each client. Headless and batch runs go
through `wiki-agent.py`, which supports both `claude` and `codex`. The agents are **orthogonal** —
pick by what you have and what you want:

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
worth keeping, since they never write). Read the `.agents/roles/*.md` files for exact handoff lists.

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
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/x.md [--cli claude|codex] [--model MODEL] [--effort high]
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
`brain-wiki project-run --project <slug>` (uses the `project-write` access profile).
Direct `wiki-agent.py` invocations apply the same runtime policy. Resolve its clarifications with the `wiki-project-clarify` skill;
manage agendas with `wiki.py project agenda …` (see `.agents/skills/wiki-projects/SKILL.md`).

**Models:** headless runs resolve the canonical role's `model_profile` through
`tools/model-profiles.json`. Claude maps `standard`/`deep` to `sonnet`/`opus`;
Codex maps them to `gpt-6-luna`/`gpt-6.1-sol`. Explicit `--model` overrides the
environment, saved per-provider model, local profile mapping, and tracked mapping,
in that order. An empty model selects the provider's native default. Plain
interactive sessions keep their native model unless explicitly configured.
**Effort:** the role's `reasoning_effort` is the default; `--effort` overrides it.
Both providers receive the selected effort. Codex uses `model_reasoning_effort`;
Claude uses `--effort`. Supported values depend on the selected model.

**Interactive subagent runs ignore those flags** — `wiki-agent.py` strips the frontmatter, so the
CLI values apply only to headless runs. Invoked by name in a session, generated adapters map each
canonical role's `permission_profile`, `model_profile`, and `reasoning_effort` to provider settings.
Adapters resolve each provider's profile mapping from the tracked
`tools/model-profiles.json` only and set the role's effort and permissions. Operator-local
`models` and `profiles` in gitignored `tools/llm.local.json` never reach the tracked adapters,
so `--check` agrees with CI on every machine. For a personal copy that applies them, export
with `--output-dir <directory> --local-models`. An empty mapping inherits the parent model.
Interactive adapters are snapshots and do not use launch-scoped environment model overrides.
Regenerate adapters after role metadata or mapping changes:
`python3 tools/agents/generate-adapters.py`.

`--check` reports adapter drift with exit code 1 and blocked filesystem access
with exit code 2. A blocked set remains unverified. To review generated output
without touching deployed adapters, use `--provider claude --output-dir <directory>`.
`tools/agent_capabilities.py` maps canonical permission profiles for both native
adapters and headless tool grants. Access profiles independently constrain files and networking.
Agent search uses a qmd-compatible lexical service built only from approved files; a shared full
vault index is never passed into a restricted run. `qmd query` can rank with the operator's qmd
index through a launcher-side bridge that returns only paths the run may read.

`--debug` previews the selected command without invoking the model, extracting
PDF text, or promoting an inbox PDF. Scheduler `--dry-run` also leaves its
ledger, logs and runtime state unchanged.

Native interactive subagents run within their parent's access profile. The role's narrower file
scope is an instruction boundary; it does not create a separate operating-system sandbox. Choose
a narrow parent profile or start a separate `wiki-agent.py` invocation when independent confinement
is needed. Model selection does not change access policy.

**Local runtime:** `wiki-agent.py` runs the selected native Claude or Codex CLI through Anthropic's
whole-process sandbox runtime. It does not use a container. Invoke it directly or use
`brain-wiki <agent> …` and `brain-cos`. Interactive wrappers are `brain-agent`, `brain-claude`, and
`brain-codex`; `brain-shell` opens a shell inside the same boundary. Use `--access-profile NAME`
and repeatable `--read-path PATH` to select a narrower policy. `--project SLUG` scopes project
work. Interactive launches inside a recognized project default to `project-write`; vault launches
default to `wiki-read`. Request wiki editing with `--access-profile wiki-write`.
Plain shells default to `wiki-read` outside a project.

Only the selected provider's isolated login state and approved model endpoints are available.
Web research requires an explicitly selected network profile. Sources, the consent queue, tools,
instructions, Obsidian configuration, and Git metadata remain protected from agent writes.
Review the resolved read/write paths before sending personal notes to a model provider. Runtime
availability checks and profile inspection are described in `README.md`.

**Chief of Staff** (`wiki-cos` / `brain-cos`): cross-project daily brief, project status, commitment
surface, and inbox triage. Read-only; advises, never writes. Modes: `--mode brief` (default),
`--mode status --project <slug>`, `--mode surface`, `--mode inbox`. The launcher gathers live
context (open items from non-frozen projects, wiki log tail, inbox listing) and injects it before
invoking the agent. Uses the `cos-read` access profile.
