# Scheduled agents — design spec

Status: **implemented 2026-06-01.** Files: `dispatch.py` (the dispatcher),
`com.brain.schedule.plist` (LaunchAgent), `brain-schedule.sudoers` (least-privilege
power rule), `install.sh` (installer), `../tests/test_schedule.py` (dispatcher tests).
Activate with `tools/schedule/install.sh` + the `sudo pmset repeat wake` and
sudoers commands it prints. This file remains the design rationale.

> **2026-06-02 — backend migrated to the Claude plan.**
> The copilot accounts (`talicaddy`, `Noortjekjzecbkjzcebkjczeh`) are no longer
> usable. LLM jobs now run on `--cli claude --model sonnet` (the logged-in
> `claude` CLI = the Claude plan subscription). The two-account failover
> collapsed to a single identity (`ACCOUNTS = ["claude-plan"]`): a Claude
> usage-limit error marks it limited and defers the batch (no second account to
> switch to). The copilot prose below is kept as historical rationale; where it
> says "copilot / gpt-5.2 / two accounts," read "claude / sonnet / one identity."
>
> **2026-06-20 — reactivated and live.** The LaunchAgent is loaded
> (`launchctl list | grep com.brain`) and `pmset repeat wake 01:25` is restored;
> the nightly batch runs (the ledger shows recent `lint`/`index`/`cos-brief`
> successes). The "Deactivation / reactivation" section at the bottom remains the
> procedure if it is ever paused again.
>
> **2026-08-17 — Codex container migration complete.**
> The dispatcher now reads `VAULTLENS_LLM_CLI=claude|codex` and optional model,
> health-host, and identity overrides. The LockBox-derived `brain-wiki` launcher
> supports Codex with private per-profile login state. The installer can either
> enable a selected backend immediately or prepare it in a disabled state.

## Lid-closed runs (AC-gated keep-awake)

To run the nightly batch with the lid closed and **no external display**, the
dispatcher overrides macOS lid-close sleep with `pmset disablesleep` -- but only
on AC, which makes the flag safe (a closed bag is always on battery) and makes
clamshell mode irrelevant. Per tick:

- **self-heal** first: `pmset -a disablesleep 0` (clears a flag left stuck by a
  hard-killed prior run; no-op otherwise).
- engage only if `on AC AND lid closed (ioreg AppleClamshellState) AND an LLM step
  is due AND online AND container`: `pmset -a disablesleep 1`, run the batch, then in
  a `finally` `pmset -a disablesleep 0` and (if still lid-closed) `pmset sleepnow`.
- battery / lid-open / nothing-due -> never touches `disablesleep`.

**Least privilege:** the entire root surface is three exact pmset argument vectors
(`-a disablesleep 1`, `-a disablesleep 0`, `sleepnow`), granted via
`brain-schedule.sudoers` -> `/etc/sudoers.d/brain-schedule`. pmset is
power-management only (no code exec / file access / user change), and any other
pmset call still needs a password. `sudo -n` is used so a missing rule fails fast
(lid-closed nights are skipped + caught up on next AC open) rather than hanging.

## Locked decisions

1. **Mechanism:** a host-side *catch-up dispatcher* fired by a launchd
   LaunchAgent. Not fixed calendar jobs, not the in-container `--forever` loop.
2. **Overnight:** forced wakes via `pmset repeat wake`, but heavy jobs run
   **only on AC** (the dispatcher gates on power; pmset itself cannot).
3. **Offline:** LLM jobs **defer until online** (no ollama fallback). Pure-python
   maintenance (Tier 0) runs offline regardless.
4. Read-only agents never write their own reports; the **dispatcher** captures
   their stdout and writes the dated report. Keeps the agents read-only.
5. **Backend (current):** one explicit provider runs an entire batch. The
   dispatcher reads `VAULTLENS_LLM_CLI=claude|codex`; Codex is the default and
   leaves its model unpinned unless
   `VAULTLENS_LLM_MODEL` is set. It never falls back across providers mid-batch.
