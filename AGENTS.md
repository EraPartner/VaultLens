# LLM Wiki Operating Schema

This vault implements the "LLM Wiki" pattern (after Karpathy's llm-wiki) as a persistent,
compounding knowledge base. This file is the single, vendor-neutral source of truth for how any AI
tool operates here — Claude Code (`projects/*/CLAUDE.md` import it via `@AGENTS.md`), opencode
(`opencode.json` `instructions`), and Copilot CLI (reads `AGENTS.md` from CWD).

## Purpose

Maintain a durable wiki in `wiki/` from immutable source material in `raw/`.

- `raw/` is the source of truth; normal ingest flows do not modify it in place.
- `wiki/` is agent-owned and updated incrementally.
- `wiki/index.md` (Dataview catalog) and `wiki/log.md` are mandatory navigation files.

## Architecture — four layers

1. **Raw** (`raw/`) — immutable source documents. Source of truth.
2. **Wiki** (`wiki/`) — LLM-generated markdown. The agent owns this layer.
3. **Projects** (`projects/`) — application workspaces that consume the wiki as a knowledge base.
   Each has its own context, notes, and preserved Q&A. Projects may reference wiki pages but
   **never write to `wiki/` or `raw/`**. Any AI tool launched from a project directory follows
   `## Working inside a project`.
4. **Schema** (`AGENTS.md`) — this file.

## Directory contract

- `raw/sources/` immutable source docs · `raw/assets/` images/attachments · `raw/inbox/` new files awaiting ingest
- `wiki/system/` schema & operating docs · `wiki/sources/` one page per ingested source ·
  `wiki/entities/` person/org/tool/place/artifact · `wiki/concepts/` concept/method pages ·
  `wiki/topics/` thematic syntheses · `wiki/syntheses/` cross-topic analyses ·
  `wiki/comparisons/` side-by-side · `wiki/queries/` preserved Q&A · `wiki/reports/` lint/audit outputs ·
  `wiki/inventory/<kind>/` tracked intentions (ingest-candidate/question/task/watch/corpus/artifact/item) ·
  `wiki/_templates/` page templates
- `projects/<slug>/` one folder per project · `project.md` metadata · `notes/` scratch · `queries/` durable Q&A
- `tools/wiki.py` core utility · `tools/wiki_extra.py` extras · `tools/scripts/` setup helpers ·
  `tools/agents/` agent system prompts (source of truth) · `.opencode/agents/` opencode symlinks → `tools/agents/`

## Required page metadata

All content pages need YAML frontmatter with at least: `title`, `type` (page/source/entity/concept/
topic/synthesis/comparison/query/project/inventory), `status` (active/superseded/archived/draft), `created`
(`YYYY-MM-DD`), `updated`, `summary` (one falsifiable sentence). Optional: `domain`
(personal/research/work/learning), `tags`, `confidence` (high/medium/low — evidential trust),
`volatility` (hot/warm/cold — refresh cadence; drives staleness thresholds 60/180/365 days).

- Analytical pages (`concepts/`, `topics/`, `syntheses/`, `comparisons/`) should set `confidence`
  and `volatility`; `lint` validates the values and flags low-confidence pages for follow-up.

- `wiki/sources/*` also need: `source_id` (e.g. `src-2026-04-11-001`), `source_type`
  (article/paper/book/pdf/video/podcast/dataset/note/other), `origin`, `ingested_on`.
- `projects/<slug>/project.md` also need: `wiki_refs` (the `[concepts/foo, topics/bar]` wikilinks the
  project depends on), plus first-class `tags` and `domain` (used to scope wiki search to the project).

**PDF support:** PDFs are first-class raw sources — place in `raw/sources/` or `raw/inbox/`; the
model reads them directly and `wiki-ingest` extracts key claims into a source page. For large/complex
PDFs, `python3 tools/wiki.py preprocess` pre-extracts `raw/sources/*.pdf` → `raw/sources-text/*.md`.

## Tool permissions

Reads are auto-approved; writes require explicit confirmation. Enforced at the tool level for Claude
Code and opencode; a standing instruction for all other models.

| Operation | Policy |
|---|---|
| Read files anywhere in the vault | auto-approved |
| Read-only shell (`ls`, `find`, `grep`, `cat`, `head`, `tail`, `wc`, `sort`, `uniq`, `cut`, `tr`, `date`, `python3`, `qmd`) | auto-approved |
| Write shell (`touch`, `mkdir`, `mv`, `cp`, `sed`, `awk`) | auto-approved for write-access agents only |
| Write or edit files | requires confirmation |

- **Claude Code** — `.claude/settings.json` `allowedTools`.
- **opencode** — `opencode.json` `permission: { read, bash, write, edit }`.
- **Copilot CLI** — reads `AGENTS.md` from repo root + CWD; `qmd` MCP in `~/.copilot/mcp-config.json`; `wiki-agent.py` passes `--allow-all-paths` + read-only `--allow-tool` flags.
- **All other models** — never write a file without explicit user approval in the same session.

`raw/` may contain symlinks to files/dirs outside the vault; they're followed automatically by the
model and the wiki tools, so existing data need not be duplicated.

## Projects layer

`projects/` consumes the wiki as a knowledge base. Each subfolder is one project workspace that owns
its structure. The scaffold (`project new`) creates `project.md`, `CLAUDE.md`, `AGENTS.md`,
`opencode.json`, `TODO.md`, and `queries/`; the user adds whatever else the project needs
(`papers/`, `meetings/`, `repos/`, `drafts/`, `data/`, …).

### Scaffolded structure

```
projects/<slug>/
  project.md      ← metadata + Description + Layout + Rules + Key questions + Context + linked wiki pages
  AGENTS.md       ← AI entrypoint: read project.md + ../../AGENTS.md
  CLAUDE.md       ← Claude shim: @AGENTS.md + @project.md
  opencode.json   ← opencode shim: instructions: ["AGENTS.md"]
  TODO.md         ← per-project todo, embedded into projects/TODO.md
  queries/        ← default Q&A landing zone (overridable in ## Rules)
```

`AGENTS.md` is the single entrypoint for all AI tools. `projects/TODO.md` aggregates each project's
`TODO.md` via Obsidian embeds (the scaffold appends an embed line per `project new`).

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
## Layout                 ← what each subfolder contains; the agent reads this first
## Rules                  ← project-specific rules the agent MUST follow
## Key questions
## Context
## Linked wiki pages
```

`wiki_refs` and `tags` are load-bearing: agents use them to scope which wiki pages to pull into
context. Add refs with `python3 tools/wiki.py project link <slug> <wiki-ref>` (never hand-edit
frontmatter — the command preserves YAML and bumps `updated`).

- **`## Layout`** — document the actual folder structure so the agent knows where to look before
  acting. If missing, the agent falls back to `ls`/`find`.
- **`## Rules`** — free-form, project-specific. **Project rules override the defaults in
  `## Working inside a project` when they conflict.**

### Keeping `project.md` current

`project.md` is the model-agnostic source of truth. After any session that establishes new
information (meeting outcome, decision, direction change, completed deliverable, new deadline),
update the relevant sections before ending: Current status/deliverables, Key questions, Context,
Description/research question, Planning. Edit only sections that changed; small targeted updates;
bump `updated`.

### Boundary rules

- Projects MAY reference any wiki page via wikilinks; `lint` validates `wiki_refs` against the wiki page set.
- Projects MUST NOT modify `wiki/` or `raw/`; an agent's write surface is restricted to `projects/<slug>/`.
- If wiki coverage is lacking, recommend a `wiki-enhancer` follow-up rather than editing the wiki.
- `lint` checks projects for required frontmatter + broken `wiki_refs`; body content (Layout, Rules) is free-form.

### Working inside a project (instructions for agents)

When operating from a `projects/<slug>/` directory, follow these on top of `## Rules` in `project.md`.

**Wiki search ladder** — try in order, stop when you have enough:

1. `qmd query "<question>" --json` — hybrid BM25 + vector + LLM rerank; best for conceptual questions.
2. `qmd search "<keywords>"` — BM25 only; fast, free, good for exact terms.
3. `python3 tools/wiki.py search "<query>"` — substring fallback when qmd is unavailable.
4. `python3 tools/wiki.py tags <tag> [<tag>...]` — AND-filter wiki pages by the project's `tags`.

If `mcp__qmd__*` tools are exposed in the session, prefer them over the CLI.

> **In the devcontainer (no GPU): invert the ladder — lead with `qmd search` (BM25, instant).**
> `qmd query`/`vsearch` and `mcp__qmd__query` run an LLM expand+embed+rerank pipeline that costs
> 30s+ on 4 CPU cores and currently stalls for minutes because the snapshot index's chunks are
> stamped with an uncached embed model (`embeddinggemma-300M`) that qmd then tries to fetch through
> the locked egress. Fix by re-embedding on the host (`qmd embed`, Metal) so chunks re-stamp to the
> cached Qwen3 model; it propagates to the container on next start.

**Citation discipline** — every load-bearing claim carries an inline wikilink
(`[[concepts/some-page]]`) to the wiki page that backs it. Mark anything not wiki-backed as
`[outside wiki — agent inference]`. Unmarked claims are treated as general knowledge.

**Saving durable Q&A** — when an answer captures a non-trivial decision/design/analysis the project
will reference later, save it to `projects/<slug>/queries/YYYY-MM-DD-<topic>.md` (unless `## Rules`
overrides) with frontmatter (`type: query`, inherit project `tags`, list cited `wiki_refs`) and body
`## Question` / `## Answer` (inline wikilinks) / `## Sources` / `## Follow-ups`. Skip the artifact
for trivial one-line Q&A.

There is no dedicated project agent: launch any AI CLI from inside `projects/<slug>/` and it picks up
the project's `AGENTS.md`/`CLAUDE.md`, which loads `project.md` plus this root schema.

## Agent integration

For complex wiki tasks use the custom agents in `tools/agents/` (`*.agent.md`). opencode loads them
as native sub-agents via `.opencode/agents/` symlinks; Claude/Copilot/others run them via
`wiki-agent.py`, which injects the agent body as a system prompt. The agents are **orthogonal** —
pick by what you have and what you want:

| You have… | You want to… | Use |
|---|---|---|
| A new file in `raw/sources/` | Add it to the wiki | `wiki-ingest` |
| An existing wiki page that's shallow/stale | Improve it in place (also loop mode: "next stub / random / keep going") | `wiki-enhancer` |
| A wiki page that may drift from its source | Verify against the source | `wiki-source-verifier` |
| A wiki page to structurally audit | Audit, no edits | `wiki-quality-reviewer` |
| Suspicion two pages disagree | Surface + analyze the conflict | `wiki-contradiction-detector` |
| A research question, no project context | Synthesized cited answer | `wiki-search` |
| A question about a `projects/` project | Project-scoped cited answer | Launch any AI CLI from `projects/<slug>/` |

**Reads / writes:** ingest (raw+wiki → wiki) · enhance (raw+wiki → wiki) · quality/verify/contradict/
search (read-only). **Handoff:** each agent ends by recommending the next (quality → enhancer to apply
fixes; contradict → verifier to decide which side is right). Read the `.agent.md` files for exact handoff lists.

**CLIs:** `opencode` · `claude` · `ollama` · `copilot`. **Models:** opencode `github-copilot/gpt-5.2`
(default) / `gpt-5.3-codex` / `opencode/minimax-m2.5-free`; claude `sonnet` (default) / `haiku` / `opus`;
ollama `qwen3.5:4b` (default) / `qwen3.5:9b` / `gemma4:e4b`; copilot `gpt-5.2` (default) / `gpt-5.3-codex`
/ `claude-sonnet-4.6`. **Effort:** `low` / `medium` (default) / `high`.

## Devcontainer sandbox

The agents run in a hardened devcontainer (`.devcontainer/`, see its `README.md`): egress is locked
to an allowlist proxy, the CLIs run as a non-root user with `--dangerously-skip-permissions`, and the
only reachable host service is the Ollama daemon. Launch from the host with the `brain-*` wrappers
(`brain-wiki <agent> …`, `brain-claude`, `brain-copilot`, `brain-opencode`, `brain-shell`).
`tools/agents/wiki-agent.py` refuses to run on the host — invoke it via `brain-wiki`.

**Inside the devcontainer (`$DEVCONTAINER=true`):** `~/.claude/` and `~/.claude.json` are an isolated
copy, host-pulled on start but **not** pushed back automatically. If you change in-container Claude
config (agents, plugins, slash commands, hooks, MCP servers, rules, settings), tell the user before
ending your turn to run on the host: `brain-claude-sync push` (it backs up `~/.claude.json` before a
newer-wins merge). Without it the change is lost on the next container rebuild. Outside the
devcontainer this does not apply.

## Canonical operations

- **Ingest** — `wiki-ingest`: extraction, source-page creation, concept/topic updates, lint, log entry.
- **Query** — `wiki-search` (general); for project-scoped Q&A run any AI CLI from `projects/<slug>/`.
  Durable answers → `wiki/queries/` (general) or `projects/<slug>/queries/` (project).
- **Lint / health check** — programmatic (fast): `wiki.py lint` (links, metadata, status/date
  validity, confidence/volatility, volatility-aware staleness), `lint --strict` (orphans too),
  `lint --json` (CI/agents), `lint --fix` (case-normalise enum/status), `validate-log`. Tests:
  `python3 tools/tests/test_wiki.py`. Semantic (thorough): `wiki-agent.py quality` /
  `contradict` / `verify`. Write findings to `wiki/reports/`; fix highest-priority issues.

## Conventions

**Links/citations:** write Obsidian path-based wikilinks `[[path/to/page]]` (the canonical form);
source citations in a `## Sources` section listing `[[sources/...]]`; keep external URLs on source
pages and reference sources indirectly from concept/topic pages. For portability outside Obsidian
(GitHub, plain-markdown viewers, headless agents) wikilinks carry a **dual-link** markdown mirror —
`[[concepts/foo]] ([Foo Title](../concepts/foo.md))`. Do **not** hand-write the `([Title](path.md))`
mirror (relative paths are error-prone); write the bare `[[...]]` and run
`python3 tools/wiki.py links --fix --write`, which adds mirrors deterministically and idempotently.
`python3 tools/wiki.py links` reports coverage without writing.

**Change quality:** preserve validated content unless superseded by stronger evidence; mark superseded
claims `status: superseded` (don't silently delete history); keep summaries concise + falsifiable;
favor incremental edits across related pages over isolated notes; bump `updated` when editing.

**Archiving:** to retire a page without deleting it, use `python3 tools/wiki.py archive page <ref>
--reason "…"` (sets `status: archived`, records it in `wiki/system/archive-registry.json`). Archived
pages stay on disk so existing wikilinks keep resolving, but are excluded from staleness/orphan checks
and from `search` (pass `--include-archived` to include them). Reverse with `archive restore <ref>`.

**Index/log:** `wiki/index.md` (Dataview) updates automatically inside Obsidian — no rebuild.
For headless agents (devcontainer, CI, plain markdown viewers) the derived `wiki/_index.md` +
`wiki/<category>/_index.md` files are the readable mirror: regenerate with
`python3 tools/wiki.py index --rebuild` after adding/removing pages (`index` alone reports
staleness). These `_index.md` files are generated — never hand-edit them. `wiki/log.md` is
append-only; headings `## [YYYY-MM-DD] operation | title`.

## Search

[qmd](https://www.npmjs.com/package/@tobilu/qmd) is the primary engine — hybrid BM25 + vector +
LLM-rerank over `wiki/` and `raw/`. **All search-using agents prefer qmd over `wiki.py search` when
available** (see the devcontainer caveat in `## Working inside a project`). `qmd mcp` exposes
`mcp__qmd__*` tools (stdio) for opencode/Claude Code. `python3 tools/wiki.py search "<query>"` is the
substring fallback that always works without setup. One-time host setup + re-index live in
`tools/scripts/setup-qmd.sh`; `qmd status` / `qmd collection list` for health.

## Optional tools — Obsidian skill (Claude Code only)

When running in Claude Code, prefer the `obsidian:` skill family for vault-native operations (other
CLIs fall back to plain file tools):

| Skill | Use for |
|---|---|
| `obsidian:obsidian-markdown` | Writing/editing wiki pages (wikilinks, callouts, tags, frontmatter) |
| `obsidian:obsidian-cli` | Vault ops (open note, search index, navigate) |
| `obsidian:defuddle` | Extracting clean markdown from a URL before placing it in `raw/inbox/` |
| `obsidian:json-canvas` | `.canvas` files |
| `obsidian:obsidian-bases` | `.base` files |

There is no Obsidian MCP server in this setup. `obsidian-cli` and `defuddle` are host-only (they
need the `obs` binary, a running Obsidian app, or network) — inside the egress-locked sandbox use
`obsidian-markdown` for formatting plus the normal file tools.

Templater auto-applies the matching `wiki/_templates/` template when a file is created in a `wiki/`
subfolder; Dataview tables update automatically from frontmatter (JS API enabled).

## Command reference

```bash
# Core maintenance
python3 tools/wiki.py lint                       # links, metadata, status/date validity, staleness, confidence/volatility
python3 tools/wiki.py lint --strict              # + orphan pages
python3 tools/wiki.py lint --json                # machine-readable report (errors/warnings split)
python3 tools/wiki.py lint --fix                 # case-normalise confidence/volatility/status values
python3 tools/tests/test_wiki.py                 # tooling test suite (golden + per-rule defect fixtures)
python3 tools/wiki.py search "term"              # substring search (qmd preferred — see Search)
python3 tools/wiki.py tags <tag> [<tag>...]      # AND-filter pages by tag
python3 tools/wiki.py coverage                   # rank sparse / underlinked pages
python3 tools/wiki.py index                      # report stale _index.md mirrors
python3 tools/wiki.py index --rebuild            # regenerate headless-readable _index.md files
python3 tools/wiki.py links                       # report wikilink dual-link coverage
python3 tools/wiki.py links --fix --write         # add portable markdown mirrors to wikilinks
python3 tools/wiki.py archive list                # list archived pages (+ registry drift)
python3 tools/wiki.py archive page concepts/foo --reason "superseded by bar"   # archive a page
python3 tools/wiki.py archive restore concepts/foo   # un-archive a page
python3 tools/wiki.py search "term" --include-archived   # search incl. archived (excluded by default)

# Inventory — track intentions distinct from raw/ and wiki/ (ingest-candidate/question/task/watch/...)
python3 tools/wiki.py inventory list                      # all records (filter: inventory list <kind> / --status X)
python3 tools/wiki.py inventory new question how-x-works --priority p1 --summary "..."
python3 tools/wiki.py inventory show question/how-x-works
python3 tools/wiki.py validate-log               # check log format
python3 tools/wiki.py append-log ...             # add a log entry
python3 tools/wiki.py preprocess                 # pre-extract raw/sources/*.pdf -> raw/sources-text/*.md

# Projects
python3 tools/wiki.py project list               # enumerate projects
python3 tools/wiki.py project new <slug>         # scaffold project.md + shims + queries/
python3 tools/wiki.py project show <slug>        # details (--json for machine output)
python3 tools/wiki.py project link <slug> concepts/some-page   # append wiki_ref + bump updated

# qmd search (preferred for agents; see Search)
qmd query "<question>" --json                    # hybrid BM25 + vector + LLM rerank
qmd search "<keywords>"                           # BM25 only, fast
qmd update                                        # re-index after content changes
qmd embed                                         # refresh vector embeddings (host: Metal)
qmd status                                        # index health

# AI agents (wiki-agent.py — injects the tools/agents/*.agent.md body as system prompt)
python3 tools/agents/wiki-agent.py ingest --source raw/sources/x.pdf
python3 tools/agents/wiki-agent.py enhance --coverage
python3 tools/agents/wiki-agent.py quality --page wiki/concepts/x.md [--cli claude --model sonnet --effort high]
python3 tools/agents/wiki-agent.py verify --source wiki/sources/x.md
python3 tools/agents/wiki-agent.py search --page "topic"
python3 tools/agents/wiki-agent.py contradict

# Extras
python3 tools/wiki_extra.py next-id              # next source ID
python3 tools/wiki_extra.py stats                # wiki statistics
```
