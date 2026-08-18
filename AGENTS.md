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

Reads are auto-approved; writes require explicit confirmation. Enforcement is layered:

| Operation | Policy |
|---|---|
| Read files anywhere in the vault | auto-approved |
| Read-only shell (`ls`, `find`, `grep`, `cat`, `head`, `tail`, `wc`, `sort`, `uniq`, `cut`, `tr`, `date`, `python3`, `qmd`) | auto-approved |
| Write shell (`touch`, `mkdir`, `mv`, `cp`, `sed`, `awk`) | auto-approved for write-access agents only |
| Write or edit files | requires confirmation |

- Interactive Claude sessions use `.claude/settings.json`; interactive Codex sessions use the
  active Codex sandbox and approval policy. Provider adapters under `.claude/agents/` and
  `.codex/agents/` map the neutral role permission profile to each client's controls.
- Headless runs use `wiki-agent.py`, which maps the same role profile to Claude tool allowlists or
  Codex sandbox settings. A wiki agent must never spawn another agent; the launcher disables that
  capability for both backends.
- The egress-locked devcontainer mount (see `## Devcontainer sandbox`) is the kernel-level backstop.

`raw/` may contain symlinks to files/dirs outside the vault; they're followed automatically by the
model and the wiki tools, so existing data need not be duplicated.

## Projects layer

`projects/` consumes the wiki as a knowledge base. Each subfolder is one project workspace that owns
its structure. The scaffold (`project new`) creates `project.md`, `AGENTS.md`, `CLAUDE.md`,
`TODO.md`, `AGENDA.md`, and `queries/`. `AGENTS.md` is the provider-neutral project entrypoint;
`CLAUDE.md` imports it for Claude Code.

**Autonomous runner:** every project carries a dormant `AGENDA.md` (loose `## Inbox` + groomed
`## Tasks`). Flip its frontmatter `enabled: true` to opt the project into the nightly `project-runner`
agent, which grooms loose tasks into a clear structured form, executes the ones that are 100% clear
and due (writing only inside `projects/<slug>/`, applied-not-committed with a pre-run snapshot for
undo), and files clarifications for anything ambiguous. Resolve those interactively with
`/wiki-project-clarify`. See `## Scheduled agents` and the runbook below.

**Runbook:** scaffolded structure, the `project.md` page schema, `project new/link/show` usage,
keeping `project.md` current, the TODO.md format/aggregators, and the `AGENDA.md` schema +
`project agenda` subcommands live in `.agents/skills/wiki-projects/SKILL.md` — read it before
creating or restructuring a project.

Always-needed facts: `project.md` is the per-project source of truth — `wiki_refs` and `tags`
in its frontmatter are load-bearing (they scope which wiki pages agents pull into context; add refs
with `project link`, never hand-edit). Its `## Rules` section **overrides the defaults in
`## Working inside a project` when they conflict**. After any session that establishes new
information, update the changed `project.md` sections and bump `updated`.

### Working inside a project (instructions for agents)

The project-session rules — wiki search ladder, citation discipline, durable-Q&A format, write
boundary, and project/wiki boundary rules — live in `projects/AGENTS.md`. A session launched inside
a project loads them automatically. A root-launched task must read that file before working in a
project. Project `## Rules` in `project.md` override them on conflict.

There is no dedicated project agent: launch a supported agent from inside `projects/<slug>/` and it
loads the root and project `AGENTS.md` chain. Claude Code reaches the same context through the local
`CLAUDE.md` compatibility import.

## Agent integration

For complex wiki tasks use the custom roles in `.agents/roles/`: `wiki-ingest`,
`wiki-enhancer`, `wiki-source-verifier`, `wiki-quality-reviewer`, `wiki-contradiction-detector`,
`wiki-search`, plus the read-only thinking agents `wiki-challenge` / `wiki-connect` / `wiki-emerge` /
`wiki-idea-discovery` — and two more with their own operating modes described elsewhere in this file:
`wiki-cos` (Chief of Staff — see `## Canonical operations`) and `wiki-project-runner` (nightly
autonomous task execution — see `## Scheduled agents`). Claude and Codex discover generated native
adapters for these roles; headless and batch runs go through `tools/agents/wiki-agent.py`
(host: `brain-wiki`), which adds the enhance loops, CoS live-context gathering, PDF pre-extraction,
and auto-logging.

Role bodies and neutral metadata are edited only in `.agents/roles/`. After changing them, run
`python3 tools/agents/generate-adapters.py`; CI uses `--check` to reject stale Claude/Codex adapters.

**Runbook:** the what-agent-for-what table, reads/writes + handoffs, thinking-agent flags,
`wiki-agent.py` invocations, and model/effort options live in
`.agents/skills/wiki-agents/SKILL.md` — read it before picking or launching an agent.

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

The local LockBox-derived tooling container supports Claude and Codex through separate provider
volumes. The external Brain `brain-wiki` container integration remains a separate migration.

**Inside the devcontainer (`$DEVCONTAINER=true`):** `~/.claude/` and `~/.claude.json` are an isolated
copy, host-pulled on start but **not** pushed back automatically. If you change in-container Claude
config (agents, plugins, slash commands, hooks, MCP servers, rules, settings), tell the user before
ending your turn to run on the host: `brain-claude-sync push` (it backs up `~/.claude.json` before a
newer-wins merge). Without it the change is lost on the next container rebuild. Repo-level config —
  this file, `.agents/`, `.claude/`, `.codex/` — lives in the mounted
workspace and needs no sync. Outside the devcontainer this does not apply.

## Scheduled agents

A host-side **catch-up dispatcher** (`tools/schedule/`) runs the maintenance/thinking agents on a
~30-minute launchd tick; each tick is a gate-checker, not an LLM trigger — all LLM work runs in one
nightly batch (AC-only, defer-until-online) on the configured CLI. Claude remains the default for
backward compatibility; `VAULTLENS_LLM_CLI=codex` uses the Codex-capable local tooling path.
Read-only agents stay read-only:
outputs are filed as dated reports under `wiki/reports/`. The one **writer** in the nightly batch is
`project-runner` (runs before `enhance`): for each opted-in project it executes due `AGENDA.md` tasks
inside `projects/<slug>/` (applied-not-committed; the dispatcher clones the project first, so the
roll-up's restore command is the undo since `projects/` is gitignored). Design rationale and
operational detail: `tools/schedule/SPEC.md`; install with `tools/schedule/install.sh`.

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
sessions, do not commit, sign, tag, push, configure Git credentials, or create a pull request with
`gh`; leave the diff for Codex's **Open pull request** action.
