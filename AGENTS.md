# LLM Wiki Operating Schema

This vault implements the "LLM Wiki" pattern as a persistent, compounding knowledge base.
Based on [Karpathy's llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

## Purpose

Maintain a durable wiki in `wiki/` from immutable source material in `raw/`.

- Raw data in `raw/` is source of truth and should not be modified in-place by normal ingest flows.
- Wiki pages in `wiki/` are maintained by the agent and can be updated incrementally.
- `wiki/index.md` and `wiki/log.md` are mandatory navigation files.

## Architecture

**Three layers:**

1. **Raw sources** (`raw/`) - Immutable source documents. The source of truth.
2. **The wiki** (`wiki/`) - LLM-generated markdown files. The agent owns this layer.
3. **The schema** (`AGENTS.md`) - This file. Tells the LLM how to operate.

## Directory contract

- `raw/sources/` - immutable source docs
- `raw/assets/` - local images/attachments referenced by sources
- `raw/inbox/` - newly added files awaiting ingest
- `wiki/` - maintained markdown knowledge base
- `wiki/system/` - schema and operating docs
- `wiki/sources/` - one page per ingested source
- `wiki/entities/` - person, org, tool, place, artifact pages
- `wiki/concepts/` - concept or method pages
- `wiki/topics/` - broad thematic pages synthesizing many concepts
- `wiki/syntheses/` - cross-topic analyses and theses
- `wiki/comparisons/` - explicit side-by-side analyses
- `wiki/queries/` - high-value Q&A artifacts worth preserving
- `wiki/reports/` - lint outputs, audits, and curation reports
- `wiki/_templates/` - templates for standardized pages
- `tools/wiki.py` - core maintenance utility
- `tools/wiki_extra.py` - additional utilities
- `tools/scripts/` - setup and helper scripts

## Required page metadata

All content pages should include YAML frontmatter with at least:

- `title`
- `type` (page, source, entity, concept, topic, synthesis, comparison, query)
- `status` (`active`, `superseded`, `archived`, `draft`)
- `created` (`YYYY-MM-DD`)
- `updated` (`YYYY-MM-DD`)
- `summary` (one sentence, falsifiable where possible)

**Optional fields for all pages:**

- `domain` - Broad category: `personal`, `research`, `work`, `learning`, etc.
- `tags` - List of topic tags for cross-referencing

Additional required fields for `wiki/sources/*`:

- `source_id` (stable id, e.g. `src-2026-04-11-001`)
- `source_type` (`article`, `paper`, `book`, `pdf`, `video`, `podcast`, `dataset`, `note`, `other`)
- `origin` (URL, publication, or provenance)
- `ingested_on` (`YYYY-MM-DD`)

### PDF Support

PDFs are fully supported as raw sources:

1. Place PDF in `raw/sources/` or `raw/inbox/`
2. The LLM can read PDFs directly
3. Creates source page with extracted key claims
4. Reference stored in `raw/assets/` if needed

For complex PDFs, consider converting to markdown first:
- Obsidian PDF import plugins
- `pandoc -t markdown input.pdf -o output.md`
- Online converters

### Multi-Topic / Multi-Domain Support

The wiki supports unlimited separate topics using **domains** and **subdirectories**:

**Option 1: Subdirectories (recommended for distinct domains)**

```
raw/sources/
  ├── course-materials/    # Learning materials
  ├── research/           # Research papers
  └── personal/           # Personal knowledge

wiki/entities/
  ├── course-materials/
  ├── research/
  └── personal/

wiki/concepts/
  ├── course-materials/
  ├── research/
  └── personal/
```

**Option 2: Frontmatter domains**

Add to page frontmatter:

```yaml
domain: learning
tags: [machine-learning, course]
```

Use Dataview to query by domain:
```
```dataview
TABLE domain, tags
FROM "wiki/concepts"
WHERE domain = "learning"
```
```

This makes the wiki a true **Second Brain** - one vault, unlimited topics, all connected.

## Symlinks

The wiki supports symlinks for existing data - no need to duplicate files:

```bash
# Link entire directories
ln -s ~/Documents/work-archive raw/sources/work
ln -s ~/Documents/personal-notes raw/sources/personal

# Link individual files
ln -s /path/to/existing/file.pdf raw/sources/my-file.pdf
```

The LLM reads from `raw/` - symlinks are followed automatically. The wiki tools work with symlinks without modification.

## Canonical operations

### Ingest

When told to ingest source(s):

1. Read source material from `raw/sources/` (or move file from `raw/inbox/` if requested).
2. Analyze and extract key claims - make them falsifiable.
3. Create/update a source page under `wiki/sources/`.
4. Update relevant pages in `wiki/entities/`, `wiki/concepts/`, `wiki/topics/`, and/or `wiki/syntheses/`.
5. Add wikilinks to connect new content with existing wiki.
6. Add contradiction notes where claims conflict with existing knowledge.
7. Run `python3 tools/wiki.py build-index`.
8. Run `python3 tools/wiki.py lint` and fix issues.
9. Append to log using `python3 tools/wiki.py append-log ...`.

### Query

When answering a user question using this wiki:

1. Read `wiki/index.md` first to understand current state.
2. Use `python3 tools/wiki.py search` or `qmd query` for relevant pages.
3. Identify and read relevant pages.
4. Synthesize with explicit citations in wiki-link form.
5. If answer is durable, save it as a page under `wiki/queries/` and link it.
6. Append query entry to `wiki/log.md` when requested or when preserving artifacts.

**Important:** Good answers should be filed back into the wiki as new pages. Comparisons, analyses, and connections discovered during research are valuable and shouldn't disappear into chat history.

### Lint / health check

Perform periodically or on request:

- Run `python3 tools/wiki.py lint` for basic checks.
- Run `python3 tools/wiki.py lint --strict` for full checks (includes orphans).
- Check for: contradictions, stale claims, sparse pages, orphan pages, missing cross-references.
- Use `python3 tools/wiki_extra.py suggest-links <page>` to find link opportunities.
- Write findings to `wiki/reports/`.
- Update wiki pages to resolve highest-priority issues.

## Link and citation conventions

- Use Obsidian wikilinks (`[[path/to/page]]`) for internal references.
- Prefer explicit path-based links when ambiguity is possible.
- For source citations, include a `## Sources` section listing `[[sources/...]]` pages.
- Keep external URLs on source pages; reference sources indirectly from concept/topic pages.

## Change quality rules

- Preserve existing validated content unless superseded by stronger evidence.
- Mark superseded claims with status: superseded; do not silently delete historical context.
- Keep summaries concise and falsifiable where possible.
- Favor incremental edits across related pages instead of creating isolated notes.
- When updating pages, update the `updated` frontmatter field.

## Index and log policy

- `wiki/index.md` is generated and should be rebuilt after every ingest-level change.
- `wiki/log.md` is append-only chronological history.
- Log headings use: `## [YYYY-MM-DD] operation | title`.

## Optional tools

### QMD Search Engine

For larger wikis, install qmd for hybrid BM25 + vector search with LLM re-ranking:

```bash
# Install
npm install -g @tobilu/qmd

# Setup (run from wiki root)
./tools/scripts/setup-qmd.sh

# Search
qmd search "query"        # BM25
qmd vsearch "query"       # Vector
qmd query "query"         # Hybrid (best)
qmd query "query" --json  # For LLM context
```

### Obsidian Plugins (optional)

- **Dataview** - Query frontmatter for dynamic tables
- **Marp** - Generate slide decks from markdown
- **Web Clipper** - Clip web articles to raw/inbox/

## Useful commands

```bash
# Core maintenance
python3 tools/wiki.py build-index          # Regenerate index
python3 tools/wiki.py lint                  # Health check
python3 tools/wiki.py lint --strict         # Full check with orphans
python3 tools/wiki.py search "term"        # Search wiki content
python3 tools/wiki.py append-log ...       # Add log entry

# Extra utilities
python3 tools/wiki_extra.py next-id        # Generate next source ID
python3 tools/wiki_extra.py qmd-search "q" # QMD search
python3 tools/wiki_extra.py suggest-links # Link suggestions
python3 tools/wiki_extra.py orphans        # Find orphan pages
python3 tools/wiki_extra.py stats          # Wiki statistics
```

## Why this works

The tedious part of maintaining a knowledge base is the bookkeeping - updating cross-references, keeping summaries current, noting contradictions, maintaining consistency. Humans abandon wikis because maintenance burden grows faster than value. LLMs don't get bored, don't forget updates, and can touch 15 files in one pass.

The human's job: curate sources, direct analysis, ask good questions, think about meaning.
The LLM's job: everything else - summarizing, cross-referencing, filing, bookkeeping.
