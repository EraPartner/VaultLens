---
title: Home
type: page
status: active
created: 2026-04-11
updated: 2026-08-30
summary: Start page for wiki, project, review-queue, and scheduled-agent workflows.
---

# Welcome to Your LLM Wiki

This wiki implements the [LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) - a persistent, compounding knowledge base maintained by an LLM agent.

## Quick Start

1. **Add approved sources** - Drop files into `raw/inbox/` or `raw/sources/`
2. **Review possible sources** - Put interesting but unapproved material in `raw/review-inbox/`; agents must ask before opening or processing it
3. **Ingest** - Tell the agent to process a specific approved source
4. **Work from a project** - Use the project dashboard and project-specific agent context
5. **Check operations** - Review scheduled-agent status and run health checks

## Key Pages

- [[index|Catalog]] - All wiki pages
- [[log|Activity Log]] - Chronological history
- [[entities/user-background|Operator Profile]] ([Operator Profile](entities/user-background.md)) - Who this Brain serves; read first to calibrate tone and priorities
- [[system/schema|Schema]] ([Schema](system/schema.md)) - Operating conventions
- [[system/enhancement-strategies|Enhancement Strategies]] ([Enhancement Strategies](system/enhancement-strategies.md)) - Source-first vs topic-first coverage axis
- [[SETUP|Setup Guide]] ([Setup Guide](SETUP.md)) - Installation and configuration
- [Project Workboard](../projects/TODO.md) - Open tasks across active projects
- [Upcoming Deadlines](../projects/deadlines.md) - Dated project work
- [Scheduled-Agent Reports](reports/) - Chief of Staff briefs and background-agent outputs
- [Scheduler Specification](../tools/schedule/SPEC.md) - Cadence, gates, recovery, and monitoring

## Operational Status

```bash
python3 tools/wiki.py project list
python3 tools/wiki.py project agenda status
python3 tools/schedule/dispatch.py status
python3 tools/wiki.py lint --strict
qmd status
```

Project `AGENDA.md` files are dormant until explicitly enabled. Enabling one authorizes the nightly
project runner to apply due, fully specified tasks inside that project only. Review the generated
morning report and acknowledge accumulated edits as described in the project runbook.

Broad nightly wiki enhancement is paused by default. Manual enhancement remains available; opt in
to scheduled runs only with `VAULTLENS_SCHEDULE_ENHANCE=1` during scheduler installation.

`raw/review-inbox/` is separate from `raw/inbox/`. It is a human consent queue. Scheduled ingest
does not process it, and agents should expose only file names until you approve a specific item.

## Dashboard

### Recent Sources

```dataview
TABLE source_type AS "Type", origin AS "Origin", ingested_on AS "Ingested"
FROM "wiki/sources"
SORT ingested_on DESC
LIMIT 10
```

### Draft Pages (Needs Review)

```dataview
LIST summary
FROM "wiki"
WHERE status = "draft"
SORT file.mtime DESC
```

### Pages by Domain

```dataview
TABLE length(rows) AS "Pages"
FROM "wiki"
WHERE domain != null AND domain != ""
GROUP BY domain
SORT length(rows) DESC
```

### Stale Pages (Not Updated in 6+ Months)

```dataview
TABLE updated AS "Last Updated", type AS "Type"
FROM "wiki"
WHERE updated != null AND date(updated) < date(today) - dur(180 days) AND type != "page"
SORT updated ASC
```

### Wiki Stats

```dataview
TABLE length(rows) AS "Count"
FROM "wiki"
WHERE type != null
GROUP BY type
SORT length(rows) DESC
```

## The Pattern

**Core idea:** Instead of RAG (retrieving raw chunks at query time), the LLM **incrementally builds and maintains a persistent wiki** between you and the sources.

- **Raw sources** (`raw/`) - Immutable, never modified by the LLM
- **Wiki** (`wiki/`) - LLM-generated, incrementally updated
- **Projects** (`projects/`) - Workspaces that consume the wiki without writing back to it
- **Schema** (`AGENTS.md`) - Tells the LLM how to operate

The wiki is a **compounding artifact** - cross-references, contradictions, and synthesis are already done. Each source touches 10-15 pages. The knowledge is compiled once and kept current.

