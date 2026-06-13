# Git hooks

Self-contained git hooks for this vault. They depend only on `git` and
`python3` (no `pre-commit` framework, no `ruff`/`pytest` requirement, no network
fetches), so they run identically on the host and inside the egress-locked
devcontainer. `ruff` is used only if it happens to be installed.

## Install

```bash
.githooks/install.sh
```

This sets `core.hooksPath` to this directory (a relative path, so it resolves on
both the host and at `/workspaces/repo` in the sandbox) and marks the hooks
executable. `.git/config` is shared between host and container, so one run
activates the hooks everywhere. It replaces the stale
`/workspaces/repo/.git/hooks` value that pinned hooks to a path that does not
exist on the host.

## `pre-commit`

Blocks the commit on real defects; warns on style; never edits your files (it
prints the exact fix command instead).

| Check | Scope | Action |
|---|---|---|
| Force-added gitignored content | all staged paths | block (driven by `.gitignore`) |
| Oversized blobs (> 1 MiB) | all staged paths | block |
| Merge-conflict markers | staged text files | block (`=======` allowed: it is a setext heading) |
| Secret/credential scan | staged text files | block (reports line numbers only) |
| `python -m py_compile` | staged `*.py` | block on syntax error |
| `ruff check` | staged `*.py`, if `ruff` present | block on lint error |
| `test_wiki.py` + `test_schedule.py` | when `tools/**.py` staged | block on failure |
| `wiki.py lint` | when `wiki/**` or `tools/wiki*.py` staged | block on errors (warnings pass) |
| `_index.md` freshness | when `wiki/**` or `tools/wiki*.py` staged | block if stale |

## `commit-msg`

Enforces Conventional Commits (`type(scope): summary`) to match this repo's
history. Merge / revert / fixup / squash subjects are exempt. Subjects over 72
chars warn but do not block.

## `pre-push`

The heavier gate before code leaves the machine — mirrors the CI jobs
(`.github/workflows/ci.yml`) so failures surface locally, not after a push.
Same self-contained philosophy (git + `python3`; `ruff` only if installed).

| Check | Action |
|---|---|
| `python -m compileall tools/` (recursive syntax compile) | block on syntax error |
| `ruff check tools/` (whole tree, if `ruff` present) | block on lint error |
| `test_wiki.py` + `test_schedule.py` | block on failure |

## CI

`.github/workflows/` mirrors these hooks server-side and is the backstop when a
hook is bypassed or `ruff` isn't installed locally: `ci.yml` (gitleaks secret
scan, `ruff check`, the unittest suites, behind a `CI Complete` gate),
`codeql.yml` (weekly Python scan), `auto-merge.yml` + `dependabot.yml` (weekly
GitHub-Actions pin bumps). Harmonised with the Vision/Watchman pipelines.

## Escape hatches

`git commit --no-verify` skips all hooks. Per-check env vars:

```
SKIP_SECRET_SCAN=1   ALLOW_IGNORED_FILES=1   ALLOW_LARGE_FILES=1
SKIP_TESTS=1         SKIP_WIKI_LINT=1        SKIP_WIKI_INDEX=1
SKIP_COMMIT_MSG_CHECK=1   MAX_FILE_BYTES=<n>
SKIP_HOOKS=1   SKIP_RUFF=1   (pre-push only; SKIP_TESTS also applies there)
```

Mark a known-safe line that looks like a secret with a trailing
`pragma: allowlist secret`.

Note: the `_index.md` freshness check runs `wiki.py index`, which inspects every
category including locally-gitignored pages. Local-only page edits can therefore
report a stale mirror even on an unrelated commit; regenerate with
`python3 tools/wiki.py index --rebuild` or bypass with `SKIP_WIKI_INDEX=1`.
