---
title: Setup Guide
type: page
status: active
created: 2026-04-11
updated: 2026-08-17
summary: How to run the Brain wiki with Obsidian, LockBox containers, Claude, Codex, and ChatGPT desktop projects.
---

# Setup Guide

Based on [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

## Prerequisites

- [Obsidian](https://obsidian.md) with plugins: Dataview, Templater
- Apple's `container` runtime with the system service started
- The LockBox-managed Brain image (Python 3.12 and qmd are baked in)
- An LLM provider login for Claude Code or OpenAI Codex

## Quick Setup

```bash
# Initialize directories for your data
mkdir -p raw/sources raw/assets raw/inbox

# Verify tools inside the Brain LockBox container
brain-wiki lint
```

## Obsidian Configuration

### Required Plugins

1. **Dataview** - Dynamic tables and queries from frontmatter
2. **Templater** - Auto-fills templates when creating new pages in wiki folders

### Recommended Plugins

- **Obsidian Git** - Auto-commit and sync
- **Web Clipper** - Clip articles to `raw/inbox/`

### Templater Setup

Templater is pre-configured to auto-apply templates when you create files in wiki subdirectories. Creating a new file in `wiki/sources/` auto-fills the source template.

### Graph View

Open graph view to see wiki structure. Color groups are pre-configured by page type (sources=blue, entities=green, concepts=purple, etc.).

## QMD Search (Optional)

For hybrid BM25 + vector search:

```bash
./tools/scripts/setup-qmd.sh
```

First run downloads a ~1.3GB embedding model. After setup:

```bash
qmd search "query"    # Keyword
qmd vsearch "query"   # Semantic
qmd query "query"     # Hybrid (best)
```

## ChatGPT desktop and Codex

Create one **local project** in the ChatGPT desktop app and attach this Brain
vault as its primary folder. Keep the Brain root primary so Codex automatically
discovers the root `AGENTS.md`, `.codex/config.toml`, skills, and the full
`wiki/` context.

Use a separate chat for each outcome or Brain project. When working on
`projects/<slug>/`, state that directory in the request or start the Codex CLI
there. Each project has its own `AGENTS.md`, which requires reading
`project.md` and preserves the project write boundary.

The desktop app's local-command sandbox cannot be replaced with the Apple
`container` runtime. Use the app for context, search, planning, and review.
Run authoritative wiki agents, mutations, tests, and scheduled work through the
LockBox entry points:

```bash
brain-wiki search --cli codex --task "..."
brain-cos --cli codex
.devcontainer/bin/codex
```

If a Brain project depends on an external repository, add it as a secondary
folder only when the chat needs direct access. The Brain root must remain
primary; Codex does not automatically discover `AGENTS.md`, skills, or
`config.toml` from secondary folders.

## Directory Structure

```
Second Brain/
├── AGENTS.md              # Operating schema
├── raw/                   # YOUR source material (immutable)
│   ├── sources/
│   ├── assets/
│   └── inbox/
├── wiki/                  # LLM-maintained knowledge base
│   ├── sources/
│   ├── entities/
│   ├── concepts/
│   ├── topics/
│   ├── syntheses/
│   ├── comparisons/
│   ├── queries/
│   ├── reports/
│   ├── inventory/         # tracked intentions (ingest-candidate/question/task/watch/...)
│   ├── system/
│   ├── _templates/
│   ├── index.md           # Dataview-powered catalog
│   └── log.md
└── tools/
    ├── wiki.py
    ├── wiki_extra.py
    ├── agents/
    └── scripts/
```

## Version Control

```bash
git init
git add .
git commit -m "Initial wiki setup"
```

The `.obsidian/` folder is tracked so plugin configs are preserved.
