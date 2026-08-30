# TODO

Format: Obsidian Tasks plugin emoji. Priority 🔺 highest / ⏫ high / 🔼 medium / 🔽 low / ⏬ lowest. Dates 📅 due / 🛫 start / ⏳ scheduled.

## Bugs

- [x] `scripts/rebuild-projects-todo.sh` widget filter kept completed `[x]` tasks, re-adding done items into `projects/TODO-widget.md` on every rebuild. Now excludes `[x]`/`[-]` tasks and subtasks; `projects/TODO-widget.md` is also gitignored (device-local). 🔼 ✅ 2026-06-01

## Features

### Scheduler — verify before trusting unattended runs

- [ ] Live smoke-test one scheduled LLM run (Codex with ChatGPT subscription): confirm the subscription login is reachable from a launchd-spawned `fish -lc "brain-wiki …"`. ⏫
- [ ] Confirm the squid egress allowlist includes the Claude API host (`api.anthropic.com`) for headless runs. ⏫
- [-] ~~Confirm copilot honours the per-exec `COPILOT_GITHUB_TOKEN` … account failover~~ (obsolete: scheduler migrated to the single Claude-plan identity, 2026-06-11)
- [ ] Install the least-privilege sudoers rule at `/etc/sudoers.d/brain-schedule` (from `schedule/brain-schedule.sudoers`) so lid-closed-on-AC nights can run. 🔼
- [ ] Lid-closed overnight test: verify the scheduled dark-wake stays alive long enough to set `disablesleep` and run the batch. 🔼

### Docs drift

- [x] `wiki/system/schema.md`: added `inventory/` + `system/` category rows and an `inventory` type subsection; bumped `updated`. 🔼 ✅ 2026-06-01
- [x] `CLAUDE.md` (ex-AGENTS.md) `tools/` line: lists the split modules and `tools/schedule/` since the Claude-only schema rewrite. 🔽 ✅ 2026-06-11

### Nice to have

- [ ] Add CI (GitHub Action) running `tools/tests/test_wiki.py` + `tools/tests/test_schedule.py` on push. 🔽

## Agentic-engineering audit (2026-07-05)

Full-stack review: agent definitions, permission machinery, devcontainer profiles, scheduler, skills/docs, Python tooling, tests. Verdict: architecture is sound (mounts are the real boundary and are consistently applied); findings below are the gaps. All 175 tooling tests passed at audit time.

### Policy tightness

- [ ] `Bash(python3 *)` in `READ_ONLY_SHELL_COMMANDS` (`tools/agents/wiki-agent.py:148-164`, applied `:682`) grants arbitrary code exec to every "read-only" shell agent (`python3 -c` writes files/spawns/sockets). Scope to `Bash(python3 tools/wiki.py *)` + `Bash(python3 tools/wiki_extra.py *)` for read-only profiles. Bounded by the RO mount today, but it's the only line of defense under `BRAIN_AGENT_ALLOW_HOST=1` or a mis-assigned profile. ⏫
- [ ] Drop `find` from the read-only shell set (`-delete`/`-exec` are mutators; Glob/Grep cover discovery). Same for interactive `.claude/settings.json:6` `Bash(find *)`. ⏫
- [ ] Fix the "Strictly read-only" comment at `wiki-agent.py:145` — false for `python3`/`find`/`sort -o`; prefix-match also doesn't see `>` redirection. State that the mount is the boundary and the allowlist is best-effort. 🔼
- [ ] `tools/tests/test_agent.py:48-55` asserts the *string form* of the Bash rules, locking in the permissive grants. Add assertions that read-only profiles contain no arbitrary-exec entries (bare `python3 *`, `find *`). ⏫
- [ ] Enhancer source-gap strategy hint (`wiki-agent.py:205`) uses `python3 -c "import random…"` — rewrite to a `wiki.py` subcommand so a tightened allowlist doesn't break it. 🔼

### Bugs (verified)

