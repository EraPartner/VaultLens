# LLM Wiki Operating Schema

This vault implements the "LLM Wiki" pattern as a persistent, compounding knowledge base.
Based on [Karpathy's llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

## Purpose

Maintain a durable wiki in `wiki/` from immutable source material in `raw/`.

- Raw data in `raw/` is source of truth and should not be modified in-place by normal ingest flows.
- Wiki pages in `wiki/` are maintained by the agent and can be updated incrementally.
- `wiki/index.md` (Dataview-powered catalog) and `wiki/log.md` are mandatory navigation files.

## Architecture

**Four layers:**

1. **Raw sources** (`raw/`) - Immutable source documents. The source of truth.
2. **The wiki** (`wiki/`) - LLM-generated markdown files. The agent owns this layer.
3. **Projects** (`projects/`) - Application workspaces that consume the wiki as a knowledge base. Each project has its own context, notes, and preserved Q&A. The `project-assistant` agent answers questions in a project's context, citing wiki pages. Projects may reference wiki pages but never write to `wiki/`.
4. **The schema** (`AGENTS.md`) - This file. Tells the LLM how to operate.

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
- `projects/<slug>/` - one folder per application project (see Projects layer below)
- `projects/<slug>/project.md` - project metadata page (frontmatter + description + linked wiki pages)
- `projects/<slug>/notes/` - project-local scratch notes
- `projects/<slug>/queries/` - durable Q&A artifacts produced by `project-assistant`
- `tools/wiki.py` - core maintenance utility
- `tools/wiki_extra.py` - additional utilities
- `tools/scripts/` - setup and helper scripts
- `tools/agents/` - agent system prompts (source of truth)
- `.opencode/agents/` - opencode agent symlinks → `tools/agents/`

## Required page metadata

All content pages should include YAML frontmatter with at least:

- `title`
- `type` (page, source, entity, concept, topic, synthesis, comparison, query, project)
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

Additional fields for `projects/<slug>/project.md`:

- `wiki_refs` - List of `[concepts/foo, topics/bar, ...]` wikilinks the project depends on
- `tags` and `domain` are first-class for projects (used by `project-assistant` to scope wiki search)

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

## Projects layer

`projects/` is the application layer that consumes the wiki as a knowledge base. Each subfolder is one project workspace.

**Each project owns its own folder structure.** The scaffold creates only `project.md` and a default `queries/` directory; the user defines whatever else the project needs (`papers/`, `meetings/`, `repos/`, `drafts/`, etc.). The project's layout and rules are documented inside `project.md` itself, and the `project-assistant` agent reads them before answering.

### Minimum scaffolded structure

```
projects/<slug>/
  project.md          ← metadata + description + Layout + Rules + linked wiki pages
  queries/            ← default Q&A artifact landing zone (overridable in ## Rules)
```

### Common bespoke additions (user-created)

```
projects/<slug>/
  papers/             ← relevant academic papers (PDFs + extracted notes)
  meetings/           ← dated meeting notes and annotations
  repos/              ← read-only references to external code
  drafts/             ← writing in progress
  data/               ← experimental data, plots
  ...
```

### Project page schema

```yaml
---
title: <Human Title>
type: project
status: active            # active | paused | archived
created: YYYY-MM-DD
updated: YYYY-MM-DD
summary: <one-sentence description>
domain: personal          # or research / work / learning
tags: [<scope tags>]
wiki_refs: [<concepts/foo, topics/bar, sources/src-...>]
---

# <Title>

## Description
## Layout                 ← describe what each subfolder contains
## Rules                  ← project-specific rules the agent MUST follow
## Key questions
## Context
## Linked wiki pages
```

The `wiki_refs` and `tags` fields are load-bearing: `project-assistant` uses them to scope which wiki pages it pulls into context. Use `python3 tools/wiki.py project link <slug> <wiki-ref>` to add a reference (it preserves frontmatter formatting and updates `updated`).

### `## Layout` section

Document the actual folder structure of this project. The agent reads this BEFORE doing anything else, so it knows where to look. Example for a thesis project:

```markdown
## Layout

- `papers/` — academic papers relevant to the thesis (PDFs + notes/<paper>.md per file)
- `repos/` — read-only references to forked or surveyed codebases
- `meetings/` — dated meeting notes (e.g. `meetings/2026-04-12-supervisor.md`)
- `drafts/` — chapter drafts in progress
- `queries/` — durable Q&A artifacts (default)
```

If `## Layout` is missing, the agent falls back to filesystem discovery via `ls` and `find`.

### `## Rules` section

Free-form, project-specific rules the `project-assistant` agent MUST follow. **Project rules override the agent's defaults** when they conflict.

```markdown
## Rules

- Save query artifacts under `meetings/qa/` instead of the default `queries/`.
- Treat `repos/` as read-only — never write inside it.
- Cite the source PDF filename whenever referencing a paper from `papers/`.
- Never summarize meeting notes in `meetings/` without asking first.
- For design questions, prefer concepts in `wiki_refs` over general wiki search.
```

### Boundary rules

- Projects MAY reference any wiki page via wikilinks. Lint validates `wiki_refs` against the canonical wiki page set.
- Projects MUST NOT modify `wiki/` or `raw/`. The `project-assistant` agent's write surface is restricted to `projects/<slug>/`.
- If a project finds wiki coverage lacking, the agent recommends a `wiki-enhancer` follow-up rather than editing the wiki itself.
- `lint` checks projects: required frontmatter fields and broken `wiki_refs`. Project body content (Layout, Rules) is intentionally free-form and not validated.

### CLI

```bash
python3 tools/wiki.py project list                                       # list all projects
python3 tools/wiki.py project new <slug>                                 # scaffold a new project
python3 tools/wiki.py project show <slug>                                # print details (use --json for machine output)
python3 tools/wiki.py project link <slug> concepts/some-page             # append wiki ref + bump updated
```

### Agent invocation

```bash
python3 tools/agents/wiki-agent.py project --project <slug> --question "<question>"
```

The wrapper attaches `projects/<slug>/project.md` to the agent so the agent reads the project's context, then expects the agent to follow `project-assistant.agent.md` for the rest.

## Agent integration

For complex wiki tasks, use the project's custom agents stored in `tools/agents/`:

### When to use wiki agents

- **Ingest**: Use `wiki-ingest` for first-pass intake of a brand-new source.
- **Enhance**: Use `wiki-enhancer` to iteratively deepen, fix, and interlink already-ingested content — also covers iterative-loop mode ("next stub", "random page", "keep going on the wiki") rewriting pages toward the canonical structure.
- **Quality review**: Use `wiki-quality-reviewer` for intrinsic page-level audits (read-only).
- **Source verification**: Use `wiki-source-verifier` to verify wiki claims against the raw source.
- **Contradiction detection**: Use `wiki-contradiction-detector` to surface intra-wiki conflicts.
- **Search/Research**: Use `wiki-search` to answer questions with cited synthesis.
- **Project Q&A**: Use `project-assistant` to answer questions in the context of a specific project under `projects/<slug>/`, citing wiki pages and saving durable Q&A artifacts.

### Custom wiki agents

This project defines specialized agents in `tools/agents/`:

- `wiki-ingest.agent.md` - First-pass intake of a brand-new source
- `wiki-enhancer.agent.md` - Iterative improvement of already-ingested pages, including loop-mode rewrites toward canonical structure
- `wiki-quality-reviewer.agent.md` - Intrinsic page-level audit (read-only)
- `wiki-source-verifier.agent.md` - Verify a single page's claims against the raw source
- `wiki-contradiction-detector.agent.md` - Find conflicts across pages
- `wiki-search.agent.md` - Answer questions with cited synthesis
- `project-assistant.agent.md` - Project-scoped reasoning grounded in the wiki KB; writes only inside `projects/<slug>/`

### opencode agent registration

Agents are registered with opencode via symlinks in `.opencode/agents/` (opencode's project-local agent directory):

```
.opencode/agents/
  wiki-ingest.md                 → ../../tools/agents/wiki-ingest.agent.md
  wiki-enhancer.md               → ../../tools/agents/wiki-enhancer.agent.md
  wiki-quality-reviewer.md       → ../../tools/agents/wiki-quality-reviewer.agent.md
  wiki-source-verifier.md        → ../../tools/agents/wiki-source-verifier.agent.md
  wiki-contradiction-detector.md → ../../tools/agents/wiki-contradiction-detector.agent.md
  wiki-search.md                 → ../../tools/agents/wiki-search.agent.md
  project-assistant.md           → ../../tools/agents/project-assistant.agent.md
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

# Scaffold a new project that consumes the wiki as a KB
python3 tools/wiki.py project new my-thesis
python3 tools/wiki.py project link my-thesis concepts/some-relevant-page

# Ask the project-assistant a question in that project's context
python3 tools/agents/wiki-agent.py project --project my-thesis --question "Which control loop should we use for the manipulator?"
```

### Available Agents

The agents are designed to be **orthogonal** — each owns a distinct slice of the
maintenance workflow. Pick by matching the input you have and the action you want.

| Agent | Reads | Writes | Owns |
|-------|-------|--------|------|
| Ingest (`wiki-ingest`) | new raw source + wiki | wiki | First-pass extraction of a brand-new source into the wiki. |
| Enhance (`wiki-enhancer`) | already-ingested raw source + wiki | wiki | Iterative improvement of pages that already exist — fix, deepen, interlink, spawn new concept pages, and rewrite stubs toward the canonical concept-page structure. Supports loop mode (shallowest stub / random / sparse-coverage). |
| Quality (`wiki-quality-reviewer`) | one wiki page | — | Intrinsic page-level audit (structure, summary, falsifiability, frontmatter). No source compare. |
| Verify (`wiki-source-verifier`) | one wiki page + its raw source-text | — | Source-fidelity audit: does the wiki accurately represent the source? |
| Contradict (`wiki-contradiction-detector`) | many wiki pages | — | Intra-wiki conflict detection across pages. Does not consult raw sources. |
| Search (`wiki-search`) | wiki | — | Query answering — locate pages, read, synthesize a cited answer. No project context. |
| Project (`project-assistant`) | one `projects/<slug>/` + its referenced wiki pages | `projects/<slug>/` only | Project-scoped Q&A — answers grounded in a specific project's context, cites wiki pages, saves durable Q&A artifacts. Never writes to `wiki/`. |

**Decision matrix** — pick the agent by what you have and what you want:

| You have... | You want to... | Use |
|---|---|---|
| A new file in `raw/sources/` | Add it to the wiki | `wiki-ingest` |
| An existing wiki page that feels shallow or stale | Improve it (in place) | `wiki-enhancer` |
| A vague "keep enhancing the wiki / next stub / random page" | Loop-mode rewrite toward canonical structure | `wiki-enhancer` |
| A wiki page you suspect drifts from the original | Verify against the source | `wiki-source-verifier` |
| A wiki page you want a structural audit of | Audit, no edits | `wiki-quality-reviewer` |
| Suspicion that two pages disagree | Surface and analyze the conflict | `wiki-contradiction-detector` |
| A research question with no project context | Get a synthesized cited answer | `wiki-search` |
| A question about a specific project under `projects/` | Project-scoped answer that cites wiki pages, optionally saved as a query artifact | `project-assistant` |

**Handoff conventions** — each agent ends its report by recommending the next
agent (e.g. quality → enhancer to apply fixes; contradict → verifier to determine
which side is correct). Read the full agent `.agent.md` files for the exact
handoff list.

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
7. Run `python3 tools/wiki.py lint` and fix issues.
8. Append to log using `python3 tools/wiki.py append-log ...`.

### Query

When answering a user question using this wiki:

1. Open `wiki/index.md` for an overview of existing pages (rendered dynamically by Dataview).
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

- `wiki/index.md` uses Dataview queries and updates automatically — no rebuild needed.
- `wiki/log.md` is append-only chronological history.
- Log headings use: `## [YYYY-MM-DD] operation | title`.

## Search

### QMD — primary search engine

The wiki uses [qmd](https://www.npmjs.com/package/@tobilu/qmd) for hybrid BM25 + vector + LLM-rerank search across `wiki/` and `raw/`. **All search-using agents (wiki-search, project-assistant, wiki-enhancer, wiki-contradiction-detector) prefer qmd over `wiki.py search` when available.**

```bash
# Install
bun install -g @tobilu/qmd     # or: npm install -g @tobilu/qmd

# Initial setup (creates wiki + raw collections, builds index, generates embeddings)
./tools/scripts/setup-qmd.sh

# Re-index after adding new files
qmd update

# Refresh vector embeddings after content changes
qmd embed

# Search (in order of preference for agents)
qmd query "<natural language question>" --json   # hybrid + reranking; best for conceptual queries
qmd search "<keywords>"                          # BM25 only; fast, good for exact terms
qmd vsearch "<question>"                         # vector only; rare niche
```

#### MCP server for agents

`qmd mcp` runs an MCP server (stdio transport) exposing `mcp__qmd__*` tools. Opencode and Claude Code can connect to it for first-class search without bash. See `qmd skill show` for the bundled agent skill.

#### Health checks

```bash
qmd status                                       # index health summary
qmd collection list                              # list collections
qmd collection show wiki                         # collection details
qmd ls wiki                                      # list indexed files
```

If the collection paths are wrong (e.g. after the vault was moved), remove and re-add:

```bash
qmd collection remove wiki && qmd collection remove raw
./tools/scripts/setup-qmd.sh
```

### `wiki.py` search — fallback

`python3 tools/wiki.py search "<query>"` is a substring matcher over the wiki bodies. It's the fallback when qmd isn't installed or indexed. Always works without setup.

## Optional tools

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
python3 tools/wiki.py lint                 # Health check (links, metadata, projects, staleness)
python3 tools/wiki.py lint --strict        # Full check including orphans
python3 tools/wiki.py search "term"        # Substring search (qmd is preferred — see Search section)
python3 tools/wiki.py tags <tag>           # Filter pages by tag (AND across multiple tags)
python3 tools/wiki.py coverage             # Rank sparse / underlinked pages
python3 tools/wiki.py validate-log         # Check log entry format
python3 tools/wiki.py append-log ...       # Add log entry
python3 tools/wiki.py preprocess           # Pre-extract raw/sources/*.pdf -> raw/sources-text/*.md

# Projects layer (application workspaces that consume the wiki KB)
python3 tools/wiki.py project list                                # enumerate projects
python3 tools/wiki.py project new <slug>                          # scaffold project.md + queries/
python3 tools/wiki.py project show <slug>                         # print details + subfolder tree
python3 tools/wiki.py project link <slug> concepts/some-page      # append wiki_ref + bump updated

# qmd search (preferred over wiki.py search for agents; see Search section above)
qmd query "<question>" --json                                     # hybrid BM25 + vector + LLM rerank
qmd search "<keywords>"                                           # BM25 only, fast
qmd update                                                        # re-index after content changes
qmd embed                                                         # refresh vector embeddings
qmd status                                                        # health check

# AI-powered agents (use wiki-agent.py)
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/x.md
python3 tools/agents/wiki-agent.py verify --source wiki/sources/x.md
python3 tools/agents/wiki-agent.py ingest --source raw/sources/x.pdf
python3 tools/agents/wiki-agent.py enhance --coverage
python3 tools/agents/wiki-agent.py search --page "topic"
python3 tools/agents/wiki-agent.py contradict
python3 tools/agents/wiki-agent.py project --project <slug> --question "..."

# Extra utilities
python3 tools/wiki_extra.py next-id         # Generate next source ID
python3 tools/wiki_extra.py qmd-search "q"  # QMD search via wiki_extra wrapper
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
