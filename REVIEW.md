# REVIEW.md — pre-change checklist for VaultLens

Run this before proposing or committing a change: it encodes the review knowledge that otherwise
lives in the maintainer's head, so review catches issues automatically (see `CLAUDE.md` and
`.githooks/README.md` for the why and the escape hatches).

## Secrets & safety
- [ ] No credentials committed — the pre-commit regex scan and CI gitleaks flag tokens/keys
      (`ghp_`/`github_pat_`/`sk-ant-`/`AKIA`/`xox`/`BEGIN ... PRIVATE KEY`) and hard-coded
      `*_TOKEN=`/`*_API_KEY=` assignments. Mark a known-safe line `pragma: allowlist secret`.
- [ ] No force-added gitignored content — raw sources, generated `wiki/` pages, `projects/*`,
      `.claude/settings.local.json`, `.devcontainer/mount-roots.local` stay local (never `git add -f`).
- [ ] No blob > 1 MiB staged (pre-commit blocks; override only with `ALLOW_LARGE_FILES=1`).

## Correctness & invariants
- [ ] Layer boundary intact: `raw/` is the immutable source of truth, `wiki/` is agent-owned,
      `projects/` never write to `wiki/` or `raw/` (a project's write surface is `projects/<slug>/`).
- [ ] Edited wiki pages keep required frontmatter (`title`/`type`/`status`/`created`/`updated`/
      `summary`) and bump `updated`; `python3 tools/wiki.py lint` is clean.
- [ ] Wikilinks written as bare `[[path/to/page]]`, then `python3 tools/wiki.py links --fix --write`
      adds the dual-link mirrors — never hand-edit mirrors or the generated `_index.md` files.
- [ ] Tooling stays stdlib-only — no third-party imports under `tools/` (CI has no dependency
      install step; the hooks assume `git` + `python3` only).

## Tests & validation
- [ ] `ruff check tools/` clean (config `tools/ruff.toml`, pinned `ruff==0.15.17` in CI).
- [ ] `python3 -m compileall -q tools` passes (syntax gate).
- [ ] Tooling tests pass — run each `python3 tools/tests/test_*.py`
      (CI runs all five; the git hooks run `test_wiki.py` + `test_schedule.py`).
- [ ] CI (the `CI` workflow, required check `CI Complete`: secrets-scan + lint + test) expected green;
      weekly `codeql.yml` Python scan also runs.

## Hygiene
- [ ] Conventional Commit subject (`type(scope): summary`, ≤ 72 chars) — the `commit-msg` hook
      enforces `feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert`.
- [ ] Commit is signed (Secure Enclave ssh key; `commit.gpgsign` + `tag.gpgsign` are `true`).
- [ ] Message says what + why; update `CLAUDE.md`, `README.md`, or `.githooks/README.md` if behavior
      or a documented gate changed.
- [ ] Hooks are installed (`.githooks/install.sh`); don't land a change by leaning on `--no-verify`.
