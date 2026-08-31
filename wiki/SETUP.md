---
title: Setup Guide
type: page
status: active
created: 2026-04-11
updated: 2026-08-30
summary: How to set up the public wiki template, projects, search index, and scheduled agents safely.
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
mkdir -p raw/sources raw/assets raw/inbox raw/review-inbox

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
qmd status             # Collection and index health
```

The setup script configures the `raw` collection to ignore `review-inbox/**` before indexing.

The Brain container receives the host qmd index as a read-only seed. Once per container boot it
copies a changed, consistent snapshot into that capability profile's writable cache. If the host
seed has an active SQLite write-ahead log, the refresh is skipped and the previous good container
snapshot is kept. Replayed launch commands do not replace a cache under a live qmd process.

## Source Approval Queues

- `raw/inbox/` contains approved material awaiting ingest.
- `raw/review-inbox/` contains material that is merely of interest. Agents may list file names and
  sizes so you know a decision is waiting, but they must ask before reading, summarizing, moving, or
  ingesting an item. Scheduled ingest never consumes this directory.

## Project Workspaces

Projects consume the wiki without writing back to `wiki/` or `raw/`. Use the generated workboard
and deadlines pages for daily navigation:

- [Project Workboard](../projects/TODO.md)
- [Upcoming Deadlines](../projects/deadlines.md)

```bash
python3 tools/wiki.py project list
python3 tools/wiki.py project show <slug>
python3 tools/wiki.py project agenda status
python3 tools/wiki.py project agenda enable <slug>   # explicit nightly-runner opt-in
```

Every `AGENDA.md` is disabled by default. Only enable a project when its task scope and acceptance
criteria are ready for unattended edits. The runner writes only inside that project and creates a
pre-run snapshot for recovery.

## Scheduled Agents

The optional host catch-up dispatcher runs maintenance, read-only thinking agents, opted-in project
work, and the morning Chief of Staff brief. Broad nightly wiki enhancement is paused by default.
Install or refresh the dispatcher only from the host:

```bash
tools/schedule/install.sh
python3 tools/schedule/dispatch.py status
```

Opt in to five nightly enhancement iterations across the whole wiki:

```bash
VAULTLENS_SCHEDULE_ENHANCE=1 tools/schedule/install.sh
```

The status view shows each job's last run, next due time, result, and cooldown. Generated outputs
are available under [Scheduled-Agent Reports](reports/). Detailed design and recovery commands are
in the [scheduler specification](../tools/schedule/SPEC.md). Scheduled ingest checks
`raw/inbox/` and `raw/sources/`; it never checks `raw/review-inbox/`.

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
│   ├── inbox/             # approved pending ingestion
│   └── review-inbox/      # explicit approval required
├── projects/              # project workspaces, TODOs, deadlines, and agendas
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
    ├── schedule/
    └── scripts/
```

## Version Control

```bash
git init
git add .
git commit -m "Initial wiki setup"
```

The `.obsidian/` folder is tracked so plugin configs are preserved.
