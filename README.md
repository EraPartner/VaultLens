# VaultLens

**A self-hosted "LLM wiki" — a compounding, agent-maintained knowledge base you run in Obsidian with ChatGPT/Codex or Claude Code.**

VaultLens is a system template (after [Karpathy's llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f))
for turning a pile of source material into a durable, cross-linked knowledge base that
agents grow and curate over time — and then *consuming* that knowledge base from project
workspaces. You drop sources into `raw/`; agents distil them into a curated `wiki/`; your
`projects/` read from the wiki without ever writing back to it.

Clone it, point Obsidian at it, and start a ChatGPT/Codex or Claude Code session.
`AGENTS.md` is the shared operating schema for these agents.
Claude Code reads `AGENTS.md` natively (v2.1.277 or newer), so the repo has no `CLAUDE.md`
files; adding one makes Claude read it instead of `AGENTS.md`. Keep operating instructions in
`AGENTS.md`.

## Architecture — four layers

Dependencies flow left → right; each layer consumes the one before it and never writes back.

```
raw/            ← immutable ingested sources (source of truth)
  → wiki/       ← curated, LLM-generated knowledge base (agent-owned)
    → projects/ ← application workspaces that consume the wiki
AGENTS.md       ← the provider-neutral operating schema that governs all of it
```

- **`raw/`** — immutable source docs (articles, PDFs, papers, notes). Normal ingest never modifies it.
- **`wiki/`** — the curated layer, owned by the agents: source pages, entities, concepts, topics,
  syntheses, comparisons, preserved Q&A, and reports. `wiki/home.md` + `wiki/SETUP.md` are the
  reader-facing entry points; as content accrues, the agents maintain `wiki/index.md` (a Dataview
  catalog) and the append-only `wiki/log.md` as the mandatory navigation files.
- **`projects/`** — application workspaces that reference wiki pages but **must not** write to
  `wiki/` or `raw/`.
- **`AGENTS.md`** — the source of truth for how any supported agent operates in the vault.

## What's Included

- `AGENTS.md` — provider-neutral operating schema for LLM agents (start here)
- `.agents/roles/` — 12 canonical wiki-agent role definitions
- `.agents/skills/` — shared operational runbooks (ingest, maintenance, projects, agent selection)
- `.claude/agents/` and `.codex/agents/` — generated, thin provider adapters
- `raw/` — source-of-truth ingest area (immutable inputs)
- `wiki/` — the curated knowledge base + page templates
- `projects/` — application workspaces that consume the wiki
- `tools/` — the `wiki.py` CLI (lint, search, ingest, index, links, projects…) plus the
  scheduled-agent dispatcher in `tools/schedule/`
- `.mcp.json` and `.codex/config.toml` — register [qmd](https://www.npmjs.com/package/@tobilu/qmd) for hybrid search.
  A plain `claude` or `codex` session started from the repo runs this unscoped, full-vault qmd server;
  runs launched through the `brain-*` wrappers search only the files their access profile approves
- `tools/local_runtime.py` and `tools/access-profiles.json` — local process isolation and versioned privacy policy
- `.gitignore` — excludes your data, keeps the system

## What the system does

Beyond the folder skeleton, the template ships a working agent operating model:

- **Ingest** — drop a file or URL in `raw/inbox/`; the `wiki-ingest` agent extracts claims into a
  source page and threads them into concept/topic pages, with links and a lint pass. PDFs are
  first-class (read directly; large ones pre-extracted to `raw/sources-text/`).
- **A fleet of wiki agents** (`.agents/roles/`) — `wiki-ingest`, `wiki-enhancer`,
  `wiki-source-verifier`, `wiki-quality-reviewer`, `wiki-contradiction-detector`, `wiki-search`,
  the read-only thinking agents `wiki-challenge` / `wiki-connect` / `wiki-emerge` /
  `wiki-idea-discovery`, plus a **Chief of Staff** (`wiki-cos`) that produces cross-project briefs.
  Invoke them by name in a session; the `wiki-agents` skill helps pick the right one.
- **Projects layer** — scaffold workspaces that consume the wiki (details below). Each carries a
  dormant `AGENDA.md`; opt in and the nightly **`wiki-project-runner`** grooms and executes its
  due tasks inside `projects/<slug>/` (applied-not-committed, with a snapshot for undo).
- **Scheduled agents** — a host-side catch-up dispatcher (`tools/schedule/`) runs the
  maintenance/thinking agents on a launchd tick and files dated outputs under `wiki/reports/`.
  Broad nightly wiki enhancement is paused by default and requires explicit opt-in; when enabled,
  it runs five alternating iterations across the whole wiki.
- **Scoped search** — agent CLI and MCP search use a fresh lexical corpus of approved files with
  qmd-compatible tool names. Explicit operator search can still use qmd's full hybrid index;
  `python3 tools/wiki.py search "…"` is the substring fallback.
- **Local agent runs** — interactive, headless, and scheduled agents run the native Claude or Codex
  CLI inside Anthropic's whole-process sandbox runtime. Access profiles govern read, write, and
  network access. Each provider uses separate login state. No container is launched.

Operating detail lives in `AGENTS.md` and the runbooks under `.agents/skills/` — this README stays
at the overview altitude.

## Switching providers

Select the provider once in the vault you use:

```bash
python3 tools/llm_provider.py select claude
python3 tools/llm_provider.py select codex
# Optional: remember a model separately for each provider.
python3 tools/llm_provider.py select claude --model sonnet
python3 tools/llm_provider.py show
```

The local preference lives in gitignored `tools/llm.local.json`. The host launch planner,
headless agents, and scheduler share it. Explicit `--cli` / `--model` flags override environment
variables, which override the saved preference. Without a preference, both providers use their
native model default. No automatic provider fallback occurs.

Wiki roles keep provider-neutral `model_profile` (`standard` or `deep`) and
`reasoning_effort` metadata. Both headless runs and generated interactive adapters use it.
The tracked mappings in `tools/model-profiles.json` map Claude's profiles to `sonnet` and
`opus`. Codex maps `standard` to `gpt-6-luna` and `deep` to `gpt-6.1-sol`.
Both providers also apply role-specific reasoning effort.

Override mappings locally in gitignored `tools/llm.local.json`, without changing role bodies:

```json
{
  "profiles": {
    "claude": {"standard": "sonnet", "deep": "opus"},
    "codex": {"standard": "gpt-6-luna", "deep": "gpt-6.1-sol"}
  }
}
```

Model names are opaque provider values; an empty string means native model selection.
For headless work, explicit `--model` overrides `VAULTLENS_LLM_MODEL`, saved per-provider
`models`, local `profiles`, and tracked mappings, in that order. Explicit `--effort` overrides
the canonical role effort. Scheduled jobs keep their deliberate effort overrides and freeze
resolved role models for the batch. Plain interactive sessions retain their native/global default.

Generated interactive adapters are configuration snapshots. They use the tracked per-provider
mappings in `tools/model-profiles.json`, not the launch-scoped environment override or your
local `llm.local.json` (use `generate-adapters.py --output-dir <dir> --local-models` for a
personal copy that applies it). Regenerate after changing mappings or roles:
`python3 tools/agents/generate-adapters.py`. A concrete adapter model/effort takes precedence
over the parent session; use an empty model mapping to inherit the parent model.

Headless Claude runs use an explicit built-in tool list, separate from scoped approval rules.
Only the scoped search server is configured for an agent run; inherited host MCP integrations are
excluded. Unattended Claude runs disable session transcript persistence, matching Codex's ephemeral
runs. Scheduler reports and logs remain available for audit.

The project runner can execute Python scripts inside its selected project. Web research requires
a separate access profile with explicit research domains. Model selection and tool grants do not
widen the process boundary. Writer runs retain recoverable snapshots for review and undo.

`tools/shell/` contains host fish wrappers: `brain-provider`
changes this preference, `brain-agent` starts the selected interactive CLI, and `brain-wiki` /
`brain-cos` select it for headless work. `brain-claude` and `brain-codex` explicitly choose a CLI.
`brain-shell` opens a shell through the same runtime. Root discovery requires `AGENTS.md`,
`tools/wiki.py`, and `tools/agents/wiki-agent.py`; the nearest matching checkout wins. `BRAIN_HOME`
provides the fallback vault. Container image markers and private container launchers are not used.
Preview host wrapper updates with `python3 tools/scripts/repair-provider-host.py --vault /path/to/Brain`.
Scheduled batches freeze their provider and model when they start. An explicitly pinned provider
in an installed LaunchAgent overrides the shared preference; see `tools/schedule/SPEC.md`.

## Quick Setup

Host tooling requires Python 3.11 or newer. CI uses Python 3.12.
On macOS, use Homebrew Python if the system `python3` is older.
Host commands reject older interpreters before doing work. Set `BRAIN_PYTHON`
to an absolute Python executable for the fish wrappers and scheduler installer;
the installer records that executable in the LaunchAgent. Direct commands must
use the supported executable explicitly, for example `/opt/homebrew/bin/python3`.

```bash
# Clone this template
git clone https://github.com/EraPartner/VaultLens.git my-wiki
cd my-wiki

# Repair the fixed scaffold and create local navigation files.
# This is idempotent and never overwrites existing files.
python3 tools/wiki.py init

# Open in Obsidian
open .
```

Run initialization on the host. `brain-wiki init` runs this same deterministic operation directly;
it does not invoke a model or require provider authentication.

`raw/inbox/` is for approved ingest candidates. `raw/review-inbox/` is a consent queue for material
that is only of interest: agents must ask before reading or processing an item. The qmd setup script
excludes that queue from lexical, vector, and hybrid search.

## Projects Layer

The `projects/` directory is an application layer on top of the wiki. Each subfolder is one project
workspace that consumes the wiki as a knowledge base **without ever writing to it**.

Each project has a `project.md` declaring its description, layout, rules, and linked wiki pages. The
scaffold also drops a project `AGENTS.md`, so each agent picks up the project's context
and the root schema (`## Working inside a project`).

### Scaffold a project

```bash
python3 tools/wiki.py project new my-project                              # create a project
python3 tools/wiki.py project link my-project concepts/trusted-execution  # link wiki pages into it
python3 tools/wiki.py project list                                       # list non-frozen projects
python3 tools/wiki.py project list --include-frozen                      # audit all projects
python3 tools/wiki.py project show my-project                             # inspect structure
python3 tools/wiki.py project freeze my-project                          # hide current work everywhere
python3 tools/wiki.py project unfreeze my-project                        # restore as active
```

### Work inside a project

`cd` into `projects/<slug>/` and start ChatGPT/Codex or Claude Code. The project's `AGENTS.md`
requires reading `project.md`. The root `## Working inside a project` section defines the wiki search ladder, citation discipline, and the
Q&A artifact convention. Durable Q&A lands in `projects/<slug>/queries/` by default, redirectable
via `## Rules` in `project.md`.

### `project.md` schema

```yaml
---
type: project
title: My Thesis
status: active
tags: [tee, sgx]
domain: research
wiki_refs:
  - concepts/trusted-execution-environments
  - topics/remote-attestation
---

## Description
...

## Layout
projects/my-project/
  project.md     ← metadata, description, layout, rules, wiki refs
  AGENTS.md      ← provider-neutral project instructions (auto-generated)
  TODO.md        ← per-project todo; embedded into projects/TODO.md (auto-generated)
  queries/       ← Q&A artifacts
  papers/        ← relevant PDFs
  meetings/      ← dated meeting notes

## Rules
- Never modify raw/ or wiki/ — treat them as read-only.
- Save all Q&A artifacts to queries/.
```

Project status is `active`, `paused`, `frozen`, or `archived`. `frozen` keeps the
workspace intact but excludes it from active lists, TODO/deadline views, briefs,
scheduled agents, and routed work. Direct `project show` remains available.

## Local runtime and privacy profiles

Install the pinned sandbox runtime with `bash tools/runtime/install.sh`. It requires Node.js and
macOS Seatbelt or Linux bubblewrap, socat, and ripgrep. The runtime is a research preview, so a
package check alone does not establish isolation. The launcher fails closed if the reviewed
runtime or operating-system prerequisites are missing. It has no unsandboxed fallback.

All runtime launches also require a current successful synthetic probe receipt. Running a new
probe revokes the older receipt; any failed or skipped required check leaves launches disabled.
Review the [runtime guide](tools/runtime/README.md) for installation and verification gates.
Provider authentication uses a dedicated per-vault store outside the vault and iCloud sync;
[authenticate afresh](tools/runtime/README.md#provider-authentication) through the scoped runtime.
Host and legacy login state is never imported automatically.

Inspect available profiles and a resolved scope before launching an agent:

```bash
python3 tools/local_runtime.py profiles
python3 tools/local_runtime.py plan --profile selected-read --read-path wiki/concepts/example.md
python3 tools/local_runtime.py plan --profile project-write --project my-project
python3 tools/local_runtime.py doctor
```

The tracked `tools/access-profiles.json` policy has a schema version, named profiles, and role
defaults. A gitignored `tools/access.local.json` stores operator choices. Local profile definitions
replace matching tracked definitions. `extends` merges read paths, write paths, deny paths, and
research domains with a parent; inherited grants are additive. Deny paths and mandatory protected
paths take precedence. Unknown fields, cycles, path traversal, symbolic links, and invalid scopes
stop the launch.

| Profile | Read access | Note writes |
| --- | --- | --- |
| `selected-read` | Explicit `--read-path` selections and trusted instructions | Reports only |
| `wiki-read` | Wiki pages | Reports only |
| `source-read` | Wiki and approved source folders | Reports only |
| `cos-read` | Wiki, project planning files, approved inbox; consent queue metadata only | Reports only |
| `wiki-write` | Wiki, approved sources, approved inbox | Wiki |
| `project-write` | Wiki and exactly one project | That project |

For a private reporting task, extend `selected-read` instead of inheriting the whole wiki:

```json
{
  "version": 1,
  "profiles": {
    "project-report": {
      "extends": "selected-read",
      "read": ["projects/my-project/project.md", "projects/my-project/TODO.md"],
      "deny_read": ["wiki/entities/user-background.md"],
      "reports": "wiki/reports/agents/my-project",
      "research_domains": []
    }
  },
  "defaults": {"search": "project-report"}
}
```

Use `--access-profile project-report` with `brain-agent`, `brain-claude`, `brain-codex`, or a
headless `brain-wiki` role. Repeatable `--read-path` adds approved selections to the chosen profile;
choose `selected-read` when only those selections should be visible. Interactive launches in a
recognized project default to `project-write`; other interactive launches default to `wiki-read`
for reporting. Root wiki editing requires an explicit `--access-profile wiki-write`.
Plain `brain-shell` uses the same defaults and explicit editing option.

Profiles keep sources, the consent queue, tools, agent instructions, Obsidian configuration, and
Git metadata protected from agent writes. A clean per-run environment excludes inherited cloud
credentials, SSH sockets, unrelated home files, and host hooks. Only selected provider state and
model/login endpoints are available; research domains must be added explicitly to a separate
profile. Model and provider changes do not change note scope.

Agent search builds its corpus from the resolved scope and starts its server inside the same
boundary. It does not read the shared host qmd index. Local execution still sends selected notes
to the model provider; a read-only grant is also a disclosure grant. Keep profile selections small
and review reports and writer snapshots before accepting automated changes.

Portable tests cover profile resolution, launch routing, provider command construction, and
scoped search. Run `tools/runtime/probe.py` on a synthetic fixture to check actual filesystem,
process, and network confinement on the deployment host. Authentication and live provider calls
are separate checks. Do not describe portable tests as proof of runtime isolation.

## See Also

- **`AGENTS.md`** — the full operating schema (directory contract, page metadata, conventions, agents, search).
- Original inspiration: Karpathy's [llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
