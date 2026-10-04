# AGENTS.md — VaultLens development

Guidance for coding agents **building** VaultLens (the code in this repository). It does not
apply to agents **using** a vault. For that, read the root `AGENTS.md`, `projects/AGENTS.md` and
`wiki/AGENTS.md`. Do not follow those files when changing code; read them only to understand
product behaviour.

The global working agreement applies (auditability, pushback, signing, publication, safety). This
file lists project-specific rules only.

## Project

VaultLens is the public, AGPL-3.0-only, provider-neutral template of an "LLM Wiki" knowledge base
for Obsidian with Claude Code or Codex. It ships the system (tooling, roles, runbooks, adapters)
and never any vault content.

| Area | Path |
| --- | --- |
| Wiki CLI and modules (`wiki.py` dispatches to `wiki_*.py`) | `tools/` |
| Headless agent launcher, adapter generator | `tools/agents/` |
| Scheduled-agent dispatcher and spec | `tools/schedule/` |
| Context budgeting and fixture evals | `tools/context_*.py`, `tools/evals/` |
| Tooling tests (stdlib `unittest`) | `tools/tests/` |
| Canonical roles and runbooks | `.agents/roles/`, `.agents/skills/` |
| Generated provider adapters (do not hand-edit) | `.claude/agents/`, `.codex/agents/` |
| Codex cloud setup, generated policy | `.codex/cloud/` |
| Sandbox launcher | `.devcontainer/` |
| Local git gates | `.githooks/` |
| CI | `.github/workflows/` |

## Start with project knowledge

Before changing code, read the relevant docs. Treat docs as intent and code as current behaviour;
resolve conflicts explicitly.

- `REVIEW.md` — the pre-change checklist. Run through it before proposing a change.
- `.githooks/README.md` — what each local gate checks and its escape hatches.
- `tools/schedule/SPEC.md` — locked scheduler decisions and rejected options.
- `tools/evals/README.md` — what the context fixtures do and do not measure.
- `.devcontainer/README.md` and `.codex/cloud/README.md` — sandbox and cloud environments.
- `.agents/skills/*/SKILL.md` — operational runbooks that the tooling must keep working.

## Commands

Run from the repository root. CI uses Python 3.12 and `ruff==0.15.17`.

```bash
.githooks/install.sh                                 # enable local hooks (once per clone)
ruff check tools/                                    # lint (rules in tools/ruff.toml)
python3 -m compileall -q tools                       # syntax gate
for t in tools/tests/test_*.py; do python3 "$t"; done  # all tooling suites
python3 tools/tests/test_wiki.py                     # one suite
python3 tools/agents/generate-adapters.py            # regenerate adapters after a role change
python3 tools/agents/generate-adapters.py --check    # fail on adapter drift
python3 tools/context_evaluation.py --check          # context fixture baseline
python3 .codex/cloud/check-instructions.py           # cloud policy matches vendored inputs
for s in .codex/cloud/tests/*.test.sh; do bash "$s"; done  # cloud lifecycle tests
```

Suites are run one file at a time, not through `unittest discover`. New `tools/tests/test_*.py`
files are picked up by CI automatically.

## Conventions

- Tooling is **stdlib-only** Python. Do not add third-party imports under `tools/`. CI has no
  dependency install step and the hooks assume `git` and `python3` only.
- Lint is `ruff check` only (`E4`, `E7`, `E9`, `F`). Formatting is not enforced. Do not reformat
  unrelated code.
- Conventional Commit subjects: `type(scope): summary`, at most 72 characters. The `commit-msg`
  hook accepts `feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert`.
- Edit canonical roles in `.agents/roles/`, then regenerate adapters. Never hand-edit
  `.claude/agents/` or `.codex/agents/`.
- `.codex/cloud/policy/` and `.codex/cloud/AGENTS.md` are generated from vendored inputs. Do not
  edit them by hand.
- Keep changes focused. Do not mix unrelated cleanup into a task.
- Add a test with each behaviour change. Safety-relevant behaviour (consent gate, write
  boundaries, cancellation, link handling) needs a regression test.

## Public-repo boundary

This repository is public. The `.gitignore` invariant is: the system is tracked and all data is
ignored.

- Never commit private vault content, the operator profile, personal `raw/`, `wiki/` or
  `projects/` material, credentials, or host-specific paths.
- Never `git add -f` an ignored path. Flag any change that would start tracking a data path.
- Do not read, copy, mount or infer the private Brain vault in a cloud session. Cloud work uses
  only the tracked template.
- Do not add model-quality claims derived from fixture character counts.

## Verification

Scale checks to risk. CI runs lint, compile, all suites, cloud checks and a secrets scan behind the
`CI Complete` gate.

- Isolated edit: the targeted suite and `ruff check tools/`.
- Cross-module change: compileall, every suite, and `ruff check tools/`.
- Agent launcher, scheduler, context budgeting, or sandbox change: the full set above, plus
  `generate-adapters.py --check` and `context_evaluation.py --check`.

Local gates do not replace these. `pre-commit` runs only `test_wiki.py` and `test_schedule.py`.
Do not land a change using `--no-verify`.

Finish with changed files, checks run, skipped checks, residual risk, and follow-ups.

## Documentation sync

Update docs in the same change when behaviour, a gate, a command, or a documented path changes:
`README.md`, `.githooks/README.md`, `tools/schedule/SPEC.md`, `tools/evals/README.md`,
`.devcontainer/README.md`, or the affected `SKILL.md`. If none needs an update, say why in the
completion report.