- [ ] `dispatch.py:432` emits the pre-run-snapshot restore command with unquoted paths (`cp -c -R {snap} {dest}`); the vault path contains spaces so the documented undo fails exactly when needed. `shlex.quote` both paths. 🔺
- [ ] `wiki/system/schema.md:36-37` drift: `source_id: src-YYYY-MM-NNN` missing the day (contradicts its own example at `:51` and `CLAUDE.md`); `source_type` enum missing `pdf`. 🔼
- [ ] Dead link: `wiki/system/enhancement-strategies.md:56` cites `wiki/reports/scheduled-emerge-2026-06-29.md`, which does not exist. 🔼
- [ ] `parse_frontmatter` (`tools/wiki.py:178`) requires exact `\n---\n`: a closing `---` at EOF without trailing newline silently parses the whole file as body → spurious "missing fields" lint errors. 🔼
- [ ] Scheduler ledger saved only at tick end (`dispatch.py:1426`); a mid-batch crash loses completed-step state and reruns expensive LLM steps. Persist after each step. 🔼
- [ ] SPEC-vs-code: `tools/schedule/SPEC.md:256` claims the flock yields one enhance instance; the lock (`dispatch.py:1213`) only serializes dispatcher ticks — a manual `brain-wiki enhance --forever` can run concurrently with the nightly enhance. Fix SPEC or add a shared enhance lock. 🔼
- [ ] No timeout on the direct (non-dispatcher) agent invocation (`wiki-agent.py:762` `subprocess.run(cmd)`); a hung CLI blocks an `enhance --forever` loop indefinitely. `pdftotext`/`qpdf` calls (`wiki_ingest.py:72-99`) also lack timeouts. 🔼
- [ ] `wiki.py search` scores substrings over the full text including frontmatter YAML (`wiki_query.py:232-233`) — "cat" matches "category", every page scores on `title`/`summary` keys. Strip frontmatter and use word boundaries. 🔼
- [ ] `inventory show` has no `..` guard (`wiki_inventory.py:163`) — path traversal in a local read-only CLI. 🔽
- [ ] `generate_source_id` uses `len(existing)+1` (`wiki_extra.py:33`); deletions/gaps cause ID collisions. 🔽
- [ ] `wiki_log.py:39` strips `.md` anywhere in the page string, not just as suffix. 🔽

### Token usage / docs redundancy

- [ ] `wiki.py lint` prints all findings uncapped (`wiki_lint.py:317-361`); on a 1300-page vault an agent ingests thousands of lines. Add `--limit`/summary default for agent callers; same for `tags` (default `--limit 0`, `wiki.py:433-437`). 🔼
- [ ] Collapse the clarify flow to one home: full procedure duplicated in `.claude/rules/working-in-projects.md:44-55` and `wiki-project-clarify/SKILL.md:24-66`; keep one, point from the other. 🔼
- [ ] Demote `wiki/system/schema.md` to a thin pointer at CLAUDE.md (it declares CLAUDE.md authoritative at `:11` then re-documents frontmatter/categories — which is exactly how the `:36-37` drift happened). 🔼
- [ ] Trim always-loaded `CLAUDE.md` (~2200 words/session): `## Tool permissions` detail, `## Devcontainer sandbox`, `## Scheduled agents` mechanics are situational — move detail to an on-demand skill/rule, keep one-line pointers. Also dedupe: metadata schema (triple copy with schema.md + wiki-projects/SKILL.md), project-runner writer semantics (triple), agent roster + CoS modes, dual-link mechanics (each doubled in a skill). 🔼
- [ ] `wiki-agents` vs `wiki-ingest`/`wiki-maintenance` skill descriptions overlap in trigger space ("ingest", "audit") — narrow `wiki-agents` to agent *selection*, drop its verb list. 🔽

### Dead weight

- [ ] `--effort` is a no-op end-to-end (`EFFORT_MAP` all-empty, `wiki-agent.py:401`; mirrored `Step.effort` in dispatch.py) yet threaded through brain-wiki → dispatch → CLI. Wire it for real or delete the layer. 🔽
- [ ] Multi-CLI abstraction is vestigial: `CLI_OPTIONS` has one entry; `get_default_model`/`validate_cli` exist for a choice that no longer exists. 🔽
- [ ] `wiki_extra.py` is a half-orphaned parallel CLI: `qmd-search` duplicates the qmd MCP path; `count_words` reimplements `wiki.wiki_files()` filtering. Fold `next-id`/`stats` into `wiki.py`, retire the rest. 🔽

### Test gaps

- [ ] Zero coverage: `wiki_ingest` (subprocess+regex PDF pipeline — highest-risk module), `wiki_projects`, `wiki_archive` (registry-drift reconciliation), `wiki_inventory`, `wiki_extra`, `wiki_log`. Priority: ingest first. 🔼
- [ ] Untested behaviors: `parse_frontmatter` edge cases (EOF close, CRLF), `_set_frontmatter_field` round-trip, dispatch stateful paths (`_run_steps`, gates, ledger persistence on crash — only pure helpers are exercised). 🔼
- [ ] Test suite invisible to `pytest`/`unittest` discovery (custom `check()` scripts; `unittest discover` finds 0 tests) — blocks the CI item above. Wrap or rename so discovery works. 🔽