6. **Single backend identity (current):** the ledger identity defaults to
   `<cli>-plan` and can be overridden with `VAULTLENS_LLM_IDENTITY`. A usage or
   rate limit marks that identity `limited_until` and **defers the rest of the
   LLM batch**, caught up on the next eligible window. There is no automatic
   cross-provider or cross-account failover.
7. **Nothing LLM runs per tick.** All LLM work happens in **one nightly batch**;
   the only daily-morning LLM job is the cos brief. The ~30-min tick is purely the
   catch-up gate-checker, never an LLM trigger.
8. **Nightly `enhance` is paused by default.** Set
   `VAULTLENS_SCHEDULE_ENHANCE=1` when installing to opt in. When enabled, it is
   capped at `--iterations 10` per night (not `--forever`).

## Backend and model

- Invocation shape: `fish -lc "brain-wiki <agent> --cli <claude|codex>
  [--model <model>] --effort <low|medium|high>"`.
- Configuration: `VAULTLENS_LLM_CLI`, with optional
  `VAULTLENS_LLM_MODEL`, `VAULTLENS_LLM_HEALTH_HOST`, and
  `VAULTLENS_LLM_IDENTITY`. Broad nightly wiki enhancement has a separate
  explicit opt-in, `VAULTLENS_SCHEDULE_ENHANCE=1`. `install.sh` copies these
  into the installed LaunchAgent. Re-run it when changing providers or this opt-in.
- Defaults: Claude uses `sonnet` and `api.anthropic.com`; Codex leaves model
  selection to its workspace configuration and uses `chatgpt.com` for the
  coarse online gate.
- Auth is owned by the selected CLI's logged-in session. Verify the chosen CLI
  non-interactively from a launchd-spawned login `fish` before relying on it.
- `brain-wiki` selects the same least-privilege container profiles for either
  provider. Every Codex profile used by unattended jobs must complete its
  one-time login before the scheduler is enabled.

### Historical Copilot account selection and failover

This section records the pre-2026-06-02 design only; it is not current behavior.
`bin/agent` minted the token **at exec time**: `gh auth token --user
${BRAIN_GH_ACCOUNT:-talicaddy}` -> name-only `-e COPILOT_GITHUB_TOKEN` on
`compose exec` (verified, lines ~178-186/226). Consequence: **switching accounts
is cheap** — the dispatcher just sets `BRAIN_GH_ACCOUNT` for the next
`brain-wiki` invocation; same running container, different token, no rebuild / no
qmd reseed.

- Accounts, in priority order: `["talicaddy", "Noortjekjzecbkjzcebkjczeh"]`
  (both confirmed present in `gh auth status`; kept in `ACCOUNTS` in dispatch.py).
