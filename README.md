# Second Brain - System Template

## What's Included

- `AGENTS.md` - Operating schema for LLM agents
- `wiki/` - Wiki templates and system
- `tools/` - CLI utilities
- `projects/` - Application workspaces that consume the wiki as a knowledge base
- `.gitignore` - Excludes data, keeps system

## Quick Setup

```bash
# Clone this template
git clone https://github.com/EraPartner/VaultLens.git my-wiki
cd my-wiki

# Initialize data directories
mkdir -p raw/sources raw/assets raw/inbox
mkdir -p wiki/sources wiki/entities wiki/concepts wiki/topics

# Open in Obsidian
open .
```

## Projects Layer

The `projects/` directory is an application layer that sits on top of the wiki. Each subfolder is one project workspace that consumes the wiki as a knowledge base without ever writing to it.

```
raw/          ← immutable ingested sources
  └─ wiki/    ← curated knowledge base
       └─ projects/<slug>/   ← project workspaces (application layer)
```

Each project has a `project.md` that declares its description, folder layout, rules, and linked wiki pages. The scaffold also drops an `AGENTS.md` (with `CLAUDE.md` and `opencode.json` shims) so any AI CLI launched from inside the project picks up the project's context plus the conventions in the root `AGENTS.md` (`## Working inside a project`).

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

`cd` into `projects/<slug>/` and start your AI CLI of choice (Claude Code, opencode, Copilot CLI). The project's `AGENTS.md` instructs the agent to read `project.md` and the root schema; the root `## Working inside a project` section defines the wiki search ladder, citation discipline, and Q&A artifact convention. Durable Q&A lands in `projects/<slug>/queries/` by default, redirectable via `## Rules` in `project.md`.

### project.md schema

```yaml
---
type: project
title: My Thesis
status: active
tags: [tee, sgx]
domain: security
wiki_refs:
  - concepts/trusted-execution-environments
  - topics/remote-attestation
---

## Description
...

## Layout
projects/my-thesis/
  project.md     ← metadata, description, layout, rules, wiki refs
  context.md     ← model-agnostic AI entrypoint (auto-generated)
  CLAUDE.md      ← Claude Code shim: @context.md (auto-generated)
  queries/       ← Q&A artifacts
  papers/        ← relevant PDFs
  meetings/      ← dated meeting notes

## Rules
- Never modify source/ — treat it as read-only.
- Save all Q&A artifacts to queries/.
```

## See Also

- Original: <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>

