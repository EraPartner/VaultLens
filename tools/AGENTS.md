# AGENTS.md — VaultLens development

Guidance for coding agents **building** VaultLens (the code in this repository). It does not
apply to agents **using** a vault. For that, read the root `AGENTS.md`, `projects/AGENTS.md` and
`wiki/AGENTS.md`. Do not follow those files when changing code; read them only to understand
product behaviour.

The global working agreement applies (auditability, pushback, signing, publication, safety). This
file lists project-specific rules only.

## Project

VaultLens is the public, AGPL-3.0-only, provider-neutral template of an "LLM Wiki" knowledge base
for Obsidian with Claude Code or Codex. It ships the system (tooling, roles, runbooks, adapters,
native agent runtime) and never any vault content.

| Area | Path |
| --- | --- |
| Wiki CLI and modules (`wiki.py` dispatches to `wiki_*.py`) | `tools/` |
| Headless agent launcher, adapter generator | `tools/agents/` |
| Access profiles, provider commands, launch routing, scoped search | `tools/access-profiles.json`, `tools/local_*.py`, `tools/agent_*.py`, `tools/provider_commands.py`, `tools/brain_launch.py`, `tools/scoped_search.py` |
| Native whole-process runtime: install, probe, deploy, maintain | `tools/runtime/`, `tools/runtime_*.py`, `tools/process_control.py`, `tools/macos_processes.py` |
| Host fish wrappers and host repair scripts | `tools/shell/`, `tools/scripts/` |
| Scheduled-agent dispatcher, recovery, spec | `tools/schedule/` |
| Context budgeting and fixture evals | `tools/context_*.py`, `tools/evals/` |
| Tooling tests (stdlib `unittest`) | `tools/tests/` |
| Canonical roles and runbooks | `.agents/roles/`, `.agents/skills/` |
| Generated provider adapters (do not hand-edit) | `.claude/agents/`, `.codex/agents/` |
| Local git gates | `.githooks/` |
| CI | `.github/workflows/` |

## Start with project knowledge

Before changing code, read the relevant docs. Treat docs as intent and code as current behaviour;
resolve conflicts explicitly.

- `REVIEW.md` — the pre-change checklist. Run through it before proposing a change.
- `tools/runtime/README.md` — runtime boundary, profiles, probe receipt, deployment, recovery.
- `tools/schedule/SPEC.md` — locked scheduler decisions and rejected options.
- `tools/evals/README.md` — what the context fixtures do and do not measure.
- `.githooks/README.md` — what each local gate checks and its escape hatches.
- `README.md` — setup, provider selection and access profiles.
- `.agents/skills/*/SKILL.md` — operational runbooks that the tooling must keep working.

## Commands

Run from the repository root. CI uses Python 3.12, `ruff==0.15.17` and `basedpyright==1.40.2`. The runtime needs Python
3.11 or newer. CI installs `fish` for the provider wrapper tests.

```bash
.githooks/install.sh                                 # enable local hooks (once per clone)
ruff check tools/                                    # lint (rules in tools/ruff.toml)
basedpyright --project tools/pyrightconfig.json     # typing gate ("all" mode, baselined)
python3 -m compileall -q tools                       # syntax gate
for t in tools/tests/test_*.py; do python3 "$t"; done  # all tooling suites
python3 tools/tests/test_wiki.py                     # one suite
python3 tools/agents/generate-adapters.py            # regenerate adapters after a role change
python3 tools/agents/generate-adapters.py --check    # fail on adapter drift
python3 tools/context_evaluation.py --check          # context fixture baseline
```

Suites are run one file at a time, not through `unittest discover`. New `tools/tests/test_*.py`
files are picked up by CI automatically.

Some checks need the operator's Mac and cannot run in a cloud session. `bash tools/runtime/install.sh`,
`python3 tools/local_runtime.py doctor`, and `python3 tools/runtime/probe.py` install and verify the
pinned sandbox runtime on the real host. Portable tests do not establish operating-system isolation.
Do not claim isolation is verified from a cloud run.

