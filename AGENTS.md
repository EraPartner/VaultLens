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
3. **Projects** (`projects/`) - Application workspaces that consume the wiki as a knowledge base. Each project has its own context, notes, and preserved Q&A. Any AI tool launched from a project directory follows the conventions in `## Working inside a project`. Projects may reference wiki pages but never write to `wiki/`.
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
- `projects/<slug>/queries/` - durable Q&A artifacts saved by project-scoped agent runs
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
- `tags` and `domain` are first-class for projects (used to scope wiki search to the project's domain)

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

## Tool permissions

Reads are auto-approved; writes require explicit confirmation. This is enforced at the tool level for Claude Code and opencode, and is a standing instruction for all other models.

| Operation | Policy |
|---|---|
| Read files anywhere in the vault | auto-approved |
| Run read-only shell commands: `set`, `ls`, `find`, `grep`, `cat`, `head`, `tail`, `wc`, `sort`, `uniq`, `cut`, `tr`, `date`, `python3`, `qmd` | auto-approved |
| Run write shell commands (agents with write access only): `touch`, `mkdir`, `mv`, `cp`, `sed`, `awk` | auto-approved for write agents |
| Write or edit files | requires confirmation |

**Claude Code** — configured in `.claude/settings.json` via `allowedTools`.
**opencode** — configured in `opencode.json` via `permission: { read, bash, write, edit }`.
**GitHub Copilot CLI** — reads `AGENTS.md` automatically from repo root and CWD; `qmd` MCP server configured in `~/.copilot/mcp-config.json`. `wiki-agent.py` passes `--allow-all-paths` and read-only `--allow-tool` flags automatically.
**All other models** — treat this section as a standing instruction: never write a file without explicit user approval in the same session.

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

**Each project owns its own folder structure.** The scaffold creates `project.md`, `CLAUDE.md`, `AGENTS.md`, `opencode.json`, `TODO.md`, and a default `queries/` directory; the user defines whatever else the project needs (`papers/`, `meetings/`, `repos/`, `drafts/`, etc.). The project's layout and rules are documented inside `project.md` itself; agents operating in the project read them before answering.

### Minimum scaffolded structure

```
projects/<slug>/
  project.md          ← metadata + description + Layout + Rules + linked wiki pages
  AGENTS.md           ← AI entrypoint: instructs any tool to read project.md + ../../AGENTS.md
  CLAUDE.md           ← Claude Code shim: @AGENTS.md (one line)
  opencode.json       ← opencode shim: instructions: ["AGENTS.md"]
  TODO.md             ← per-project todo; embedded into projects/TODO.md
  queries/            ← default Q&A artifact landing zone (overridable in ## Rules)
```

`AGENTS.md` is the single entrypoint for all AI tools. Claude Code imports it via `@AGENTS.md`; opencode loads it via `instructions`; Copilot CLI reads it automatically from CWD.

`projects/TODO.md` aggregates each project's `TODO.md` via Obsidian embeds. The scaffold appends a new embed line each time `project new` runs, so the aggregator stays in sync without a build step.

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

The `wiki_refs` and `tags` fields are load-bearing: agents working in the project use them to scope which wiki pages they pull into context. Use `python3 tools/wiki.py project link <slug> <wiki-ref>` to add a reference (it preserves frontmatter formatting and updates `updated`).

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

Free-form, project-specific rules agents working in the project MUST follow. **Project rules override the defaults in `## Working inside a project`** when they conflict.

```markdown
## Rules

- Save query artifacts under `meetings/qa/` instead of the default `queries/`.
- Treat `repos/` as read-only — never write inside it.
- Cite the source PDF filename whenever referencing a paper from `papers/`.
- Never summarize meeting notes in `meetings/` without asking first.
- For design questions, prefer concepts in `wiki_refs` over general wiki search.
```

### Keeping project.md current

`project.md` is the model-agnostic source of truth for a project. After any session where new information is established — a meeting outcome, a decision, a direction change, a completed deliverable, a new deadline — update the relevant sections of `project.md` before the session ends.

Sections most likely to need updating:

- **Current status / deliverables** — mark things done, add next steps, update deadlines
- **Key questions** — remove resolved questions, add newly opened ones
- **Context** — new constraints, signals, or facts from meetings or supervisor conversations
- **Description / research question** — update if the framing or scope shifts
- **Planning sections** — update if timeline or milestones change

Rules: only edit sections that changed. Small, targeted updates. Update the `updated` frontmatter field whenever you touch the file.

### Boundary rules

- Projects MAY reference any wiki page via wikilinks. Lint validates `wiki_refs` against the canonical wiki page set.
- Projects MUST NOT modify `wiki/` or `raw/`. Agents working in a project have their write surface restricted to `projects/<slug>/`.
- If a project finds wiki coverage lacking, the agent recommends a `wiki-enhancer` follow-up rather than editing the wiki itself.
- `lint` checks projects: required frontmatter fields and broken `wiki_refs`. Project body content (Layout, Rules) is intentionally free-form and not validated.

### Working inside a project (instructions for agents)

When operating from a `projects/<slug>/` directory (CWD or attached project), follow these conventions on top of `## Rules` in `project.md`.

**Wiki search ladder** — try in order, stop when you have enough:

1. `qmd query "<natural-language question>" --json` — hybrid BM25 + vector + LLM rerank. Best for conceptual questions.
2. `qmd search "<keywords>"` — BM25 only. Fast, free, good for exact terms.
3. `python3 tools/wiki.py search "<query>"` — substring fallback when qmd is unavailable.
4. `python3 tools/wiki.py tags <tag> [<tag>...]` — AND-filter wiki pages by the project's frontmatter `tags` to surface sibling concept pages.

If `mcp__qmd__*` tools are exposed in the session, prefer them over the CLI.

**Citation discipline** — every load-bearing claim in a project answer carries an inline wikilink (`[[concepts/some-page]]`) to the wiki page that backs it. Mark anything not backed by the wiki as `[outside wiki — agent inference]`. A claim with no marker is treated as obviously general knowledge.

**Adding a `wiki_ref`** — never hand-edit `project.md` frontmatter. Use `python3 tools/wiki.py project link <slug> <wiki-ref>`; it preserves YAML formatting and bumps `updated`.

**Saving durable Q&A** — when an answer captures a non-trivial decision, design, or analysis the project will reference later, save it. Default path is `projects/<slug>/queries/YYYY-MM-DD-<short-topic-slug>.md` unless `## Rules` overrides:

```yaml
---
title: <short title of the question>
type: query
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
summary: <one-line summary of the question + answer>
tags: [<inherit from project + add specific tags>]
wiki_refs: [<the wiki pages this answer cites>]
---
```

Body sections:

- `## Question` — the question verbatim or paraphrased.
- `## Answer` — synthesis with inline wikilinks.
- `## Sources` — bullet list of cited `[[wiki-pages]]`.
- `## Follow-ups` — open questions or recommended agent passes.

Skip the artifact for trivial Q&A — one-line answers belong inline.

### CLI

```bash
python3 tools/wiki.py project list                                       # list all projects
python3 tools/wiki.py project new <slug>                                 # scaffold a new project
python3 tools/wiki.py project show <slug>                                # print details (use --json for machine output)
python3 tools/wiki.py project link <slug> concepts/some-page             # append wiki ref + bump updated
```

### Working in a project from any AI tool

There is no dedicated project agent. Launch any AI CLI (Claude Code, opencode, Copilot CLI) from inside `projects/<slug>/` and it will pick up the project's `AGENTS.md` (and `CLAUDE.md` shim), which loads `project.md` plus the root schema. The behavior described in `## Working inside a project` above is the contract every such session follows.

## Agent integration

For complex wiki tasks, use the project's custom agents stored in `tools/agents/`:

### When to use wiki agents

- **Ingest**: Use `wiki-ingest` for first-pass intake of a brand-new source.
- **Enhance**: Use `wiki-enhancer` to iteratively deepen, fix, and interlink already-ingested content — also covers iterative-loop mode ("next stub", "random page", "keep going on the wiki") rewriting pages toward the canonical structure.
- **Quality review**: Use `wiki-quality-reviewer` for intrinsic page-level audits (read-only).
- **Source verification**: Use `wiki-source-verifier` to verify wiki claims against the raw source.
- **Contradiction detection**: Use `wiki-contradiction-detector` to surface intra-wiki conflicts.
- **Search/Research**: Use `wiki-search` to answer questions with cited synthesis.
- **Project Q&A**: Launch any AI tool from `projects/<slug>/` — the project's `AGENTS.md` shim and root `## Working inside a project` section drive the workflow.

Agent definition files live in `tools/agents/*.agent.md`. opencode loads them as native sub-agents via symlinks in `.opencode/agents/`. Claude Code, Copilot, and other CLIs are invoked via `wiki-agent.py`, which reads the agent file and injects its body as a system prompt — no native subagent registration needed.

### Agent workflow examples

```bash
# Search the wiki
python3 tools/agents/wiki-agent.py search --page "machine learning"

# Quality review (opencode default; override with --cli claude/copilot/ollama)
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md --cli claude --model sonnet --effort high

# Verify source claims
python3 tools/agents/wiki-agent.py verify --source wiki/sources/my-source.md --effort high

# Ingest new source
python3 tools/agents/wiki-agent.py ingest --source raw/sources/paper.pdf

# Find contradictions
python3 tools/agents/wiki-agent.py contradict
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
| A question about a specific project under `projects/` | Project-scoped answer that cites wiki pages, optionally saved as a query artifact | Launch any AI CLI from `projects/<slug>/` |

**Handoff conventions** — each agent ends its report by recommending the next
agent (e.g. quality → enhancer to apply fixes; contradict → verifier to determine
which side is correct). Read the full agent `.agent.md` files for the exact
handoff list.

**CLI options**: `opencode`, `claude`, `ollama`, `copilot`
**OpenCode models**: `github-copilot/gpt-5.2` (default), `opencode/minimax-m2.5-free`, `github-copilot/gpt-5.3-codex`
**Claude models**: `sonnet` (default), `haiku`, `opus`
**Ollama models**: `qwen3.5:4b` (default), `qwen3.5:9b`, `gemma4:e4b`
**Copilot models**: `gpt-5.2` (default), `gpt-5.3-codex`, `claude-sonnet-4.6`
**Effort levels**: `low` (fast), `medium` (default), `high` (deep thinking)

## Devcontainer sandbox

The agents run inside a hardened devcontainer (`.devcontainer/`, see its
`README.md`): egress is locked to an allowlist proxy, the agent CLIs run as a
non-root user with `--dangerously-skip-permissions`, and the only host service
reachable is the Ollama daemon. Launch from the host with the `brain-*`
wrappers — `brain-wiki <agent> …`, `brain-claude`, `brain-copilot`,
`brain-opencode`, `brain-shell`. `tools/agents/wiki-agent.py` refuses to run on
the host; it must be invoked through `brain-wiki` (which executes it in the
sandbox).

### When running inside the devcontainer (`DEVCONTAINER=true`)

The container's `~/.claude/` and `~/.claude.json` are independent copies, not
bind-mounted live. The host auto-pulls into the container on every start
(`post-start.sh`), but the reverse — container → host — is **manual and must be
done explicitly**.

**If you modify `~/.claude/` or `~/.claude.json` during this session** (adding
or editing agents, plugins, slash commands, hooks, MCP servers, rules, or
settings), **tell the user before ending your turn**:

> Heads-up: I changed your in-container Claude config. To propagate those
> changes back to the host safely, run in your host fish shell:
> `brain-claude-sync push`

This is mandatory — `brain-claude-sync push` backs up `~/.claude.json` before a
newer-wins merge, so it is the safe way to sync. Without it, the changes only
live in the container volume and are lost on the next container rebuild. Detect
"inside the devcontainer" by checking `$DEVCONTAINER` (set to `true` by
`devcontainer.json`). Outside the devcontainer this rule does not apply.

## Canonical operations

### Ingest

Use `wiki-ingest` agent: `python3 tools/agents/wiki-agent.py ingest --source raw/sources/FILE.pdf`. The agent handles extraction, source-page creation, concept/topic page updates, linting, and log entry. See `wiki-ingest.agent.md` for the full workflow.

### Query

Use `wiki-search` (general) — `python3 tools/agents/wiki-agent.py search --page "question"`. For project-scoped Q&A, run any AI CLI from inside `projects/<slug>/`. Durable answers worth preserving should be filed as `wiki/queries/` pages (general) or `projects/<slug>/queries/` (project-scoped).

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

The wiki uses [qmd](https://www.npmjs.com/package/@tobilu/qmd) for hybrid BM25 + vector + LLM-rerank search across `wiki/` and `raw/`. **All search-using agents (wiki-search, wiki-enhancer, wiki-contradiction-detector — and any AI tool answering project-scoped questions) prefer qmd over `wiki.py search` when available.**

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

### Obsidian skill (Claude Code)

When running inside Claude Code, the `obsidian:` skill family is available. Use these skills for vault-native operations:

| Skill | Use for |
|-------|---------|
| `obsidian:obsidian-markdown` | Creating or editing wiki pages with Obsidian-flavored markdown (wikilinks, callouts, tags, frontmatter) |
| `obsidian:obsidian-cli` | Vault-level operations (open note, search vault index, navigate) |
| `obsidian:defuddle` | Extracting clean markdown from a web URL before placing it in `raw/inbox/` |
| `obsidian:json-canvas` | Creating or editing `.canvas` files |
| `obsidian:obsidian-bases` | Creating or editing `.base` files |

**When to prefer these over raw file tools:** use `obsidian:obsidian-markdown` when writing new wiki pages to ensure OFM-compatible formatting (correct wikilink syntax, frontmatter, callouts). Use `obsidian:defuddle` instead of fetching raw HTML when clipping a web article to `raw/inbox/`.

These skills are optional — fall back to Read/Write/Edit tools if the skill is unavailable.

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

# Extra utilities
python3 tools/wiki_extra.py next-id         # Generate next source ID
python3 tools/wiki_extra.py qmd-search "q"  # QMD search via wiki_extra wrapper
python3 tools/wiki_extra.py stats           # Wiki statistics
```

## Obsidian integrations

**Templater** — creating a file in any `wiki/` subfolder auto-applies the matching template from `wiki/_templates/` (title, date, cursor, Dataview backlinks pre-filled).
**Dataview** — dynamic tables throughout the wiki update automatically from frontmatter. JS API is enabled.
