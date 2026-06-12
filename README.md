# Second Brain - System Template

## What's Included

- `CLAUDE.md` - Operating schema for LLM agents
- `.claude/` - Agent definitions (`agents/`) and operational runbook skills (`skills/`)
- `wiki/` - Wiki templates and system
- `tools/` - CLI utilities
- `projects/` - Application workspaces that consume the wiki as a knowledge base
- `.gitignore` - Excludes data, keeps system

## Quick Setup

```bash
# Clone this template
git clone https://github.com/EraPartner/VaultLens.git my-wiki
cd my-wiki

# Initialize data directories (canonical set — see CLAUDE.md "Directory contract")
mkdir -p raw/sources raw/assets raw/inbox
mkdir -p wiki/sources wiki/entities wiki/concepts wiki/topics \
         wiki/comparisons wiki/syntheses wiki/queries wiki/reports wiki/inventory wiki/system

# Open in Obsidian
open .
```

## Projects Layer

The `projects/` directory is an application layer that sits on top of the wiki. Each subfolder is one project workspace that consumes the wiki as a knowledge base without ever writing to it.

```
vault/
├─ raw/                ← immutable ingested sources (source of truth)
├─ wiki/               ← curated knowledge base (generated from raw/)
├─ projects/<slug>/    ← project workspaces (application layer)
└─ CLAUDE.md           ← operating schema

# Sibling top-level layers, NOT nested. Dependency flows left→right:
# raw/ → wiki/ → projects/ (each consumes the one before it).
```

Each project has a `project.md` that declares its description, folder layout, rules, and linked wiki pages. The scaffold also drops a `CLAUDE.md` entrypoint so a session launched from inside the project picks up the project's context plus the conventions in the root `CLAUDE.md` (`## Working inside a project`).

### Scaffold a project

```bash
# Create a new project
python3 tools/wiki.py project new my-thesis

# Link relevant wiki pages into the project
python3 tools/wiki.py project link my-thesis concepts/trusted-execution

# List all projects
python3 tools/wiki.py project list

# Inspect a project's structure
python3 tools/wiki.py project show my-thesis
```

### Work inside a project

`cd` into `projects/<slug>/` and start Claude Code. The project's `CLAUDE.md` loads `project.md`, and the root schema loads automatically; the root `## Working inside a project` section defines the wiki search ladder, citation discipline, and Q&A artifact convention. Durable Q&A lands in `projects/<slug>/queries/` by default, redirectable via `## Rules` in `project.md`.

### project.md schema

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
projects/my-thesis/
  project.md     ← metadata, description, layout, rules, wiki refs
  CLAUDE.md      ← AI entrypoint: @project.md + operating principles (auto-generated)
  TODO.md        ← per-project todo; embedded into projects/TODO.md (auto-generated)
  queries/       ← Q&A artifacts
  papers/        ← relevant PDFs
  meetings/      ← dated meeting notes

## Rules
- Never modify source/ — treat it as read-only.
- Save all Q&A artifacts to queries/.
```

## See Also

- Original: <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>