- The dispatcher picks the first account **not** in cooldown, sets
  `BRAIN_GH_ACCOUNT`, runs the job. Account choice is **sticky** within and across
  nights (don't split a batch) until the active account hits a limit.
- On a rate-limit/quota error: mark the current account `limited_until` in the
  ledger, switch to the next healthy account, **retry the same job once**.
- If **all** accounts are limited: defer the job and the rest of the LLM batch;
  notify "all copilot accounts limited."
- VERIFY at build: copilot honors the per-exec `COPILOT_GITHUB_TOKEN` over any
  cached in-container auth state, so the switch actually takes effect.

## Rate limits and request budget

The available quota is provider- and plan-dependent, so the dispatcher does not
hardcode a number. Two facts drive the design:
- Treat quota as runtime state reported by the selected CLI.
- **Each agentic run is many model turns** (tool calls), so one `cos brief` or
  `contradict` can consume many requests, not one. Budget accordingly.

The dispatcher classifies CLI exit output into three failure modes:

| Class | Signal | Behavior |
|---|---|---|
| Transient / network | timeout, 5xx | retry next tick (ledger not advanced) |
| Short rate-limit | 429 / "rate limit" | mark backend identity `limited_until` (backoff 30m -> 1h -> 2h); defer |
| **Quota exhausted** | "quota" / "premium request" / "upgrade" | mark backend identity `limited_until` (probe again in ~24h; do not compute an exact reset); defer |
| **Backend limited** | selected identity is cooling down | defer the job and rest of the LLM batch; notify |

There is one configured backend identity. The dispatcher never switches provider
or credentials automatically.

Budget-shaping (build into the job table):
- `enhance` is **off by default**. When explicitly enabled, it is capped at
  **`--iterations 10` per night** (the biggest consumer; no `--forever`).
- Heavy digests (contradict/emerge/discover) stay **weekly** (Sunday batch).
- Each agentic run is many model turns, so cos brief uses `--effort low`.

## Concrete schedule

The dispatcher ticks every ~30 min only to check gates + the ledger. Actual work:

**Nightly batch — once per night, ~01:30 (pmset wake 01:25), AC-gated, in order:**
1. `lint` + `index` + `qmd update` (offline, host-native pre-check), weekly
   `qmd cleanup` for inactive documents and orphan chunks, then a bounded
   `qmd embed` pass on AC power. Cleanup and embedding run only on AC. Embedding
   saves progress and resumes on later nights until the semantic index is current.
2. `ingest` **if** `raw/inbox` / `raw/sources` has unprocessed files
   (checked here, **once a night**, not per tick)
3. **Sundays only:** `contradict` + `emerge` + `discover` (read-only digests)
4. `project-runner` — one invocation per non-frozen, opted-in (`enabled: true`) project with a
   due `AGENDA.md` task (capped `MAX_PROJECTS_PER_NIGHT`). User-facing work claims
   budget before any optional enhancement; writes `projects/<slug>/` (not wiki/),
   applied-not-committed, with a pre-run snapshot per project for undo
5. **Only when `VAULTLENS_SCHEDULE_ENHANCE=1`:** `enhance --iterations 10`
   (capped), last in the nightly batch and before the morning-only brief

All LLM steps use the configured `--cli` and optional `--model`, and defer if a
usage limit is hit or if offline. The whole batch runs at most once per night; if a night is missed
(battery / asleep), the ledger catches it up on the next AC night.

**Daily morning — ~07:00 window, battery OK:**
- `cos brief` (`--effort low`). The only LLM job outside the nightly batch.

Weekly digests land Sunday night so Monday's brief can reference them.

## Monitoring

- Built-in: `launchctl list | grep com.brain` (loaded? last exit), `launchctl print
  gui/$(id -u)/com.brain.schedule` (full state), `pmset -g sched` (scheduled wakes),
  `log show --last 2h --predicate 'process == "dispatch.py"'`.
- Domain-specific (preferred): **`python3 tools/schedule/dispatch.py status`** ->
  table of job | last run | next due | last result | cooldown/quota. Raw ledger:
  `jq . ~/.brain/schedule-state.json`.
- Logs: `~/.brain/logs/`.
- Optional GUI: LaunchControl (third-party) browses all LaunchAgents/Daemons.

## Why a dispatcher and not calendar jobs

A laptop is asleep, offline, or lid-closed exactly when a fixed-time job is due.
Instead of N calendar jobs that silently miss, one dispatcher runs often and
asks per job: *overdue? in window? gates pass?* Missed windows just run at the
next eligible tick. Sleep / offline / closed-lid become non-events.

## Components

| # | Component | Path | Notes |
|---|---|---|---|
| 1 | LaunchAgent plist | `~/Library/LaunchAgents/com.brain.schedule.plist` | **User agent, not a daemon** — only the GUI session has Keychain, iCloud, and the apple/container runtime. **No `StartInterval` polling** (work runs at most once/day). `RunAtLoad` + `StartCalendarInterval` anchors spanning the windows (nightly 01:30/04, morning 07:05, catch-up 09/10). launchd reruns a missed anchor on the next wake; the spread anchors give same-day retry if a gate was temporarily down. |
| 2 | Dispatcher | `tools/schedule/dispatch.py` | stdlib only (matches the rest of `tools/`). Reads job table, checks gates, runs due jobs, writes ledger, captures + files output. |
| 3 | Ledger + lock | `~/.brain/schedule-state.json` | per-job last-run timestamps + a `flock` so ticks never overlap and never collide with the enhance loop. Outside the iCloud vault to avoid sync conflict copies. |
| 4 | Job table | inline in `dispatch.py` (or sibling `jobs.json`) | declarative: command, cadence, window, gates, invocation path. |
| 5 | pmset wake | one-time `sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00` | wakes the Mac before the overnight heavy window; AC gate in the dispatcher decides whether to actually run. |

## Invocation paths

- **Tier 0 (pure python):** dispatcher calls `python3 tools/wiki.py <cmd>` directly
  on the host. No container, no network, no fish. Maximally robust.
- **LLM agents:** dispatcher calls `fish -lc "brain-wiki <agent> …"` (login shell
  so the `brain-*` autoloaded functions + PATH + Keychain resolve). `brain-wiki`
  already maps each command to its container profile (reader / author / project),
  so the dispatcher does not re-implement profiles. `brain-wiki` also sets
  `BRAIN_SCAN_SCOPE=none` for the read-only thinking agents
  (`contradict`/`emerge`/`discover`) so they mount **no** external coursework/source
  dirs — they reason over `wiki/` only. Readers that do read across projects (CoS)
  keep the default scan. So every scheduled container is least-privilege on both
  axes: writes scoped to the profile's hole, reads scoped to what the agent uses.
- **VERIFY before build:** selected-CLI session access from a launchd-spawned
  `fish` in the Aqua session (confirm `brain-wiki` can authenticate
  non-interactively).

## Job table (WHICH + WHEN)

| Job | Command | Cadence / window | Gates | Output |
|---|---|---|---|---|
| lint | `wiki.py lint` | **nightly** (batch step 1) | offline-ok, host-native | notify only on errors |
| index | `wiki.py index` (→ `--rebuild` if stale) | **nightly** (batch step 1) | offline-ok, host-native | log |
| qmd update | `qmd update` | **nightly** (batch step 1) | offline-ok, host-native | lexical search index |
| qmd cleanup | `qmd cleanup` | **weekly**, before embedding | offline-ok, host-native, **AC** | remove inactive documents/orphan chunks; compact derived index |
| qmd embed | `qmd embed --timeout 24` | **nightly**, bounded/resumable | offline-ok, host-native, **AC** | semantic vectors |
| links | `wiki.py links --fix` | weekly *(manual — not in the dispatcher; writes wiki/, needs the author profile)* | offline-ok, host-native | log |
| coverage snapshot | `wiki.py coverage --json` | weekly *(manual — not in the dispatcher)* | offline-ok, host-native | feeds enhance |
| **cos brief** | `brain-wiki cos --mode brief` | daily, 07:00 window | online, container, icloud, battery-ok | `wiki/reports/` + macOS notify |
| contradict | `brain-wiki contradict` | weekly, overnight AC window | online, container, icloud, **AC** | `wiki/reports/` |
| emerge | `brain-wiki emerge` | weekly | online, container, icloud | `wiki/reports/` + notify |
| discover | `brain-wiki discover` | weekly | online, container, icloud | `wiki/reports/` + notify |
| verify *(optional)* | `brain-wiki verify --source <changed>` | weekly, on recently-changed source pages | online, container, icloud | report |
| ingest | `brain-wiki ingest --source <new>` | **nightly**, only if `raw/inbox` / `raw/sources` has unprocessed files | online, container, icloud, **AC** | wiki + promote inbox PDF |
| project-runner | `brain-wiki project-run --project <slug>` (one per due, opted-in project) | **nightly**, after the digests | online, container, icloud, **AC** | writes `projects/<slug>/` (applied-not-committed; pre-run snapshot) + roll-up `wiki/reports/` |
| enhance *(opt-in)* | `brain-wiki enhance --iterations 10` | nightly only when `VAULTLENS_SCHEDULE_ENHANCE=1`, last nightly step (capped, not `--forever`) | online, container, icloud, **AC** | writes wiki directly |

**Scheduled (in `build_steps`), in run order:** lint, index, qmd update, qmd cleanup, qmd embed,
ingest, contradict, emerge, discover, project-runner, optional enhance, cos brief. **Documented but not yet wired into the
dispatcher (run manually):** links, coverage snapshot, (optional) verify.

The `project-runner` builder (`_project_runner_targets`) is pure-python: it reads each
project's `AGENDA.md` via `tools/agenda.py`, skips frozen projects, dormant (`enabled: false`), and
review-paused projects, and emits one `project-run --project <slug>` arg-vector per
enabled project that is **due** (capped at `MAX_PROJECTS_PER_NIGHT`). A project is due
when it is enabled AND has either a clear, due task **or** loose `## Inbox` content
awaiting grooming (`agenda.project_is_due` / `inbox_has_groomable_content`) — so routed
CoS proposals and ad-hoc Inbox dumps are picked up the next night even before they have
been groomed into Tasks. The dispatcher
clones each project to `~/.brain/project-snapshots/<date>/` before the run (the apply-don't-commit
undo, since `projects/` is gitignored) and writes one aggregated roll-up. **Egress note:**
research tasks fetch via in-container `python3` bound by the squid allowlist — a task needing a
non-allowlisted host is marked `blocked`, not run. **Host note:** `project-run` routing lives in
the host fish function `~/.config/fish/functions/brain-wiki.fish` (it selects the `project` mount
profile + `BRAIN_WRITE_PATH=projects/<slug>`); that file is outside the vault repo, so re-apply it
after a host reset alongside the reactivation steps below.
**On-demand only — never scheduled** (need human input): `challenge` (a position),
`connect` (two domains), `search` (a query). `emerge`/`discover` may *suggest*
running these, but never auto-fire them.

## Gate definitions (HOW the messy conditions are handled)

| Gate | Detection | Behavior when failing |
|---|---|---|
| online | `nc -z -G 5 $VAULTLENS_LLM_HEALTH_HOST 443` (provider default if unset) | **defer** LLM jobs (ledger not advanced → retried next tick). Tier 0 unaffected. |
| container | `container system status` exits 0 | `container system start` + bounded wait (~60s); else defer LLM jobs. Tier 0 still runs. |
| icloud | `find <input> -flags +dataless` empty, else `brctl download <path>` | defer until materialized. |
| AC | `pmset -g batt` shows `AC Power` | heavy jobs (enhance, contradict) defer; light jobs proceed. |
| battery-ok | battery ≥ ~20% | defer heavy; allow light (cos brief, lint). |
| idle | `ioreg`/`HIDIdleTime` over threshold | enhance only; pause if the user is active. |
| not-already-done | ledger: `now ≥ last_run + period` | skip if recently run. |
| no-overlap | `flock` on the ledger | at most one dispatcher run; one enhance instance. |

### Behavior in the three named scenarios

- **Closed lid:** on AC (clamshell / never-sleep) it runs normally; on battery it
  sleeps and the ledger catches up when you reopen.
- **No connectivity:** Tier 0 maintenance keeps running; every LLM job defers (no
  ollama fallback) and retries on the next tick once `online` passes.
- **Sleep cycles:** the idempotent ledger means any wake triggers exactly one
  catch-up of whatever is overdue. Forced wakes (`pmset repeat wake`) exist only
  to guarantee the overnight heavy window; the AC gate means a battery wake
  (e.g. in a bag) does nothing and the Mac re-sleeps. PowerNap micro-wakes are
  ignored (the container runtime is down / uptime window too small).

## Output, notifications, failure

- **Reports:** dispatcher writes `wiki/reports/scheduled-<job>-<YYYY-MM-DD>.md`
  from successful agent stdout only. Container and runtime diagnostics on stderr
  stay out of successful reports; both streams remain available when a run fails.
  (The vault `wiki/reports/` is gitignored personal content.)
- **Retention:** each tick the dispatcher prunes dated `scheduled-<type>-*.md` to
  the latest `REPORT_RETENTION` (14) per type, except `cos-brief`, which retains
  only the latest generated report. Only `scheduled-*` files are touched — never
  `schedule-status.md` or hand-written synthesis reports.
- **Notifications:** `osascript -e 'display notification …'` (or `terminal-notifier`
  if present) on completion of cos brief / emerge / discover, and on any job error.
- **Logs:** `~/.brain/logs/schedule-<date>.log`; LaunchAgent `StandardOutPath` /
  `StandardErrorPath` to the same dir.
- **Retry semantics:** a failed or gated job does **not** advance its ledger
  timestamp, so it retries next tick. A *succeeded* job advances it. Repeated
  failures (e.g. 3 ticks) raise an error notification rather than looping silently.

## Routed work-items → per-project inboxes (inter-role handoff bus)

Chief of Staff briefs are advisory only. The dispatcher does not route
`proposal::` lines, and strips any legacy final `## Proposals` block before
storing a brief. This prevents daily advice from becoming duplicate tracked work.

The project-runner can still emit
`handoff:: <to-project> | <ask> | <deliverable-ref>` lines after a successful
pass. The dispatcher routes them through `_route_work_items` and one per-tick
`RoutingGuard`:

- self-handoffs are blocked;
- direct reciprocal edges within one tick are blocked;
- total routed items per tick are capped by `MAX_ROUTED_PER_TICK`;
- frozen, unknown, and missing project targets are not routed.

Items carry `[from:<source>]` provenance. A handoff only queues into an inbox
picked up by an already-scheduled project run; it never triggers another ad-hoc
agent run.

Tested: successful-output selection, Chief of Staff proposal stripping,
`parse_handoffs`, `resolve_proposal_dest`, `format_work_item`, and `RoutingGuard`
in `test_schedule.py`; inbox append and due-state behavior in `test_agenda.py`.

## Open implementation questions / risks

1. Keychain reachable from launchd-spawned `fish` (verify before relying on it).
2. Per-profile container cold-start cost (qmd / safe-chain / claude re-seed on
   first launch of each `${devcontainerId}` profile) — the first scheduled
   reader/author run of the day pays this; acceptable, but log it.
3. `pmset repeat wake` needs one-time sudo and cannot itself be AC-conditioned;
   the AC gate in the dispatcher is what enforces "AC only."
4. iCloud eviction of report-target dirs — ensure `wiki/reports/` is materialized
   before writing.

## Rejected / out of scope

- ollama offline fallback for LLM jobs (decision 3: defer instead).
- Scheduling `challenge` / `connect` / `search` (need human input).
- Auto-rewrite "Two-Output Rule" and bi-temporal facts (already rejected for the
  thinking-agent layer; see `[[project-brain-thinking-agents]]`).

## Deactivation / reactivation

To deactivate a deployed scheduler while leaving the plist, dispatcher,
sudoers rule, and ledger in place, turn off only the LaunchAgent and forced
wake:

```sh
launchctl bootout gui/$(id -u)/com.brain.schedule   # stop the agent firing
sudo pmset repeat cancel                             # stop the nightly 01:25 wake
```

To reactivate an already prepared scheduler:

```sh
tools/schedule/install.sh --enable-prepared         # loads the backend stored in the plist
sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00     # restore the overnight wake
launchctl list | grep com.brain                      # confirm loaded
python3 tools/schedule/dispatch.py status            # confirm ledger + backend identity health
```

To change and prepare a backend without starting it, run
`VAULTLENS_LLM_CLI=<claude|codex> tools/schedule/install.sh --prepare-disabled`.
The least-privilege lid-close sudoers rule, if installed, is untouched by
deactivation and needs no action.