## Conventions

- Tooling is **stdlib-only at runtime**. Do not add third-party imports under `tools/`. The only
  non-stdlib dependencies are dev-only checkers (`ruff`, `basedpyright`), installed in CI's lint
  and typing jobs and never imported by tooling. The hooks assume `git` and `python3` only.
- Lint is `ruff check` (`E4`, `E7`, `E9`, `F`, `ANN`; rules in `tools/ruff.toml`). Formatting is
  not enforced. Do not reformat unrelated code.
- Typing is `basedpyright` in `all` mode (`tools/pyrightconfig.json`), with
  `reportAny`, `reportUnusedCallResult`, `reportImplicitOverride`, `reportImplicitRelativeImport`
  and `reportImplicitStringConcatenation` switched off, checked against
  `tools/typing-baseline.json`. The baseline holds the findings that predate the switch from
  `strict`; any new finding fails CI. Shrink the baseline as you fix code
  (`basedpyright --project tools/pyrightconfig.json --writebaseline` after fixing, never to admit
  new findings). Do not disable further rules, add `# pyright: ignore`, bare `Any` or `cast` without a
  one-line reason. Ruff `ANN` has no `per-file-ignores`; do not add any.
- Conventional Commit subjects: `type(scope): summary`, at most 72 characters. The `commit-msg`
  hook accepts `feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert`.
- Edit canonical roles in `.agents/roles/`, then regenerate adapters. Never hand-edit
  `.claude/agents/` or `.codex/agents/`.
- Do not add `CLAUDE.md` files. Claude Code reads `AGENTS.md` natively (v2.1.277 or newer), and
  any `CLAUDE.md` in the directory or above it makes Claude read that file instead.
- Keep access policy (`tools/access-profiles.json`) separate from provider and model selection.
  Do not add an automatic provider switch or an unsandboxed fallback.
- Keep changes focused. Do not mix unrelated cleanup into a task.
- Add a test with each behaviour change. Safety-relevant behaviour (consent gate, access
  profiles, write boundaries, cancellation and cleanup, link handling) needs a regression test.

## Public-repo boundary

This repository is public. The `.gitignore` invariant is: the system is tracked and all data is
ignored.

- Never commit private vault content, the operator profile, personal `raw/`, `wiki/` or
  `projects/` material, credentials, or host-specific paths.
- Never commit local state: `tools/llm.local.json`, `tools/access.local.json`,
  `tools/runtime-state/` (receipts, deployments, backups), or provider authentication stores.
- Never `git add -f` an ignored path. Flag any change that would start tracking a data path.
- Do not read, copy, mount or infer the private Brain vault in a cloud session. Cloud work uses
  only the tracked template.
- Do not add model-quality claims derived from fixture character counts.

## Verification

Scale checks to risk. CI runs lint, typing, compile, all suites and a secrets scan behind the
`CI Complete` gate.

- Isolated edit: the targeted suite, `ruff check tools/` and the basedpyright command above.
- Cross-module change: compileall, every suite, `ruff check tools/` and basedpyright.
- Agent launcher, access profile, runtime, scheduler, or context budgeting change: the full set
  above, plus `generate-adapters.py --check` and `context_evaluation.py --check`. Say which
  host-only checks (probe, native provider runs) were not run.

Local gates do not replace these. `pre-commit` and `pre-push` run only `test_wiki.py` and
`test_schedule.py`. Do not land a change using `--no-verify`.

Finish with changed files, checks run, skipped checks, residual risk, and follow-ups.

## Documentation sync

Update docs in the same change when behaviour, a gate, a command, or a documented path changes:
`README.md`, `.githooks/README.md`, `tools/runtime/README.md`, `tools/schedule/SPEC.md`,
`tools/evals/README.md`, or the affected `SKILL.md`. If none needs an update, say why in the
completion report.
