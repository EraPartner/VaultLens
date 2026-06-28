# LLM Wiki Operating Schema

This vault implements the "LLM Wiki" pattern (after Karpathy's llm-wiki) as a persistent,
compounding knowledge base. This file is the source of truth for how Claude Code operates here;
`projects/*/CLAUDE.md` add per-project context on top of it.

Multi-step **runbooks** (ingest, maintenance, projects, agents) live in `.claude/skills/*/SKILL.md`
and load automatically when relevant. Custom **subagents** live in `.claude/agents/*.md`. This file
keeps the always-needed schema and rules; the skills hold the procedures and full command reference.

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
   **never write to `wiki/` or `raw/`**. Any session launched from a project directory follows
   `## Working inside a project`.
4. **Schema** (`CLAUDE.md`) — this file.

## Operator profile

If `wiki/entities/user-background.md` exists (referenced vault-wide as `[[user-background]]`), it is the
profile of the person this Brain serves: background, current focus, goals, and how they want agents to
work. Any agent producing advice, a brief, a status report, or project guidance for the operator should
read it first (it is qmd-indexed and surfaces on a `qmd search "operator profile"`) to calibrate tone and
priorities. It is gitignored (personal), so it ships only in this operator's vault, not the public
template. The Chief of Staff launcher injects it automatically into its live context.

## Directory contract

- `raw/sources/` immutable source docs · `raw/sources-text/` preprocessed PDF text (`preprocess` output) ·
  `raw/assets/` images/attachments · `raw/inbox/` new files awaiting ingest · `raw/review-inbox/` items staged for manual review before ingest
- `wiki/system/` schema & operating docs · `wiki/sources/` one page per ingested source ·
  `wiki/entities/` person/org/tool/place/artifact · `wiki/concepts/` concept/method pages ·
  `wiki/topics/` thematic syntheses · `wiki/syntheses/` cross-topic analyses ·
  `wiki/comparisons/` side-by-side · `wiki/queries/` preserved Q&A · `wiki/reports/` lint/audit + scheduled-agent outputs ·
  `wiki/inventory/<kind>/` tracked intentions (ingest-candidate/question/task/watch/corpus/artifact/item) ·
  `wiki/_templates/` page templates · `wiki/log/` runtime background-agent logs (gitignored) ·
  `wiki/home.md` + `wiki/SETUP.md` reader-facing nav docs
- `projects/<slug>/` one folder per project · `project.md` metadata · `notes/` scratch · `queries/` durable Q&A · `AGENDA.md` dormant autonomous-runner agenda (opt-in via its `enabled` frontmatter flag)
- `tools/wiki.py` CLI dispatcher → focused modules (`wiki_ingest`, `wiki_lint`, `wiki_query`,
  `wiki_projects`, `wiki_index`, `wiki_links`, `wiki_log`, `wiki_inventory`, `wiki_archive`) ·
  `tools/wiki_extra.py` extras · `tools/scripts/` setup helpers · `tools/tests/` tooling test suite ·
  `tools/agents/wiki-agent.py` headless agent launcher · `tools/schedule/` host catch-up dispatcher
  for scheduled agents
- `.claude/agents/` Claude subagent definitions (source of truth for the wiki agents) ·
  `.claude/skills/` operational runbooks (auto-loading skills)

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

Reads are auto-approved; writes require explicit confirmation. Enforcement is layered:

| Operation | Policy |
|---|---|
| Read files anywhere in the vault | auto-approved |
| Read-only shell (`ls`, `find`, `grep`, `cat`, `head`, `tail`, `wc`, `sort`, `uniq`, `cut`, `tr`, `date`, `python3`, `qmd`) | auto-approved |
| Write shell (`touch`, `mkdir`, `mv`, `cp`, `sed`, `awk`) | auto-approved for write-access agents only |
| Write or edit files | requires confirmation |

- Interactive sessions: `.claude/settings.json` `permissions.allow` is the exact source of truth for
  the auto-approved set — beyond the read-only commands above it also pre-approves a few scoped-write
  `wiki.py` subcommands (`preprocess`, `append-log`) and extra read helpers (`wiki_extra.py …`,
  `qmd vsearch`/`ls`).
- Headless agent runs: `wiki-agent.py` builds a per-agent `--allowedTools` allowlist from the
  agent's permission profile, runs `--permission-mode acceptEdits` (writers) / `default`
  (read-only), and passes `--disallowedTools Task` (a wiki agent must never spawn its own
  subagent). Each `.claude/agents/*.md` `tools:` frontmatter mirrors the read/write *capability
  tier* — but a frontmatter `Bash` token is unrestricted (it cannot encode the per-command
  allowlist), so interactive subagent runs rely on the operator's permission prompts, the global
  bash guard, and the container mount for command scoping, not the `tools:` line.
- The egress-locked devcontainer mount (see `## Devcontainer sandbox`) is the kernel-level backstop.

`raw/` may contain symlinks to files/dirs outside the vault; they're followed automatically by the
model and the wiki tools, so existing data need not be duplicated.

## Projects layer

`projects/` consumes the wiki as a knowledge base. Each subfolder is one project workspace that owns
its structure. The scaffold (`project new`) creates `project.md`, `CLAUDE.md`, `TODO.md`, `AGENDA.md`,
and `queries/`; `CLAUDE.md` is the AI entrypoint (it imports `project.md`; this root schema loads
automatically via directory walking).

**Autonomous runner:** every project carries a dormant `AGENDA.md` (loose `## Inbox` + groomed
`## Tasks`). Flip its frontmatter `enabled: true` to opt the project into the nightly `project-runner`
agent, which grooms loose tasks into a clear structured form, executes the ones that are 100% clear
and due (writing only inside `projects/<slug>/`, applied-not-committed with a pre-run snapshot for
undo), and files clarifications for anything ambiguous. Resolve those interactively with
`/project-clarify`. See `## Scheduled agents` and the runbook below.

**Runbook:** scaffolded structure, the `project.md` page schema, `project new/link/show` usage,
keeping `project.md` current, the TODO.md format/aggregators, and the `AGENDA.md` schema +
`project agenda` subcommands live in `.claude/skills/wiki-projects/SKILL.md` — read it before
creating or restructuring a project.

Always-needed facts: `project.md` is the per-project source of truth — `wiki_refs` and `tags`
in its frontmatter are load-bearing (they scope which wiki pages agents pull into context; add refs
with `project link`, never hand-edit). Its `## Rules` section **overrides the defaults in
`## Working inside a project` when they conflict**. After any session that establishes new
information, update the changed `project.md` sections and bump `updated`.

### Boundary rules

- Projects MAY reference any wiki page via wikilinks; `lint` validates `wiki_refs` against the wiki page set.
- Projects MUST NOT modify `wiki/` or `raw/`; an agent's write surface is restricted to `projects/<slug>/`.
- If wiki coverage is lacking, recommend a `wiki-enhancer` follow-up rather than editing the wiki.
- `lint` checks projects for required frontmatter + broken `wiki_refs`; body content (Layout, Rules) is free-form.

### Working inside a project (instructions for agents)

The project-session rules — wiki search ladder (and its devcontainer inversion), citation
discipline, durable-Q&A format, write boundary — load path-scoped from
`.claude/rules/working-in-projects.md` when working under `projects/`. Project `## Rules` in
`project.md` override them on conflict.

There is no dedicated project agent: launch Claude Code from inside `projects/<slug>/` and it picks
up the project's `CLAUDE.md`, which loads `project.md` plus this root schema.

## Agent integration

For complex wiki tasks use the custom agents in `.claude/agents/` (`*.md`): `wiki-ingest`,
`wiki-enhancer`, `wiki-source-verifier`, `wiki-quality-reviewer`, `wiki-contradiction-detector`,
`wiki-search`, plus the read-only thinking agents `wiki-challenge` / `wiki-connect` / `wiki-emerge` /
`wiki-idea-discovery`. They are native Claude Code subagents (invoke by name in a session); headless
and batch runs go through `tools/agents/wiki-agent.py` (host: `brain-wiki`), which adds the enhance
loops, CoS live-context gathering, PDF pre-extraction, and auto-logging.

**Runbook:** the what-agent-for-what table, reads/writes + handoffs, thinking-agent flags,
`wiki-agent.py` invocations, and model/effort options live in
`.claude/skills/wiki-agents/SKILL.md` — read it before picking or launching an agent.

## Devcontainer sandbox

The agents run in a hardened devcontainer (`.devcontainer/`, see its `README.md`): egress is locked
to an allowlist proxy. Interactive sessions (`brain-claude`/`brain-shell`) run the CLI as a non-root
user with `--dangerously-skip-permissions`; headless `wiki-agent.py` runs instead pass an explicit
`--allowedTools` allowlist with `--permission-mode acceptEdits`/`default` and `--disallowedTools Task`
(see `## Tool permissions`).
Launch from the host with the `brain-*` wrappers
(`brain-cos`, `brain-wiki <agent> …`, `brain-claude`, `brain-shell`).
`tools/agents/wiki-agent.py` refuses to run on the host — invoke wiki agents via `brain-wiki`
and the Chief of Staff via `brain-cos`.

**Inside the devcontainer (`$DEVCONTAINER=true`):** `~/.claude/` and `~/.claude.json` are an isolated
copy, host-pulled on start but **not** pushed back automatically. If you change in-container Claude
config (agents, plugins, slash commands, hooks, MCP servers, rules, settings), tell the user before
ending your turn to run on the host: `brain-claude-sync push` (it backs up `~/.claude.json` before a
newer-wins merge). Without it the change is lost on the next container rebuild. Repo-level config —
this file, `.claude/agents/`, `.claude/skills/`, `.claude/settings.json` — lives in the mounted
workspace and needs no sync. Outside the devcontainer this does not apply.

## Scheduled agents

A host-side **catch-up dispatcher** (`tools/schedule/`) runs the maintenance/thinking agents on a
~30-minute launchd tick; each tick is a gate-checker, not an LLM trigger — all LLM work runs in one
nightly batch (AC-only, defer-until-online) on the Claude CLI. Read-only agents stay read-only:
outputs are filed as dated reports under `wiki/reports/`. The one **writer** in the nightly batch is
`project-runner` (runs before `enhance`): for each opted-in project it executes due `AGENDA.md` tasks
inside `projects/<slug>/` (applied-not-committed; the dispatcher clones the project first, so the
roll-up's restore command is the undo since `projects/` is gitignored). Design rationale and
operational detail: `tools/schedule/SPEC.md`; install with `tools/schedule/install.sh`.

## Canonical operations

- **Chief of Staff** — `wiki-cos` / `brain-cos`: cross-project daily brief, project status,
  commitment surface, inbox triage. Read-only; advises, never writes. Modes + launcher detail:
  `.claude/skills/wiki-agents/SKILL.md`.
- **Ingest** — raw/inbox → source page → concept/topic updates → links/lint/log. Full runbook:
  `.claude/skills/wiki-ingest/SKILL.md`.
- **Query** — `wiki-search` (general); for project-scoped Q&A launch a session from `projects/<slug>/`.
  Durable answers → `wiki/queries/` (general) or `projects/<slug>/queries/` (project).
- **Lint / health / archive / index** — programmatic checks via `wiki.py`; semantic checks via the
  `quality` / `contradict` / `verify` agents. Full command reference:
  `.claude/skills/wiki-maintenance/SKILL.md`. Write findings to `wiki/reports/`.
- **Projects** — scaffold/link/show + `project.md` lifecycle: `.claude/skills/wiki-projects/SKILL.md`.

## Conventions

**Links/citations:** write Obsidian path-based wikilinks `[[path/to/page]]` (the canonical form);
in a `## Sources` section, concept/topic pages cite the source *page* (`[[sources/...]]`), while a
**source page** (`wiki/sources/src-*.md`) cites its immutable raw material — `- Source text:
[[raw/sources-text/<stem>]]` (always present, linked without the `.md`) and, when a PDF exists,
`- Source PDF: [[raw/sources/<stem>.pdf]]`. (`raw/` wikilinks to real files are validated by `lint`;
filenames containing `[`/`]` use an angle-bracket markdown link `[Label](<../../raw/...>)` since
Obsidian wikilinks cannot contain `]`.) Keep external URLs on source
pages and reference sources indirectly from concept/topic pages. For portability outside Obsidian
(GitHub, plain-markdown viewers, headless agents) wikilinks carry a **dual-link** markdown mirror —
`[[concepts/foo]] ([Foo Title](../concepts/foo.md))`. Do **not** hand-write the `([Title](path.md))`
mirror (relative paths are error-prone); write the bare `[[...]]` and run
`python3 tools/wiki.py links --fix --write`, which adds mirrors deterministically and idempotently.
`python3 tools/wiki.py links` reports coverage without writing.

**Change quality:** preserve validated content unless superseded by stronger evidence; mark superseded
claims `status: superseded` (don't silently delete history); keep summaries concise + falsifiable;
favor incremental edits across related pages over isolated notes; bump `updated` when editing.

**Archiving:** retire pages with `archive page <ref> --reason "…"` — never delete (archived pages
keep wikilinks resolving but drop out of staleness/orphan checks and `search`). Full semantics:
`.claude/skills/wiki-maintenance/SKILL.md`.

**Index/log:** `wiki/index.md` (Dataview) updates itself inside Obsidian. The derived `_index.md`
mirrors are **generated — never hand-edit**; regenerate via the maintenance skill after
adding/removing pages. `wiki/log.md` is append-only; headings `## [YYYY-MM-DD] operation | title`.

## Search

[qmd](https://www.npmjs.com/package/@tobilu/qmd) is the primary engine — hybrid BM25 + vector +
LLM-rerank over `wiki/` and `raw/`. **All search-using agents prefer qmd over `wiki.py search` when
available** (see the devcontainer caveat in `## Working inside a project`). `qmd mcp` exposes
`mcp__qmd__*` tools (stdio; registered in `.mcp.json`). `python3 tools/wiki.py search "<query>"` is
the substring fallback that always works without setup. One-time host setup + re-index live in
`tools/scripts/setup-qmd.sh`; `qmd status` / `qmd collection list` for health.

## Obsidian skills

Prefer the `obsidian:` skill family for vault-native operations (`obsidian-markdown` for page
edits, `defuddle` for URL → clean markdown into `raw/inbox/`, `obsidian-cli` / `json-canvas` /
`obsidian-bases` as needed). There is no Obsidian MCP server. `obsidian-cli` and `defuddle` are
host-only (need the `obs` binary, a running Obsidian app, or network) — inside the egress-locked
sandbox use `obsidian-markdown` for formatting plus the normal file tools.

Templater auto-applies the matching `wiki/_templates/` template when a file is created in a `wiki/`
subfolder; Dataview tables update automatically from frontmatter (JS API enabled).

## Command index

Day-to-day core — everything else lives in the per-operation runbooks under
`## Canonical operations`:

```bash
python3 tools/wiki.py lint                       # fast health check (links, metadata, staleness)
python3 tools/wiki.py search "term"              # substring search (qmd preferred — see Search)
qmd search "<keywords>"                          # BM25; `qmd query "<question>" --format json` for hybrid
qmd update                                       # re-index after content changes
```
