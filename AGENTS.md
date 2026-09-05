# LLM Wiki Operating Schema

This vault implements the "LLM Wiki" pattern (after Karpathy's llm-wiki) as a persistent,
compounding knowledge base. This file is the provider-neutral source of truth for how AI agents
operate here. Claude Code loads it through `CLAUDE.md`; ChatGPT and Codex load it directly.

Multi-step **runbooks** (ingest, maintenance, projects, agents) live in `.agents/skills/*/SKILL.md`
and load automatically when relevant. Canonical custom-agent role bodies live in `.agents/roles/`;
`.claude/agents/` and `.codex/agents/` are generated provider adapters. This file keeps the
always-needed schema and rules; skills and roles hold procedures and specialized instructions.

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
4. **Schema** (`AGENTS.md`) — this file.

## Operator profile

If `wiki/entities/user-background.md` exists (referenced vault-wide as `[[user-background]]`), it is the
profile of the person this Brain serves: background, current focus, goals, and how they want agents to
work. Any agent producing advice, a brief, a status report, or project guidance for the operator should
read it first (it is qmd-indexed and surfaces on a `qmd search "operator profile"`) to calibrate tone and
priorities. It is gitignored (personal), so it ships only in this operator's vault, not the public
template. The Chief of Staff launcher injects it automatically into its live context.

Read `.agents/context-policy.md` when using live document context. The headless launcher applies
that shared policy to both providers and supplies live documents as task data, separately from
trusted role instructions. `VAULTLENS_COS_CONTEXT_CHARS` enables an experimental character budget
for Chief of Staff live context; it is unset by default pending model quality evaluation. It keeps
the complete profile, consent gate, project/desk overview and review-queue names. It scans all open
tasks before fair priority selection, reports source paths and omissions, and fails explicitly if
mandatory context cannot fit. See `tools/evals/README.md` for the fixture baseline and limitations.

## Directory contract

- `raw/sources/` immutable source docs · `raw/sources-text/` preprocessed PDF text (`preprocess` output) ·
  `raw/assets/` images/attachments · `raw/inbox/` approved files awaiting ingest ·
  `raw/review-inbox/` consent queue (agents may list names and sizes only, then must ask before reading, summarizing, moving, or ingesting)
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
  `tools/scripts/` setup helpers · `tools/tests/` tooling test suite · `tools/agents/wiki-agent.py`
  headless agent launcher · `tools/schedule/` host catch-up dispatcher for scheduled agents
- `.agents/roles/` canonical wiki-agent roles · `.agents/skills/` operational runbooks ·
  `.claude/` and `.codex/` provider-specific discovery and configuration adapters

## Page metadata and authoring

Wiki page frontmatter, link/citation format, change-quality rules, archiving, and index/log
conventions live in `wiki/AGENTS.md`. When a task launched from the repository root will read or
write `wiki/`, read that file before acting. Claude also loads it through `.claude/rules/`.

`projects/<slug>/project.md` needs: `wiki_refs` (the `[concepts/foo, topics/bar]` wikilinks the
project depends on), plus first-class `tags` and `domain` (used to scope wiki search to the project).

**PDF support:** PDFs are first-class raw sources — place in `raw/sources/` or `raw/inbox/`; the
model reads them directly and `wiki-ingest` extracts key claims into a source page. For large/complex
PDFs, `python3 tools/wiki.py preprocess` pre-extracts `raw/sources/*.pdf` → `raw/sources-text/*.md`.

## Tool permissions

Reads are auto-approved; writes require confirmation and the role's write profile. Interactive
clients apply their own controls. Headless roles are mapped by `wiki-agent.py`, and may never spawn
another agent. Tool allowlists are best-effort; the matching egress-locked container mount is the
hard read/write boundary. See `.devcontainer/README.md` and `.agents/skills/wiki-agents/SKILL.md`.

`raw/` may contain symlinks to files/dirs outside the vault, so existing data need not be duplicated.
Headless inbox previews never follow directory or file links, because those links could bypass
the review-inbox consent gate. Other authorized reads still follow the normal source workflow.

## Projects layer

`projects/` consumes the wiki as a knowledge base. Each subfolder is one project workspace that owns
its structure. The scaffold (`project new`) creates `project.md`, `AGENTS.md`, `CLAUDE.md`,
`TODO.md`, `AGENDA.md`, and `queries/`. `AGENTS.md` is the provider-neutral project entrypoint;
`CLAUDE.md` imports it for Claude Code.

Every project includes a dormant `AGENDA.md`. When enabled, the nightly runner executes only clear,
due work inside that project and snapshots it for review or undo. Ambiguous tasks go to the
interactive `wiki-project-clarify` flow. The complete project and agenda contract lives in
`.agents/skills/wiki-projects/SKILL.md`; read it before creating or restructuring a project.

Always-needed facts: `project.md` is the per-project source of truth — `wiki_refs` and `tags`
in its frontmatter are load-bearing (they scope which wiki pages agents pull into context; add refs
with `project link`, never hand-edit). Its `## Rules` section **overrides the defaults in
`## Working inside a project` when they conflict**. After any session that establishes new
information, update the changed `project.md` sections and bump `updated`.

`status: frozen` removes a project from current-work and routing surfaces without deleting it. Use
`project freeze <slug>` and `project unfreeze <slug>` so generated views stay synchronized;
`project list --include-frozen` is the audit view.

### Working inside a project (instructions for agents)

The project-session rules — wiki search ladder, citation discipline, durable-Q&A format, write
boundary, and project/wiki boundary rules — live in `projects/AGENTS.md`. A session launched inside
a project loads them automatically. A root-launched task must read that file before working in a
project. Project `## Rules` in `project.md` override them on conflict.

There is no dedicated project agent: launch a supported agent from inside `projects/<slug>/` and it
loads the root and project `AGENTS.md` chain. Claude Code reaches the same context through the local
`CLAUDE.md` compatibility import.

## Agent integration

Canonical custom roles live in `.agents/roles/`; provider adapters are generated. After a role
change, run `python3 tools/agents/generate-adapters.py`. Role selection, read/write behavior,
handoffs, models, effort, and launcher commands live in `.agents/skills/wiki-agents/SKILL.md`.

## Devcontainer sandbox

Agents run in the hardened, egress-locked devcontainer. Launch them from the host with `brain-wiki`
or `brain-cos`; `wiki-agent.py` refuses host execution. Provider state, mount profiles, configuration
sync, and troubleshooting are documented in `.devcontainer/README.md`. Repository configuration in
this file, `.agents/`, `.claude/`, and `.codex/` lives in the mounted workspace.

## Scheduled agents

The host catch-up dispatcher runs one gated nightly batch and records dated reports. It applies
project-runner edits without committing and keeps pre-run snapshots. Broad wiki enhancement is
opt-in. Scheduling, recovery, gates, provider selection, and installation are defined in
`tools/schedule/SPEC.md` and `tools/schedule/install.sh`.

## Canonical operations

- **Chief of Staff** — `wiki-cos` / `brain-cos`: cross-project daily brief, project status,
  commitment surface, inbox triage. Read-only; advises, never writes. Modes + launcher detail:
  `.agents/skills/wiki-agents/SKILL.md`.
- **Ingest** — raw/inbox → source page → concept/topic updates → links/lint/log. Full runbook:
  `.agents/skills/wiki-ingest/SKILL.md`.
- **Query** — `wiki-search` (general); for project-scoped Q&A launch a session from `projects/<slug>/`.
  Durable answers → `wiki/queries/` (general) or `projects/<slug>/queries/` (project).
- **Lint / health / archive / index** — programmatic checks via `wiki.py`; semantic checks via the
  `quality` / `contradict` / `verify` agents. Full command reference:
  `.agents/skills/wiki-maintenance/SKILL.md`. Write findings to `wiki/reports/`.
- **Projects** — scaffold/link/show + `project.md` lifecycle: `.agents/skills/wiki-projects/SKILL.md`.

## Search

[qmd](https://www.npmjs.com/package/@tobilu/qmd) is the primary engine — hybrid BM25 + vector +
LLM-rerank over `wiki/` and `raw/`. **All search-using agents prefer qmd over `wiki.py search` when
available** (see the devcontainer caveat in `projects/AGENTS.md`). `qmd mcp` exposes
`mcp__qmd__*` tools (stdio; registered for Claude in `.mcp.json` and for Codex in
`.codex/config.toml`). `python3 tools/wiki.py search "<query>"` is
the substring fallback that always works without setup. One-time host setup + re-index live in
`tools/scripts/setup-qmd.sh`; `qmd status` / `qmd collection list` for health.

In Codex cloud (`CODEX_SESSION_ENV=cloud`), lead with `qmd search "<keywords>"`. Cloud setup builds
the keyword index but intentionally does not download or build semantic embeddings. Do not use
`qmd query`, `qmd vsearch`, or the equivalent semantic MCP tools until `qmd embed` has completed
successfully in that environment.

## Obsidian skills

Prefer the `obsidian:` skill family for vault-native operations (`obsidian-markdown` for page
edits, `defuddle` for URL → clean markdown into `raw/inbox/`, `obsidian-cli` / `json-canvas` /
`obsidian-bases` as needed). There is no Obsidian MCP server. `obsidian-cli` and `defuddle` are
host-only (need the `obs` binary, a running Obsidian app, or network) — inside the egress-locked
sandbox use `obsidian-markdown` for formatting plus the normal file tools.

## Command index

Day-to-day core — everything else lives in the per-operation runbooks under
`## Canonical operations`:

```bash
python3 tools/wiki.py lint                       # fast health check (links, metadata, staleness)
python3 tools/wiki.py search "term"              # substring search (qmd preferred — see Search)
qmd search "<keywords>"                          # BM25; `qmd query "<question>" --format json` for hybrid
qmd update                                       # re-index after content changes
```

## Cloud sessions

Run `bash .codex/cloud/setup.sh` as the Codex cloud environment setup command. Cloud sessions work
only with the tracked VaultLens template and example content. Brain is private, local-only, and
must never be copied, mounted, fetched, indexed, or inferred in a cloud session. The bootstrap
builds only the keyword index; semantic embeddings remain an explicit optional step. In cloud
sessions, do not publish with shell Git commands, configure Git credentials, or create a pull
request with `gh`. The platform-managed **Open pull request** action may create a pull request, and
the connected GitHub integration may update the same branch for pull-request-linked follow-ups.
When the user explicitly requests it, that integration may merge the pull request after all
required checks and approvals pass and no blocking review remains. Do not use an admin bypass or
directly update a default or protected branch outside that approved merge.
