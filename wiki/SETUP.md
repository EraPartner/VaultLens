---
title: Setup Guide
type: page
status: active
created: 2026-04-11
updated: 2026-04-11
summary: How to set up and configure your LLM Wiki Second Brain.
---

# Setup Guide

Based on [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

## Prerequisites

- [Obsidian](https://obsidian.md) with plugins: Dataview, Templater
- Python 3.10+
- An LLM CLI: `claude`, `opencode`, or `ollama`

## Quick Setup

```bash
# Initialize directories for your data
mkdir -p raw/sources raw/assets raw/inbox

# Verify tools work
python3 tools/wiki.py lint
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
