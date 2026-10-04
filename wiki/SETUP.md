---
title: Setup Guide
type: page
status: active
created: 2026-04-11
updated: 2026-10-03
summary: How to set up the public wiki template, projects, search index, and scheduled agents safely.
---

# Setup Guide

Based on [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

## Prerequisites

- [Obsidian](https://obsidian.md) with plugins: Dataview, Templater
- Python 3.11 or newer; use a compatible Homebrew Python when the system interpreter is older
- Node.js 22.12 or newer and the pinned standalone sandbox runtime installed by `tools/runtime/install.sh`
- macOS Seatbelt, or bubblewrap, socat, and ripgrep on Linux
- An installed native Claude Code or OpenAI Codex CLI; authenticate only after the isolation checks pass

The runtime uses named access profiles for read selection, note writes, reports, and network access.
Provider login stores live outside the vault and iCloud sync. Host and legacy login state is never
imported automatically. See the [runtime guide](../tools/runtime/README.md) for the boundary and
fresh authentication procedure.

## Quick Setup

Use a supported Python executable for these host commands. Set `BRAIN_PYTHON` to its absolute
path when using the Fish wrappers or installing the scheduler.

```bash
# Deterministic operator commands; no model or provider login is needed.
python3 tools/wiki.py init
python3 tools/wiki.py lint

# Install the pinned runtime and inspect an explicit reader selection.
bash tools/runtime/install.sh
python3 tools/local_runtime.py profiles
python3 tools/local_runtime.py plan --profile selected-read --read-path wiki/system/schema.md

# Test synthetic fixtures, then check launch readiness.
python3 tools/runtime/probe.py
python3 tools/local_runtime.py doctor
```

All agent and scoped shell launches require a current successful synthetic probe receipt.
Any failed or skipped required check leaves them disabled. A new probe revokes the older receipt;
changes to launcher sources, access policy, runtime, dependencies, or operating system require a
fresh probe. Package installation and portable tests do not prove isolation. Authenticate through
the scoped runtime only after these gates pass; see the [runtime guide](../tools/runtime/README.md).

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

For explicit full-vault hybrid search outside scoped agent runs:

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

Native agent runs start a fresh lexical search server inside the same whole-process boundary.
Its corpus includes only files approved by that run's access profile. The qmd-compatible CLI and
Model Context Protocol (MCP) tools use this scoped corpus; they never copy or open the host's
shared qmd index or cache. In an agent run, `qmd query` is a lexical compatibility command.
Full-vault hybrid search remains the separate, explicit operator workflow above.

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

Keep the personal Brain in local sessions. For ordinary note analysis, launch a native Brain
wrapper with `selected-read` and explicit approved paths. The wrapper loads vault instructions
and applies that run's access profile.

A desktop app chat has its own permission settings; the native launcher's profile does not change
those settings. Attach the complete Brain root to a local desktop chat only when you intend to
make that workspace available. Selected material used by Claude or Codex is still sent to its
configured model provider. Brain must never be copied into a hosted cloud session.

Use a separate chat for each outcome or Brain project. When working on
`projects/<slug>/`, state that directory in the request or start the Codex CLI
there. Each project has its own `AGENTS.md`, which requires reading
`project.md` and preserves the project write boundary.

Interactive, headless, and scheduled Brain agents use native clients through the whole-process
runtime. Start a reporting run or interactive session with a narrow reader profile:

```bash
brain-wiki search --cli codex --access-profile selected-read --read-path wiki/system/schema.md --task "Summarize the selected schema note."
brain-codex --access-profile selected-read --read-path wiki/system/schema.md

# Inspect the broader brief scope before a cross-project run.
python3 tools/local_runtime.py plan --profile cos-read
brain-cos --cli codex
```

Use `brain-claude` for the native Claude client, or `brain-agent` for the selected provider.
Requested wiki editing uses `--access-profile wiki-write`; project editing uses
`--access-profile project-write --project <slug>`. Sources, the consent queue, tools, instructions,
Obsidian configuration, and Git metadata remain protected from writes. Writer runs retain
recoverable snapshots; review their changes before accepting them. Deterministic `wiki.py`
commands and tooling tests remain explicit host operations independent of provider login.

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
│   ├── sources-text/
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