## PDF Support

Drop PDFs in `raw/sources/` or `raw/inbox/`, tell the agent to ingest. Agent extracts key claims and creates a source page.

## Multi-Topic Support

This is a **Second Brain** for any topic. Use `domain` frontmatter or subdirectories:

```yaml
domain: learning
tags: [machine-learning, course]
```

Query with Dataview: `WHERE domain = "learning"`

One vault, unlimited topics, all connected.

## Directory Structure

```
Second Brain/
├── AGENTS.md              # Operating schema (authoritative)
├── raw/                    # Immutable source material
│   ├── sources/           # Ingested documents
│   ├── assets/            # Images/attachments
│   ├── inbox/             # Approved pending ingestion
│   └── review-inbox/      # Interesting items; explicit approval required
├── projects/              # Project workspaces, TODOs, deadlines, and dormant agendas
├── wiki/                   # LLM-maintained knowledge base
│   ├── sources/           # Per-source summary pages
│   ├── entities/          # People, orgs, tools, places
│   ├── concepts/          # Methods, theories, ideas
│   ├── topics/            # Thematic syntheses
│   ├── syntheses/         # Cross-topic analyses
│   ├── comparisons/       # Side-by-side comparisons
│   ├── queries/           # Preserved Q&A artifacts
│   ├── reports/           # Lint/audit outputs
│   ├── system/            # Schema docs
│   ├── _templates/        # Templater page templates
│   ├── index.md           # Dataview-powered catalog
│   └── log.md             # Append-only activity log
└── tools/                  # Wiki maintenance utilities
    ├── wiki.py            # Core CLI (index, lint, search, log)
    ├── wiki_extra.py      # Extra utilities (qmd, stats, IDs)
    ├── agents/            # Agent definitions + wrapper
    ├── schedule/          # Host catch-up dispatcher for scheduled agents
    └── scripts/           # Setup scripts
```

## Commands

### Core Maintenance

```bash
python3 tools/wiki.py lint                 # Health check
python3 tools/wiki.py lint --strict        # Include orphan check
python3 tools/wiki.py search "term"        # Search wiki content
python3 tools/wiki.py validate-log         # Check log format
python3 tools/wiki.py append-log ...       # Add log entry
```

### Extra Utilities

```bash
python3 tools/wiki_extra.py next-id         # Generate next source ID
python3 tools/wiki_extra.py qmd-search "q"  # Search with QMD
python3 tools/wiki_extra.py stats           # Wiki statistics
```

### QMD Search (optional)

```bash
# Setup (one-time)
./tools/scripts/setup-qmd.sh

# Search modes
qmd search "query"         # BM25 (fast, keyword)
qmd vsearch "query"        # Vector (semantic)
qmd query "query"          # Hybrid (best quality)
qmd query "query" --format json  # For LLM context
```

## Workflows

### Ingest Source

1. Place source in `raw/inbox/` or `raw/sources/`
2. Tell agent: "Ingest [source file]"
3. Agent creates source page, updates entities/concepts/topics
4. Agent runs lint
5. Agent appends log entry

### Ask Question

1. Agent opens `index.md` for navigation (Dataview-powered)
2. Agent searches relevant pages
3. Agent synthesizes answer with citations
4. If durable, agent saves to `wiki/queries/`
5. Agent logs query to `log.md`

### Health Check

1. Run lint: `python3 tools/wiki.py lint --strict`
2. Review reports in `wiki/reports/`
3. Fix issues

## Graph View

Graph is color-coded by page type:
- **Blue** - Sources
- **Green** - Entities
- **Purple** - Concepts
- **Orange** - Topics
- **Magenta** - Syntheses
- **Yellow** - Comparisons
- **Cyan** - Queries
- **Gray** - Reports

## Why This Works

The tedious part is bookkeeping - updating cross-references, noting contradictions, maintaining consistency. Humans abandon wikis because maintenance burden grows faster than value. LLMs don't get bored and can touch 15 files in one pass.

**Human's job:** curate sources, direct analysis, ask good questions, think about meaning.
**LLM's job:** everything else - summarizing, cross-referencing, filing, bookkeeping.
