---
title: Home
type: page
status: active
created: 2026-04-11
updated: 2026-04-11
summary: Welcome to your LLM wiki - start here for workflows and conventions.
---

# Welcome to Your LLM Wiki

This wiki implements the [LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) - a persistent, compounding knowledge base maintained by an LLM agent.

## Quick Start

1. **Add sources** - Drop files into `raw/inbox/` or `raw/sources/`
2. **Ingest** - Tell the agent to process a source
3. **Query** - Ask questions against the wiki
4. **Lint** - Run health checks periodically

## Key Pages

- [[index|Catalog]] - All wiki pages
- [[log|Activity Log]] - Chronological history
- [[system/schema|Schema]] - Operating conventions

## The Pattern

**Core idea:** Instead of RAG (retrieving raw chunks at query time), the LLM **incrementally builds and maintains a persistent wiki** between you and the sources.

- **Raw sources** (`raw/`) - Immutable, never modified by the LLM
- **Wiki** (`wiki/`) - LLM-generated, incrementally updated
- **Schema** (`AGENTS.md`) - Tells the LLM how to operate

The wiki is a **compounding artifact** - cross-references, contradictions, and synthesis are already done. Each source touches 10-15 pages. The knowledge is compiled once and kept current.

## PDF Support

Yes! PDFs are fully supported:

1. Drop PDF in `raw/sources/` or `raw/inbox/`
2. Tell agent to ingest
3. Agent extracts key claims and creates source page

## Multi-Topic Support

Yes! This is designed as a **Second Brain** for any topic:

**Subdirectories** (recommended for distinct domains):
```
raw/sources/course-materials/  # Course notes
raw/sources/research/         # Research papers
raw/sources/personal/         # Personal info
```

**Or use frontmatter domains:**
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
│   └── inbox/             # Pending ingestion
├── wiki/                   # LLM-maintained knowledge base
│   ├── sources/           # Per-source summary pages
│   ├── entities/         # People, orgs, tools, places
│   ├── concepts/        # Methods, theories, ideas
│   ├── topics/          # Thematic syntheses
│   ├── syntheses/      # Cross-topic analyses
│   ├── comparisons/   # Side-by-side comparisons
│   ├── queries/       # Preserved Q&A artifacts
│   ├── reports/       # Lint/audit outputs
│   ├── system/        # Schema docs
│   ├── _templates/   # Page templates
│   ├── index.md     # Generated catalog
│   └── log.md      # Append-only activity log
└── tools/                    # Wiki maintenance utilities
    ├── wiki.py              # Core CLI (index, lint, search, log)
    ├── wiki_extra.py         # Extra utilities
    └── scripts/            # Setup scripts
```

## Commands

### Core Maintenance

```bash
python3 tools/wiki.py build-index          # Regenerate catalog
python3 tools/wiki.py lint              # Health check
python3 tools/wiki.py lint --strict      # Include orphan check
python3 tools/wiki.py search "term"      # Search wiki content
python3 tools/wiki.py append-log ...   # Add log entry
```

### Extra Utilities

```bash
python3 tools/wiki_extra.py next-id        # Generate next source ID
python3 tools/wiki_extra.py qmd-search "q"  # Search with QMD
python3 tools/wiki_extra.py suggest-links    # Link suggestions
python3 tools/wiki_extra.py orphans        # Find orphan pages
python3 tools/wiki_extra.py stats         # Wiki statistics
```

### QMD Search (optional)

```bash
# Setup (one-time)
./tools/scripts/setup-qmd.sh

# Search modes
qmd search "query"         # BM25 (fast, keyword)
qmd vsearch "query"        # Vector (semantic)
qmd query "query"         # Hybrid (best quality)
qmd query "query" --json  # For LLM context
```

## Workflows

### Ingest Source

1. Place source in `raw/inbox/` or `raw/sources/`
2. Tell agent: "Ingest [source file]"
3. Agent creates source page, updates entities/concepts/topics
4. Agent runs index build and lint
5. Agent appends log entry

### Ask Question

1. Agent reads `index.md` for navigation
2. Agent searches relevant pages
3. Agent synthesizes answer with citations
4. If durable, agent saves to `wiki/queries/`
5. Agent logs query to `log.md`

### Health Check

1. Run lint: `python3 tools/wiki.py lint --strict`
2. Review reports in `wiki/reports/`
3. Fix issues
4. Rebuild index

## Tips

- **Obsidian Web Clipper** - Clip web articles as markdown
- **Local images** - Configure Obsidian to save to `raw/assets/`
- **Graph view** - See wiki structure and connections
- **Dataview** - Query frontmatter for dynamic tables
- **Marp** - Generate slide decks from markdown

## Why This Works

The tedious part is bookkeeping - updating cross-references, noting contradictions, maintaining consistency. Humans abandon wikis because maintenance burden grows faster than value. LLMs don't get bored and can touch 15 files in one pass.

**Human's job:** curate sources, direct analysis, ask good questions, think about meaning.
**LLM's job:** everything else - summarizing, cross-referencing, filing, bookkeeping.