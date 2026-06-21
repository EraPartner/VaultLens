# Scheduled agents — design spec

Status: **implemented 2026-06-01.** Files: `dispatch.py` (the dispatcher),
`com.brain.schedule.plist` (LaunchAgent), `brain-schedule.sudoers` (least-privilege
power rule), `install.sh` (installer), `../tests/test_schedule.py` (21 tests).
Activate with `tools/schedule/install.sh` + the `sudo pmset repeat wake` and
sudoers commands it prints. This file remains the design rationale.

> **2026-06-02 — backend migrated to the Claude plan; scheduler deactivated.**
> The copilot accounts (`talicaddy`, `Noortjekjzecbkjzcebkjczeh`) are no longer
> usable. LLM jobs now run on `--cli claude --model sonnet` (the logged-in
> `claude` CLI = the Claude plan subscription). The two-account failover
> collapsed to a single identity (`ACCOUNTS = ["claude-plan"]`): a Claude
> usage-limit error marks it limited and defers the batch (no second account to
> switch to). The scheduler is currently **deactivated** (LaunchAgent unloaded +
> `pmset repeat` cancelled) — see "Reactivation" at the bottom of this file.
> The copilot prose below is kept as historical rationale; where it says
> "copilot / gpt-5.2 / two accounts," read "claude / sonnet / one identity."

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
5. **Backend (current):** all LLM jobs run on the **Claude CLI pinned to
   `sonnet`** (`--cli claude --model sonnet`), authenticated by the logged-in
   `claude` CLI (the Claude plan subscription). The dispatcher pins the model
   explicitly for determinism. *(Was `--cli copilot --model gpt-5.2` until
   2026-06-02; copilot accounts are no longer usable.)*
6. **Single backend identity (current):** `ACCOUNTS = ["claude-plan"]`. There is
   one Claude subscription auth, so there is no account to fail over to: a Claude
   usage-limit/rate-limit error marks the identity `limited_until` and **defers
   the rest of the LLM batch**, caught up on the next eligible window. *(Was two
   copilot `gh` accounts selected via `BRAIN_GH_ACCOUNT` with switch-on-limit
   failover.)*
7. **Nothing LLM runs per tick.** All LLM work happens in **one nightly batch**;
   the only daily-morning LLM job is the cos brief. The ~30-min tick is purely the
   catch-up gate-checker, never an LLM trigger.
8. **`enhance` is capped at `--iterations 10` per night** (not `--forever`).

## Backend & model

- Invocation: `fish -lc "brain-wiki <agent> --cli claude --model sonnet --effort <low|high>"`.
  Note the `--effort` flag is a **no-op for the Claude CLI** (`EFFORT_MAP[...]["claude"]`
  is empty in `wiki-agent.py`); it is still passed for a uniform invocation shape
  and matters only if the backend ever switches back to copilot.
- Egress: for headless Claude runs the squid allowlist must include
  `api.anthropic.com`. Interactive `claude` already works in-container, so it is
  likely present — **verify before relying on scheduled claude runs.**
- Auth: the Claude CLI uses its own logged-in session (the Claude plan), not a
  forwarded token. **Verify `claude -p` works non-interactively from a
  launchd-spawned `fish` in the GUI session before reactivating** (the same
  Keychain-reachability open question as the copilot token had).

### Account selection & failover

