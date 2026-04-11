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
- `tools/agents/` - agent system prompts (source of truth)
- `.opencode/agents/` - opencode agent symlinks → `tools/agents/`

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

## Agent integration

For complex wiki tasks, use the project's custom agents stored in `tools/agents/`:

### When to use wiki agents

- **Quality review**: Use `wiki-quality-reviewer` for deep content analysis
- **Source verification**: Use `wiki-source-verifier` to verify claims against raw sources
- **Ingest**: Use `wiki-ingest` for processing new sources
- **Contradiction detection**: Use `wiki-contradiction-detector` to find conflicts
- **Search/Research**: Use `wiki-search` to find and synthesize information

### Custom wiki agents

This project defines specialized agents in `tools/agents/`:

- `wiki-quality-reviewer.agent.md` - Deep content quality analysis
- `wiki-source-verifier.agent.md` - Verify claims against raw sources
- `wiki-ingest.agent.md` - Process new sources through ingest workflow
- `wiki-contradiction-detector.agent.md` - Find contradictions across pages
- `wiki-search.agent.md` - Search and research wiki content

### opencode agent registration

Agents are registered with opencode via symlinks in `.opencode/agents/` (opencode's project-local agent directory):

```
.opencode/agents/
  wiki-ingest.md                → ../../tools/agents/wiki-ingest.agent.md
  wiki-quality-reviewer.md      → ../../tools/agents/wiki-quality-reviewer.agent.md
  wiki-source-verifier.md       → ../../tools/agents/wiki-source-verifier.agent.md
  wiki-contradiction-detector.md → ../../tools/agents/wiki-contradiction-detector.agent.md
  wiki-search.md                → ../../tools/agents/wiki-search.agent.md
```

Each agent file includes opencode-compatible YAML frontmatter (`description`, `mode`, `tools`).
The `wiki-agent.py` wrapper uses `--agent NAME` to invoke them by registered name.

**Claude** does not need project-local registration — it receives the agent instructions
via `--system-prompt` directly from the source files.

### Agent workflow examples

```bash
# Search the wiki
python3 tools/agents/wiki-agent.py search --page "machine learning"

# Quality review with opencode (default)
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md

# Run with Claude Sonnet, high effort
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md --cli claude --model sonnet --effort high

# Verify source claims
python3 tools/agents/wiki-agent.py verify --source wiki/sources/my-source.md --effort high

# Ingest new source
python3 tools/agents/wiki-agent.py ingest --source raw/sources/paper.pdf --cli ollama --model qwen3.5:9b

# Find contradictions
python3 tools/agents/wiki-agent.py contradict
```

### Available Agents

| Agent | File | Purpose |
|-------|------|---------|
| Quality | `wiki-quality-reviewer.agent.md` | Deep content analysis - claims, summaries, cross-references |
| Verify | `wiki-source-verifier.agent.md` | Verify wiki claims against raw source material |
| Ingest | `wiki-ingest.agent.md` | Process new sources through full ingest workflow |
| Contradict | `wiki-contradiction-detector.agent.md` | Find contradictions across pages |
| Search | `wiki-search.agent.md` | Search and synthesize wiki content |

**CLI options**: `opencode`, `claude`, `ollama`
**OpenCode models**: `opencode/minimax-m2.5-free` (default), `github-copilot/gpt-5.3-codex`
**Claude models**: `sonnet` (default), `haiku`, `opus`
**Ollama models**: `qwen3.5:4b` (default), `qwen3.5:9b`, `gemma4:e4b`
**Effort levels**: `low` (fast), `medium` (default), `high` (deep thinking)

## Canonical operations

### Ingest

When told to ingest source(s):

1. Use `python3 tools/agents/wiki-agent.py ingest --source raw/sources/FILE.pdf` to analyze
2. Extract key claims - make them falsifiable.
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
2. Use `python3 tools/agents/wiki-agent.py search --page "query"` to find relevant pages.
3. Identify and read relevant pages.
4. Synthesize with explicit citations in wiki-link form.
5. If answer is durable, save it as a page under `wiki/queries/` and link it.
6. Append query entry to `wiki/log.md` when requested or when preserving artifacts.

**Important:** Good answers should be filed back into the wiki as new pages. Comparisons, analyses, and connections discovered during research are valuable and shouldn't disappear into chat history.

### Lint / health check

Perform periodically or on request:

**Programmatic checks** (structural, fast):
- Run `python3 tools/wiki.py lint` for links, metadata, and staleness (>180 days).
- Run `python3 tools/wiki.py lint --strict` to also fail on orphan pages.
- Run `python3 tools/wiki.py validate-log` to check log entry format.

**Agent checks** (semantic, thorough):
- Use `python3 tools/agents/wiki-agent.py quality --page wiki/concepts/x.md` for deep quality review.
- Use `python3 tools/agents/wiki-agent.py contradict` for contradiction detection across pages.
- Use `python3 tools/agents/wiki-agent.py verify --source wiki/sources/x.md` to verify claims against raw sources.

Link suggestions are best done by agents (semantic understanding) rather than substring matching.

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

### Obsidian Plugins

**Installed and active:**
- **Dataview** - Dynamic tables and queries from frontmatter (JS enabled)
- **Templater** - Auto-fills templates when creating pages in wiki folders

**Recommended (optional):**
- **Web Clipper** - Clip web articles to raw/inbox/
- **Obsidian Git** - Auto-commit and sync

## Useful commands

```bash
# Core maintenance
python3 tools/wiki.py build-index          # Regenerate index
python3 tools/wiki.py lint                 # Health check (links, metadata, staleness)
python3 tools/wiki.py lint --strict        # Full check including orphans
python3 tools/wiki.py search "term"        # Search wiki content
python3 tools/wiki.py validate-log         # Check log entry format
python3 tools/wiki.py append-log ...       # Add log entry

# AI-powered agents (use wiki-agent.py)
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/x.md
python3 tools/agents/wiki-agent.py verify --source wiki/sources/x.md
python3 tools/agents/wiki-agent.py ingest --source raw/sources/x.pdf
python3 tools/agents/wiki-agent.py contradict

# Extra utilities
python3 tools/wiki_extra.py next-id         # Generate next source ID
python3 tools/wiki_extra.py qmd-search "q"  # QMD search
python3 tools/wiki_extra.py stats           # Wiki statistics
```

## Quality checks

### Agent-powered quality review

Use the wiki-agent.py wrapper to invoke AI agents:

```bash
# Quality review with opencode (default)
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md

# With Claude, high effort
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/x.md --cli claude --model sonnet --effort high

# Source verification
python3 tools/agents/wiki-agent.py verify --source wiki/sources/my-source.md

# Contradiction detection
python3 tools/agents/wiki-agent.py contradict

# Ingest new source
python3 tools/agents/wiki-agent.py ingest --source raw/sources/new-paper.pdf
```

The agents read `.agent.md` files as their system prompt and use configurable:
- **CLI**: `opencode`, `claude`, or `ollama`
- **Model**: varies by CLI (e.g., `sonnet`, `qwen3.5`, `minimax-free`)
- **Effort**: `low`, `medium`, `high` (thinking depth)

```bash
# Deep content quality analysis - checks claims, clarity, structure
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/your-concept.md --effort high

# Verify source claims against original sources
python3 tools/agents/wiki-agent.py verify --source wiki/sources/your-source.md --effort high
```

When to use agents:
- **Structural issues**: Templates, frontmatter, linking patterns
- **Content depth**: Are claims well-supported? Any gaps?
- **Cross-wiki consistency**: Contradictions that automated tools miss
- **Summary quality**: Does summary match content accurately?

## PDF and image handling

### PDF parsing

**Option 1: Direct LLM reading (recommended for this wiki)**
- Place PDF in `raw/sources/` or `raw/inbox/`
- The LLM reads PDF directly via the Read tool
- Extract key claims and create source page

**Option 2: Convert to markdown first**
For complex PDFs with tables/figures, convert before ingestion:

```bash
# Using pandoc
pandoc -t markdown input.pdf -o output.md

# Using Obsidian PDF plugins
# - PDF Highlights: Extract annotations and highlights
# - Readwise: Sync highlights to markdown
```

**Option 3: Specialized extraction**
For academic papers:
- SciPDF (arxiv papers)
- GPTPDF for OCR-heavy documents

### Image handling

- Store images in `raw/assets/`
- Use relative paths in wiki: `![image](../../raw/assets/image.png)`
- Add to obsidian config for image folder if needed

## Obsidian integrations

### Templater

Templater is configured with folder templates — creating a file in any wiki subdirectory auto-applies the matching template from `wiki/_templates/`.

| Folder | Template Applied |
|--------|-----------------|
| `wiki/sources/` | `source.md` — prompts for source_type |
| `wiki/entities/` | `entity.md` — prompts for entity_type |
| `wiki/concepts/` | `concept.md` |
| `wiki/topics/` | `topic.md` |
| `wiki/syntheses/` | `synthesis.md` |
| `wiki/comparisons/` | `comparison.md` |
| `wiki/queries/` | `query.md` |
| `wiki/reports/` | `report.md` — prompts for report_type |

All templates auto-fill: title from filename, today's date, cursor placement, and Dataview backlink queries.

### Dataview

Every template includes Dataview queries for automatic backlink/relationship tables. Key patterns:

```
# Show all pages linking to current page
FROM "wiki" WHERE contains(file.outlinks, this.file.link)

# Show sources by domain
FROM "wiki/sources" WHERE domain = "research"

# Show draft pages needing review
FROM "wiki" WHERE status = "draft"

# Show stale pages
FROM "wiki" WHERE date(updated) < date(today) - dur(180 days)
```

The home page has a live dashboard with recent sources, drafts, domain breakdown, and staleness alerts.

### Graph View

Graph is color-coded by page type:
- **Blue** — Sources | **Green** — Entities | **Purple** — Concepts
- **Orange** — Topics | **Magenta** — Syntheses | **Yellow** — Comparisons
- **Cyan** — Queries | **Gray** — Reports

Orphan pages are visible. Filter is set to `path:wiki`.

## Why this works

The tedious part of maintaining a knowledge base is the bookkeeping - updating cross-references, keeping summaries current, noting contradictions, maintaining consistency. Humans abandon wikis because maintenance burden grows faster than value. LLMs don't get bored, don't forget updates, and can touch 15 files in one pass.

The human's job: curate sources, direct analysis, ask good questions, think about meaning.
The LLM's job: everything else - summarizing, cross-referencing, filing, bookkeeping.
