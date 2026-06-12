# TODO

Format: Obsidian Tasks plugin emoji. Priority 🔺 highest / ⏫ high / 🔼 medium / 🔽 low / ⏬ lowest. Dates 📅 due / 🛫 start / ⏳ scheduled.

## Bugs

- [x] `scripts/rebuild-projects-todo.sh` widget filter kept completed `[x]` tasks, re-adding done items into `projects/TODO-widget.md` on every rebuild. Now excludes `[x]`/`[-]` tasks and subtasks; `projects/TODO-widget.md` is also gitignored (device-local). 🔼 ✅ 2026-06-01

## Features

### Scheduler — verify before trusting unattended runs

- [ ] Live smoke-test one scheduled LLM run (claude/sonnet): confirm the subscription login is reachable from a launchd-spawned `fish -lc "brain-wiki …"`. ⏫
- [ ] Confirm the squid egress allowlist includes the Claude API host (`api.anthropic.com`) for headless runs. ⏫
- [-] ~~Confirm copilot honours the per-exec `COPILOT_GITHUB_TOKEN` … account failover~~ (obsolete: scheduler migrated to the single Claude-plan identity, 2026-06-11)
- [ ] Install the least-privilege sudoers rule at `/etc/sudoers.d/brain-schedule` (from `schedule/brain-schedule.sudoers`) so lid-closed-on-AC nights can run. 🔼
- [ ] Lid-closed overnight test: verify the scheduled dark-wake stays alive long enough to set `disablesleep` and run the batch. 🔼

### Docs drift

- [x] `wiki/system/schema.md`: added `inventory/` + `system/` category rows and an `inventory` type subsection; bumped `updated`. 🔼 ✅ 2026-06-01
- [x] `CLAUDE.md` (ex-AGENTS.md) `tools/` line: lists the split modules and `tools/schedule/` since the Claude-only schema rewrite. 🔽 ✅ 2026-06-11

### Nice to have

- [ ] Add CI (GitHub Action) running `tools/tests/test_wiki.py` + `tools/tests/test_schedule.py` on push. 🔽