`bin/agent` mints the token **at exec time**: `gh auth token --user
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

## Rate limits & premium-request budget

The scarce resource is the **monthly premium-request quota**, not per-minute
throttling. Two facts that drive the design:
- Copilot premium-request allowances are **plan-dependent and change over time** —
  the dispatcher must not hardcode a number; treat quota as a runtime state.
- **Each agentic run is many model turns** (tool calls), so one `cos brief` or
  `contradict` can cost 5-20+ premium requests, not one. Budget accordingly.

Dispatcher classifies copilot exit/stderr into three failure modes:

| Class | Signal | Behavior |
|---|---|---|
| Transient / network | timeout, 5xx | retry next tick (ledger not advanced) |
| Short rate-limit | 429 / "rate limit" | mark account `limited_until` (backoff 30m -> 1h -> 2h), **switch to other account**, retry job once |
| **Monthly quota exhausted** | "quota" / "premium request" / "upgrade" | mark account `limited_until` (probe again in ~24h — don't compute exact reset), **switch to other account**, retry job once |
| **Both accounts limited** | switch found no healthy account | defer the job + rest of the LLM batch; notify |

The two accounts roughly double the effective monthly budget; with `enhance`
capped at 10 iterations/night the nightly batch is bounded, so two quotas should
cover it comfortably.

Budget-shaping (build into the job table):
- `enhance` capped at **`--iterations 10` per night** (the biggest consumer; no
  `--forever`).
- Heavy digests (contradict/emerge/discover) stay **weekly** (Sunday batch).
- Each agentic run is many model turns, so cos brief uses `--effort low`.

## Concrete schedule

The dispatcher ticks every ~30 min only to check gates + the ledger. Actual work:

**Nightly batch — once per night, ~03:00 (pmset wake 02:55), AC-gated, in order:**
1. `lint` + `index` (offline, host-native pre-check; clean state for enhance)
2. `ingest` **if** `raw/inbox` / `raw/sources` has unprocessed files
   (checked here, **once a night**, not per tick)
3. `enhance --iterations 10` (capped)
4. **Sundays only:** `contradict` + `emerge` + `discover`

All LLM steps: `--cli claude --model sonnet`, deferred if a usage limit is hit
(single identity, no failover) or if offline. The whole batch runs at most once per night; if a night is missed
(battery / asleep), the ledger catches it up on the next AC night.

**Daily morning — ~07:00 window, battery OK:**
- `cos brief` (`--effort low`). The only LLM job outside the nightly batch.

Weekly digests land Sunday night so Monday's brief can reference them.

## Monitoring

- Built-in: `launchctl list | grep com.brain` (loaded? last exit), `launchctl print
  gui/$(id -u)/com.brain.schedule` (full state), `pmset -g sched` (scheduled wakes),
  `log show --last 2h --predicate 'process == "dispatch.py"'`.
- Domain-specific (preferred): **`brain-wiki schedule status`** = `dispatch.py
  --status` -> table of job | last run | next due | last result | cooldown/quota.
  Build this alongside the dispatcher. Raw ledger: `jq . ~/.brain/schedule-state.json`.
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
| 1 | LaunchAgent plist | `~/Library/LaunchAgents/com.brain.schedule.plist` | **User agent, not a daemon** — only the GUI session has Keychain, iCloud, and the apple/container runtime. **No `StartInterval` polling** (work runs at most once/day). `RunAtLoad` + `StartCalendarInterval` anchors spanning the windows (nightly 03/06/09, morning 07:05/10). launchd reruns a missed anchor on the next wake; the spread anchors give same-day retry if a gate was temporarily down. |
| 2 | Dispatcher | `tools/schedule/dispatch.py` | stdlib only (matches the rest of `tools/`). Reads job table, checks gates, runs due jobs, writes ledger, captures + files output. |
| 3 | Ledger + lock | `~/.brain/schedule-state.json` | per-job last-run timestamps + a `flock` so ticks never overlap and never collide with the enhance loop. Outside the iCloud vault to avoid sync conflict copies. |
| 4 | Job table | inline in `dispatch.py` (or sibling `jobs.json`) | declarative: command, cadence, window, gates, invocation path. |
| 5 | pmset wake | one-time `sudo pmset repeat wakeorpoweron MTWRFSU 02:55:00` | wakes the Mac before the overnight heavy window; AC gate in the dispatcher decides whether to actually run. |

## Invocation paths

- **Tier 0 (pure python):** dispatcher calls `python3 tools/wiki.py <cmd>` directly
  on the host. No container, no network, no fish. Maximally robust.
- **LLM agents:** dispatcher calls `fish -lc "brain-wiki <agent> …"` (login shell
  so the `brain-*` autoloaded functions + PATH + Keychain resolve). `brain-wiki`
  already maps each command to its container profile (reader / author), so the
  dispatcher does not re-implement profiles.
- **VERIFY before build:** Keychain access from a launchd-spawned `fish` in the
  Aqua session (should work; confirm `brain-wiki` can mint the copilot/claude
  token non-interactively).

## Job table (WHICH + WHEN)

| Job | Command | Cadence / window | Gates | Output |
|---|---|---|---|---|
| lint | `wiki.py lint` | **nightly** (batch step 1) | offline-ok, host-native | notify only on errors |
| index | `wiki.py index --check` (→ `--rebuild` if stale) | **nightly** (batch step 1) | offline-ok, host-native | log |
| links | `wiki.py links --fix` | weekly *(manual — not in the dispatcher; writes wiki/, needs the author profile)* | offline-ok, host-native | log |
| coverage snapshot | `wiki.py coverage --json` | weekly *(manual — not in the dispatcher)* | offline-ok, host-native | feeds enhance |
| **cos brief** | `brain-wiki cos --mode brief` | daily, 07:00 window | online, container, icloud, battery-ok | `wiki/reports/` + macOS notify |
| contradict | `brain-wiki contradict` | weekly, overnight AC window | online, container, icloud, **AC** | `wiki/reports/` |
| emerge | `brain-wiki emerge` | weekly | online, container, icloud | `wiki/reports/` + notify |
| discover | `brain-wiki discover` | weekly | online, container, icloud | `wiki/reports/` + notify |
| verify *(optional)* | `brain-wiki verify --source <changed>` | weekly, on recently-changed source pages | online, container, icloud | report |
| ingest | `brain-wiki ingest --source <new>` | **nightly**, before enhance, only if `raw/inbox` / `raw/sources` has unprocessed files | online, container, icloud, **AC** | wiki + promote inbox PDF |
| enhance | `brain-wiki enhance --iterations 10` | **nightly**, after ingest (capped, not `--forever`) | online, container, icloud, **AC** | writes wiki directly |

**Scheduled (in `build_steps`):** lint, index, ingest, enhance, contradict,
emerge, discover, cos brief. **Documented but not yet wired into the dispatcher
(run manually):** links, coverage snapshot, (optional) verify.
**On-demand only — never scheduled** (need human input): `challenge` (a position),
`connect` (two domains), `search` (a query). `emerge`/`discover` may *suggest*
running these, but never auto-fire them.

## Gate definitions (HOW the messy conditions are handled)

| Gate | Detection | Behavior when failing |
|---|---|---|
| online | `nc -z -G 5 api.anthropic.com 443` (or `curl --max-time 5`) | **defer** LLM jobs (ledger not advanced → retried next tick). Tier 0 unaffected. |
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
  from captured stdout. (The vault `wiki/reports/` is gitignored personal content;
  fine — these are local artifacts.)
- **Retention:** each tick the dispatcher prunes dated `scheduled-<type>-*.md` to
  the latest `REPORT_RETENTION` (14) per type, so daily cos-brief / weekly digests
  do not pile up. Only `scheduled-*` files are touched — never `schedule-status.md`
  or hand-written reports. The CoS is read-only, so this hygiene lives in the
  dispatcher (which owns report writing), not the agent.
- **Notifications:** `osascript -e 'display notification …'` (or `terminal-notifier`
  if present) on completion of cos brief / emerge / discover, and on any job error.
- **Logs:** `~/.brain/logs/schedule-<date>.log`; LaunchAgent `StandardOutPath` /
  `StandardErrorPath` to the same dir.
- **Retry semantics:** a failed or gated job does **not** advance its ledger
  timestamp, so it retries next tick. A *succeeded* job advances it. Repeated
  failures (e.g. 3 ticks) raise an error notification rather than looping silently.

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

Deactivated 2026-06-02. The plist, dispatcher, sudoers rule, and ledger are all
left in place; only the LaunchAgent and the forced wake were turned off:

```sh
launchctl bootout gui/$(id -u)/com.brain.schedule   # stop the agent firing
sudo pmset repeat cancel                             # stop the nightly 02:55 wake
```

**To reactivate** (after verifying `claude -p` runs non-interactively from a
launchd-spawned login `fish` — see "Backend & model"):

```sh
tools/schedule/install.sh                            # re-copies plist, re-bootstraps, kickstarts one run
sudo pmset repeat wakeorpoweron MTWRFSU 02:55:00     # restore the overnight wake
launchctl list | grep com.brain                      # confirm loaded
python3 tools/schedule/dispatch.py status            # confirm ledger + "claude-plan healthy"
```

`install.sh` is idempotent (it boots out any existing agent first), so it is the
single command to bring the scheduler back. The least-privilege lid-close sudoers
rule, if it was installed, is untouched by deactivation and needs no action.
