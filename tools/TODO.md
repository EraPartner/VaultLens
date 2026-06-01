# TODO

Format: Obsidian Tasks plugin emoji. Priority 🔺 highest / ⏫ high / 🔼 medium / 🔽 low / ⏬ lowest. Dates 📅 due / 🛫 start / ⏳ scheduled.

## Bugs

- [x] `scripts/rebuild-projects-todo.sh` widget filter kept completed `[x]` tasks, re-adding done items into `projects/TODO-widget.md` on every rebuild. Now excludes `[x]`/`[-]` tasks and subtasks; `projects/TODO-widget.md` is also gitignored (device-local). 🔼 ✅ 2026-06-01

## Features

### Scheduler — verify before trusting unattended runs

- [ ] Live smoke-test one scheduled LLM run (copilot/GPT-5.2): confirm the Keychain token is reachable from a launchd-spawned `fish -lc "brain-wiki …"`. ⏫
- [ ] Confirm the squid egress allowlist includes Copilot API hosts (`api.githubcopilot.com`, `api.individual.githubcopilot.com`) for headless runs. ⏫
- [ ] Confirm copilot honours the per-exec `COPILOT_GITHUB_TOKEN` over any cached in-container auth, so the `talicaddy` ↔ `Noortje…` account failover actually takes effect. ⏫
- [ ] Install the least-privilege sudoers rule at `/etc/sudoers.d/brain-schedule` (from `schedule/brain-schedule.sudoers`) so lid-closed-on-AC nights can run. 🔼
- [ ] Lid-closed overnight test: verify the scheduled dark-wake stays alive long enough to set `disablesleep` and run the batch. 🔼

### Docs drift

- [x] `wiki/system/schema.md`: added `inventory/` + `system/` category rows and an `inventory` type subsection; bumped `updated`. 🔼 ✅ 2026-06-01
- [ ] `AGENTS.md` `tools/` line: list the split modules (`wiki_ingest` / `wiki_log` / `wiki_projects` / `wiki_query`) and `tools/schedule/`. 🔽

### Nice to have

- [ ] Add CI (GitHub Action) running `tools/tests/test_wiki.py` + `tools/tests/test_schedule.py` on push. 🔽
