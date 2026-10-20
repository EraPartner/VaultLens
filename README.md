# VaultLens

**An agent-maintained "LLM wiki" for Obsidian. You collect sources; Claude Code or Codex turns
them into a cross-linked knowledge base that compounds over time, and your projects draw on it.**

VaultLens is a template, after [Karpathy's llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
It ships the system (CLI tooling, agent roles, runbooks, provider adapters, a sandboxed agent
runtime and a scheduler) and none of your content: the `.gitignore` tracks the system and ignores
the data. Licensed [AGPL-3.0-only](LICENSE).

- **Provider-neutral.** One operating schema, [`AGENTS.md`](AGENTS.md), drives both Claude Code and
  Codex. Switch providers with one command; there is no automatic fallback.
- **Layered and one-directional.** Sources are immutable, the wiki is agent-owned, and projects
  read the wiki but never write to it.
- **Scoped by default.** Every agent launch runs inside a versioned access profile that fixes what
  it may read, write and reach on the network, enforced by an OS-level sandbox with no unsandboxed
  fallback.
- **Plain files.** Markdown with frontmatter, wikilinks and Dataview. The tooling is a
  stdlib-only Python CLI.

## How it works

```
raw/            immutable sources: articles, PDFs, papers, notes (source of truth)
  → wiki/       curated, agent-generated knowledge base (agent-owned)
    → projects/ workspaces that consume the wiki, never write back
AGENTS.md       the operating schema that governs all three
```

1. **Collect.** Drop a file into `raw/inbox/` (approved for ingest) or `raw/review-inbox/` (a
   consent queue: agents may list names and sizes, then must ask before reading anything there).
2. **Ingest.** The `wiki-ingest` agent writes a source page under `wiki/sources/`, threads its
   claims into `entities/`, `concepts/` and `topics/` pages, then fixes links, lints and appends
   to `wiki/log.md`. PDFs are first-class; `python3 tools/wiki.py preprocess` pre-extracts large
   ones to `raw/sources-text/`.
3. **Maintain.** Read-only reviewer agents check quality, verify pages against sources, flag
   contradictions and surface patterns. Their findings land in `wiki/reports/`.
4. **Use.** Start a session inside `projects/<slug>/`. The agent reads that project's
   `project.md`, searches the wiki pages it links to, cites them, and saves durable answers to the
   project's `queries/`.

`wiki/home.md` and `wiki/SETUP.md` are the reader-facing entry points in Obsidian;
`wiki/index.md` (a Dataview catalog) and `wiki/log.md` are the navigation files agents maintain.

## Quick start

**Requirements**

- Python 3.11 or newer (CI uses 3.12). On macOS, use Homebrew Python if the system `python3` is
  older; the CLI refuses to run on older interpreters.
- [Obsidian](https://obsidian.md) with the Dataview and Templater plugins.
- To run agents: a native [Claude Code](https://claude.com/claude-code) or Codex CLI, Node.js
  22.12 or newer, and macOS Seatbelt or (on Linux) bubblewrap, socat and ripgrep.
- Optional: [`fish`](https://fishshell.com) for the `brain-*` wrappers, and
  [qmd](https://www.npmjs.com/package/@tobilu/qmd) for ranked search.

**1. Create a vault**

```bash
git clone https://github.com/EraPartner/VaultLens.git my-wiki
cd my-wiki
python3 tools/wiki.py init     # idempotent; creates wiki/index.md, wiki/log.md and projects/TODO*.md
python3 tools/wiki.py lint     # fast health check
```

Open the folder as a vault in Obsidian. These commands are deterministic and need no model or
provider login.

**2. Install and verify the agent runtime** (once per host)

```bash
bash tools/runtime/install.sh           # pinned @anthropic-ai/sandbox-runtime, via npm ci
python3 tools/runtime/probe.py          # synthetic isolation probe on this host
python3 tools/local_runtime.py doctor   # launch readiness
```

Every agent launch requires a current, successful probe receipt. A failed or skipped check
leaves launches disabled, and changes to the runtime, launcher sources, access policy or OS
require a fresh probe. Then [authenticate the provider](tools/runtime/README.md#provider-authentication)
through the scoped runtime; host login state is never imported.

**3. Pick a provider and start working**

```bash
python3 tools/llm_provider.py select claude     # or: codex
python3 tools/llm_provider.py show
```

With the fish wrappers from `tools/shell/` on your path:

```bash
brain-agent                          # interactive session with the selected CLI
brain-wiki ingest --source raw/inbox/paper.pdf
brain-wiki quality --page wiki/concepts/example.md
brain-cos                            # Chief of Staff brief across projects
```

`brain-claude` and `brain-codex` pick a CLI explicitly, `brain-shell` opens a scoped shell, and
`brain-provider` changes the saved provider. Outside a VaultLens checkout, set `BRAIN_HOME` to
the vault; set `BRAIN_PYTHON` to an absolute Python 3.11+ path if `python3` is too old.

## Agents

Twelve canonical roles live in [`.agents/roles/`](.agents/roles); provider adapters in
`.claude/agents/` and `.codex/agents/` are generated from them. Headless runs use
`brain-wiki <agent>` (or `python3 tools/agents/wiki-agent.py <agent>`).

| Agent | Role | Writes |
| --- | --- | --- |
| `ingest` (`wiki-ingest`) | Turn a raw source into source, concept and topic pages | Wiki |
| `enhance` (`wiki-enhancer`) | Re-read sources to deepen sparse pages; loop strategies (`coverage`, `random`, `stub`, `source-gap`, `auto`, `alternate`) | Wiki |
| `verify` (`wiki-source-verifier`) | Check a page's claims against its raw source | Reports |
| `quality` (`wiki-quality-reviewer`) | Source-blind review of one page's consistency and structure | Reports |
| `contradict` (`wiki-contradiction-detector`) | Find conflicting claims across pages | Reports |
| `search` (`wiki-search`) | Search and synthesise cited answers | Reports |
| `challenge`, `connect`, `emerge`, `discover` | Red-team a decision, bridge two domains, surface unnamed patterns, rank what to work on next | Reports |
| `cos` (`wiki-cos`) | Chief of Staff: daily brief, status, commitments, inbox triage | Reports |
| `project-run` (`wiki-project-runner`) | Nightly runner for one opted-in project's `AGENDA.md` | That project |

Operational runbooks in [`.agents/skills/`](.agents/skills) (`wiki-ingest`, `wiki-maintenance`,
`wiki-projects`, `wiki-project-clarify`, `wiki-agents`) load automatically in a session when
relevant. Invoked by name inside an interactive session, a role runs as a subagent in the
parent's boundary; a narrower role prompt is not separate isolation.

## Projects

Each folder under `projects/` is a workspace that consumes the wiki. `project new` scaffolds
`project.md`, `AGENTS.md`, `TODO.md`, a dormant `AGENDA.md` and `queries/`.

```bash
python3 tools/wiki.py project new my-project
python3 tools/wiki.py project link my-project concepts/trusted-execution
python3 tools/wiki.py project show my-project
python3 tools/wiki.py project list [--include-frozen]
python3 tools/wiki.py project freeze my-project      # hide from current-work views; unfreeze restores
```

`project.md` is the project's source of truth. Its `wiki_refs`, `tags` and `domain` frontmatter
scope which wiki pages agents pull into context (add refs with `project link`, not by hand), and
its `## Rules` section overrides the default project-session rules in
[`projects/AGENTS.md`](projects/AGENTS.md). Status is `active`, `paused`, `frozen` or `archived`.

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
```

Set `enabled: true` in a project's `AGENDA.md` to opt it into the nightly runner. The runner
executes only clear, due tasks inside that project, applies edits without committing, keeps a
snapshot for undo, and files anything ambiguous for the `wiki-project-clarify` skill.

## Privacy and access profiles

Agents run the native Claude or Codex CLI inside Anthropic's
[standalone sandbox runtime](tools/runtime/README.md) (pinned in `tools/runtime/package-lock.json`).
No container is used. Policy lives in [`tools/access-profiles.json`](tools/access-profiles.json):

| Profile | Read access | Writes |
| --- | --- | --- |
| `selected-read` | Explicit `--read-path` selections and trusted instructions | Reports only |
| `wiki-read` | Wiki pages | Reports only |
| `source-read` | Wiki and approved source folders | Reports only |
| `cos-read` | Wiki, project planning files, approved inbox; consent-queue metadata only | Reports only |
| `wiki-write` | Wiki, approved sources, approved inbox | Wiki |
| `project-write` | Wiki and exactly one project | That project |

Interactive launches inside a project default to `project-write`, other interactive launches to
`wiki-read`; editing the wiki from the root needs an explicit `--access-profile wiki-write`.
Inspect a resolved scope before launching:

```bash
python3 tools/local_runtime.py profiles
python3 tools/local_runtime.py plan --profile project-write --project my-project
```

Operator overrides go in gitignored `tools/access.local.json`. A local profile can `extends` a
tracked one; grants are additive, while deny paths and protected paths always win. Unknown fields,
cycles, path traversal, symlinks and invalid scopes stop the launch. For example, a narrow
reporting profile:

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

What every profile guarantees:

- Sources, the consent queue, tooling, agent instructions, Obsidian config, `wiki/_templates/`
  and Git metadata are protected from agent writes.
- Each run gets a clean environment without inherited cloud credentials, SSH sockets, unrelated
  home files or host hooks. Only the selected provider's login state and endpoints are reachable;
  web research needs a separate profile that lists its domains.
- Agent search builds its corpus from the resolved scope only and never reads the shared host
  qmd index directly. `qmd query` ranking goes through a host bridge that returns approved paths
  only (disable with `VAULTLENS_QMD_BRIDGE=off`); `python3 tools/wiki.py search` is the substring
  fallback.
- Templater's creation trigger is off and headless reports escape `<%` markers, because Templater
  executes template code on the host.

Local execution still sends the selected notes to the model provider: a read grant is a
disclosure grant. Keep selections small, and review reports and writer snapshots before accepting
automated changes. Portable tests cover profile resolution, routing and command construction; only
`tools/runtime/probe.py` on the deployment host checks real filesystem, process and network
confinement.

## Models and providers

The saved provider lives in gitignored `tools/llm.local.json` and is shared by interactive
launches, headless agents and the scheduler. Explicit `--cli` / `--model` flags beat environment
variables, which beat the saved preference.

Roles declare a provider-neutral `model_profile` (`standard` or `deep`) and `reasoning_effort`.
[`tools/model-profiles.json`](tools/model-profiles.json) maps them per provider:

| Provider | `standard` | `deep` |
| --- | --- | --- |
| Claude | `sonnet` | `opus` |
| Codex | `gpt-6-luna` | `gpt-6.1-sol` |

Override the mapping locally under `"profiles"` in `tools/llm.local.json`; an empty string means
the provider's native default. `--effort` overrides a role's effort. Generated interactive adapters
use only the tracked mapping, so regenerate them after changing roles or mappings:
`python3 tools/agents/generate-adapters.py`. Full precedence rules:
[`.agents/skills/wiki-agents/SKILL.md`](.agents/skills/wiki-agents/SKILL.md).

## Scheduled agents (macOS)

`tools/schedule/` is a host-side catch-up dispatcher fired by a launchd LaunchAgent. It runs one
gated batch per day, defers model jobs until the machine is online and on AC power, writes dated
reports, and freezes the provider and resolved models for the batch. Broad wiki enhancement is off
unless you install with `VAULTLENS_SCHEDULE_ENHANCE=1`. Install with `tools/schedule/install.sh`; design decisions and recovery are in
[`tools/schedule/SPEC.md`](tools/schedule/SPEC.md).

## Repository layout

| Path | Contents |
| --- | --- |
| `AGENTS.md`, `wiki/AGENTS.md`, `projects/AGENTS.md` | Operating schema for agents using a vault |
| `raw/` | `sources/`, `sources-text/`, `assets/`, `inbox/`, `review-inbox/` |
| `wiki/` | Page folders, `_templates/`, `system/`, `home.md`, `SETUP.md` |
| `projects/` | Project workspaces (content gitignored) |
| `.agents/roles/`, `.agents/skills/` | Canonical roles and runbooks |
| `.claude/`, `.codex/` | Generated provider adapters and settings (do not hand-edit adapters) |
| `tools/wiki.py` + `tools/wiki_*.py` | CLI: `init`, `lint`, `search`, `coverage`, `tags`, `stats`, `preprocess`, `project`, `index`, `archive`, `inventory`, `links`, log helpers |
| `tools/agents/` | Headless launcher (`wiki-agent.py`) and adapter generator |
| `tools/local_*.py`, `tools/access-profiles.json`, `tools/runtime/` | Access policy, sandbox runtime, probe and deployment |
| `tools/schedule/` | Scheduled-agent dispatcher and installer |
| `tools/shell/`, `tools/scripts/` | Fish wrappers and host setup or repair scripts |
| `tools/tests/`, `tools/evals/` | Stdlib `unittest` suites and context fixture evals |
| `.githooks/`, `.github/workflows/` | Local gates and CI |

## Development

Contributor rules are in [`tools/AGENTS.md`](tools/AGENTS.md) and the pre-change checklist in
[`REVIEW.md`](REVIEW.md). The essentials:

- **Stdlib-only runtime.** No third-party imports under `tools/`. `ruff` and `basedpyright` are
  dev-only checkers and are never imported.
- **Lint:** `ruff check tools/` (rules `E4`, `E7`, `E9`, `F`, `ANN` in `tools/ruff.toml`).
  Formatting is not enforced.
- **Typing:** `basedpyright` in `all` mode with no baseline (`tools/pyrightconfig.json`); any
  finding fails CI.
- **Commits:** Conventional Commits (`type(scope): summary`, at most 72 characters), checked by the
  `commit-msg` hook.

```bash
.githooks/install.sh                                    # once per clone
ruff check tools/
basedpyright --project tools/pyrightconfig.json
python3 -m compileall -q tools
for t in tools/tests/test_*.py; do python3 "$t"; done   # suites run one file at a time
python3 tools/agents/generate-adapters.py --check       # adapter drift
python3 tools/context_evaluation.py --check             # context fixture baseline
```

CI (`.github/workflows/ci.yml`, Python 3.12, `ruff==0.15.17`, `basedpyright==1.40.2`) runs a
gitleaks secrets scan, lint, typing, compile, the adapter and context checks and every suite
behind the `CI Complete` gate; CodeQL runs weekly. The local `pre-commit` and `pre-push` hooks run
a subset (see [`.githooks/README.md`](.githooks/README.md)). Runtime installation and the isolation
probe need a real host and cannot be verified from CI.

## Further reading

- [`AGENTS.md`](AGENTS.md): the full operating schema
- [`tools/runtime/README.md`](tools/runtime/README.md): runtime boundary, probe receipt, authentication, recovery
- [`tools/schedule/SPEC.md`](tools/schedule/SPEC.md): scheduler design
- [`tools/evals/README.md`](tools/evals/README.md): what the context fixtures do and do not measure
- [Karpathy's llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): the original idea
