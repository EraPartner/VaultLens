# VaultLens

**A self-hosted "LLM wiki" — a compounding, agent-maintained knowledge base you run in Obsidian + Claude Code.**

VaultLens is a system template (after [Karpathy's llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f))
for turning a pile of source material into a durable, cross-linked knowledge base that
agents grow and curate over time — and then *consuming* that knowledge base from project
workspaces. You drop sources into `raw/`; agents distil them into a curated `wiki/`; your
`projects/` read from the wiki without ever writing back to it.

Clone it, point Obsidian at it, and start a Claude Code session — `CLAUDE.md` is the
operating schema that tells agents how to work here.

## Architecture — four layers

Dependencies flow left → right; each layer consumes the one before it and never writes back.

```
raw/            ← immutable ingested sources (source of truth)
  → wiki/       ← curated, LLM-generated knowledge base (agent-owned)
    → projects/ ← application workspaces that consume the wiki
CLAUDE.md       ← the operating schema that governs all of it
```

- **`raw/`** — immutable source docs (articles, PDFs, papers, notes). Normal ingest never modifies it.
- **`wiki/`** — the curated layer, owned by the agents: source pages, entities, concepts, topics,
  syntheses, comparisons, preserved Q&A, and reports. `wiki/home.md` + `wiki/SETUP.md` are the
  reader-facing entry points; as content accrues, the agents maintain `wiki/index.md` (a Dataview
  catalog) and the append-only `wiki/log.md` as the mandatory navigation files.
- **`projects/`** — application workspaces that reference wiki pages but **must not** write to
  `wiki/` or `raw/`.
- **`CLAUDE.md`** — the source of truth for how Claude Code operates in the vault.

## What's Included

- `CLAUDE.md` — operating schema for LLM agents (start here)
- `.claude/agents/` — 12 custom wiki subagents (see **Agents** below)
- `.claude/skills/` — auto-loading operational runbooks (ingest, maintenance, projects, agent selection)
- `raw/` — source-of-truth ingest area (immutable inputs)
- `wiki/` — the curated knowledge base + page templates
- `projects/` — application workspaces that consume the wiki
- `tools/` — the `wiki.py` CLI (lint, search, ingest, index, links, projects…) plus the
  scheduled-agent dispatcher in `tools/schedule/`
- `.mcp.json` — registers [qmd](https://www.npmjs.com/package/@tobilu/qmd) for hybrid search
- `.devcontainer/` — a hardened, egress-locked sandbox the agents run inside (see its `README.md`)
- `.gitignore` — excludes your data, keeps the system

## What the system does

Beyond the folder skeleton, the template ships a working agent operating model:

- **Ingest** — drop a file or URL in `raw/inbox/`; the `wiki-ingest` agent extracts claims into a
  source page and threads them into concept/topic pages, with links and a lint pass. PDFs are
  first-class (read directly; large ones pre-extracted to `raw/sources-text/`).
- **A fleet of wiki agents** (`.claude/agents/`) — `wiki-ingest`, `wiki-enhancer`,
  `wiki-source-verifier`, `wiki-quality-reviewer`, `wiki-contradiction-detector`, `wiki-search`,
  the read-only thinking agents `wiki-challenge` / `wiki-connect` / `wiki-emerge` /
  `wiki-idea-discovery`, plus a **Chief of Staff** (`wiki-cos`) that produces cross-project briefs.
  Invoke them by name in a session; the `wiki-agents` skill helps pick the right one.
- **Projects layer** — scaffold workspaces that consume the wiki (details below). Each carries a
  dormant `AGENDA.md`; opt in and the nightly **`wiki-project-runner`** grooms and executes its
  due tasks inside `projects/<slug>/` (applied-not-committed, with a snapshot for undo).
- **Scheduled agents** — a host-side catch-up dispatcher (`tools/schedule/`) runs the
  maintenance/thinking agents on a launchd tick and files dated outputs under `wiki/reports/`.
- **Hybrid search** — qmd (BM25 + vector + LLM-rerank) is the primary engine, exposed over MCP;
  `python3 tools/wiki.py search "…"` is the always-works substring fallback.
- **Sandboxed by default** — agents run in an egress-locked devcontainer so autonomous runs can't
  reach anything off their allowlist.

Operating detail lives in `CLAUDE.md` and the runbooks under `.claude/skills/` — this README stays
at the overview altitude.

## Quick Setup

```bash
# Clone this template
git clone https://github.com/EraPartner/VaultLens.git my-wiki
cd my-wiki

# Initialize the data skeleton (canonical set — CLAUDE.md "Directory contract" is authoritative)
mkdir -p raw/sources raw/sources-text raw/assets raw/inbox raw/review-inbox
mkdir -p wiki/system wiki/sources wiki/entities wiki/concepts wiki/topics \
         wiki/syntheses wiki/comparisons wiki/queries wiki/reports wiki/inventory wiki/_templates

# Open in Obsidian
open .
```

## Projects Layer

The `projects/` directory is an application layer on top of the wiki. Each subfolder is one project
workspace that consumes the wiki as a knowledge base **without ever writing to it**.

Each project has a `project.md` declaring its description, layout, rules, and linked wiki pages. The
scaffold also drops a `CLAUDE.md` entrypoint so a session launched from inside the project picks up
the project's context plus the root schema (`## Working inside a project`).

### Scaffold a project

```bash
python3 tools/wiki.py project new my-project                              # create a project
python3 tools/wiki.py project link my-project concepts/trusted-execution  # link wiki pages into it
python3 tools/wiki.py project list                                       # list all projects
python3 tools/wiki.py project show my-project                             # inspect structure
```

### Work inside a project

`cd` into `projects/<slug>/` and start Claude Code. The project's `CLAUDE.md` loads `project.md`,
and the root schema loads automatically; the root `## Working inside a project` section defines the
wiki search ladder, citation discipline, and the Q&A artifact convention. Durable Q&A lands in
`projects/<slug>/queries/` by default, redirectable via `## Rules` in `project.md`.

### `project.md` schema

```yaml
---
type: project
title: My Thesis
status: active
tags: [tee, sgx]
domain: research
wiki_refs:
  - concepts/trusted-execution-environments
  - topics/remote-attestation
---

## Description
...

## Layout
projects/my-project/
  project.md     ← metadata, description, layout, rules, wiki refs
  CLAUDE.md      ← AI entrypoint: @project.md + operating principles (auto-generated)
  TODO.md        ← per-project todo; embedded into projects/TODO.md (auto-generated)
  queries/       ← Q&A artifacts
  papers/        ← relevant PDFs
  meetings/      ← dated meeting notes

## Rules
- Never modify raw/ or wiki/ — treat them as read-only.
- Save all Q&A artifacts to queries/.
```

## See Also

- **`CLAUDE.md`** — the full operating schema (directory contract, page metadata, conventions, agents, search).
- Original inspiration: Karpathy's [llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
