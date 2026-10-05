# Scheduled agents — design spec

Status: **implemented 2026-06-01.** Files: `dispatch.py` (the dispatcher),
`com.brain.schedule.plist` (LaunchAgent template), `brain-schedule.sudoers`
(least-privilege power rule template), `render_plist.py` (fills the templates and
validates the result), `install.sh` (installer), `restore_project.py` (undo helper
for project-runner snapshots), and the tests `../tests/test_schedule.py`,
`../tests/test_schedule_recovery.py` and `../tests/test_render_plist.py`.
Activate with `tools/schedule/install.sh` + the `sudo pmset repeat wake` and
sudoers commands it prints. This file remains the design rationale.

**Templates.** The tracked plist and sudoers files carry no host-specific value.
`@BRAIN_ROOT@` (the checkout that holds `render_plist.py`), `@BRAIN_HOME@` (the
rendering account's home) and `@BRAIN_USER@` (that account's name) are filled in by
`render_plist.py`; an unresolved placeholder is an error. The sudoers account name
must be a plain non-root login name, so a hostile value cannot add rules.

**2026-10-03 — native runtime migration.** Scheduled model jobs invoke the
repository's Python launcher directly. Claude and Codex share named access
profiles enforced by Anthropic's whole-process sandbox runtime. The dispatcher
does not start a service or select another execution backend when isolation is
unavailable. Source defaults live in `tools/access-profiles.json`; private
operator overrides live in gitignored `tools/access.local.json`.

## Lid-closed runs (AC-gated keep-awake)

To run the nightly batch with the lid closed and **no external display**, the
dispatcher overrides macOS lid-close sleep with `pmset disablesleep` -- but only
on AC, which makes the flag safe (a closed bag is always on battery) and makes
clamshell mode irrelevant. Per tick:

- **self-heal** first: `pmset -a disablesleep 0` (clears a flag left stuck by a
  hard-killed prior run; no-op otherwise).
- engage only if `on AC AND lid closed (ioreg AppleClamshellState) AND an LLM step
  is due AND online AND runtime available`: `pmset -a disablesleep 1`, run the batch, then in
  a `finally` `pmset -a disablesleep 0` and (if still lid-closed) `pmset sleepnow`.
- battery / lid-open / nothing-due -> never touches `disablesleep`.

**Least privilege:** the entire root surface is three exact pmset argument vectors
(`-a disablesleep 1`, `-a disablesleep 0`, `sleepnow`), granted via
`brain-schedule.sudoers`, rendered for the operator's account by
`install.sh --render-sudoers`, validated with `visudo -cf`, then installed as
`/etc/sudoers.d/brain-schedule`. pmset is
power-management only (no code exec / file access / user change), and any other
pmset call still needs a password. `sudo -n` is used so a missing rule fails fast
(lid-closed nights are skipped + caught up on next AC open) rather than hanging.

## Locked decisions

1. **Mechanism:** a host-side *catch-up dispatcher* fired by a launchd
   LaunchAgent. Its ledger controls catch-up and bounded daily work.
2. **Overnight:** forced wakes via `pmset repeat wake`, but heavy jobs run
   **only on AC** (the dispatcher gates on power; pmset itself cannot).
3. **Offline:** LLM jobs **defer until online** (no ollama fallback). Pure-python
   maintenance (Tier 0) runs offline regardless.
4. Read-only agents never write their own reports; the **dispatcher** captures
   their stdout and writes the dated report. Keeps the agents read-only.
5. **Backend (current):** one provider and a snapshot of resolved role models run an entire batch. The
   dispatcher resolves environment overrides, then shared `tools/llm.local.json`, then
   the Claude default. Roles select standard/deep provider mappings; Codex defaults to Luna for standard roles and Sol for deep roles.
   It never falls back across providers or rereads model preferences mid-batch.
   Invalid provider configuration, model mappings or canonical roles block all
   LLM jobs before their gates or builders run. Status and the mirrored health
   report show the configuration error. Host maintenance, cancellation
   acknowledgement and recovery of a stuck lid-close override remain available;
   blocked jobs stay due for the next tick after the configuration is corrected.
6. **Single backend identity (current):** the ledger identity defaults to
   `<cli>-plan` and can be overridden with `VAULTLENS_LLM_IDENTITY`. A usage or
   rate limit marks that identity `limited_until` and **defers the rest of the
   LLM batch**, caught up on the next eligible window. There is no automatic
   cross-provider or cross-account failover.
7. **Nothing LLM runs per tick.** All LLM work happens in **one nightly batch**;
   the only daily-morning LLM job is the cos brief. A tick (launchd calendar
   anchors plus one run at load) is purely the catch-up gate-checker, never an LLM
   trigger.
8. **Nightly `enhance` is paused by default.** Set
   `VAULTLENS_SCHEDULE_ENHANCE=1` when installing to opt in. When enabled, it is
   capped at `--iterations 5` across the whole wiki per night (not `--forever`).

## Backend and model

- Invocation shape: `<dispatcher-python> tools/agents/wiki-agent.py <agent>
  --access-profile <profile> --cli <claude|codex> --model <model> --effort <effort>`.
  The executable and script path are absolute argument-vector entries; there is
  no login shell or shell command interpolation. The launcher applies the access
  boundary before collecting role context or opening the selected notes.
- Shared configuration: `brain-provider claude|codex` or
  `python3 tools/llm_provider.py select claude|codex` writes gitignored
  `tools/llm.local.json`. Optional `--model MODEL` saves a separate model per provider.
  The next dispatcher process picks up the preference without reinstalling launchd.
- Overrides: `VAULTLENS_LLM_CLI`, `VAULTLENS_LLM_MODEL`,
  `VAULTLENS_LLM_HEALTH_HOST`, and `VAULTLENS_LLM_IDENTITY` take precedence over
  shared configuration. `install.sh` copies only explicitly supplied override variables
  into the LaunchAgent. Clear a previous provider pin by preparing the plist with these
  variables unset. Broad nightly wiki enhancement has a separate opt-in,
  `VAULTLENS_SCHEDULE_ENHANCE=1`; changing that installed flag requires preparation again.
- Preview only: `tools/schedule/install.sh --render /tmp/brain-schedule.plist` validates and
  renders a plist (placeholders filled for this checkout and account) without installing
  or loading it. `--render-sudoers OUTPUT` renders the sudoers template the same way. Invalid provider configuration fails
  before the installer changes an existing job. Validation reads `tools/llm.local.json`
  beside the dispatcher targeted by `ProgramArguments`, even when the installer runs
  from another checkout. Missing or ambiguous dispatcher targets are rejected.
  The installer requires Python 3.11 or newer and renders the selected executable
  (`BRAIN_PYTHON`, when supplied) into the LaunchAgent. `RunAtLoad` starts the first
  gate check after bootstrap; installation does not kill and restart that run.
- Dispatcher preview: `python3 tools/schedule/dispatch.py run --dry-run` probes due work
  and existing gates. It does not request iCloud downloads, write logs,
  acquire a persistent lock, or change the ledger. An unavailable runtime remains a
  failed gate in this preview.
- Defaults: Claude role profiles select Sonnet or Opus; Codex selects GPT-6 Luna or GPT-6.1 Sol.
  `tools/model-profiles.json` defines the mappings; local `profiles` in `tools/llm.local.json`
  override them per provider. Global model overrides still take precedence. Scheduled effort
  overrides remain explicit, and all resolved role models are frozen for the batch.
  Claude uses `api.anthropic.com` and Codex uses `chatgpt.com` for the coarse online gate.
- Auth is owned by the selected CLI's login. The native runtime stages the
  minimum provider login state; unrelated credentials remain outside its read
  boundary. Verify a bounded native run from the launchd environment before
  enabling unattended jobs. Credentials are never selected by the scheduler.

### Scheduled access profiles

The launcher owns policy compilation. The dispatcher uses the centralized
`default_access_profile` mapping and passes the chosen name explicitly.

| Jobs | Access profile | Output authority |
|---|---|---|
| contradict, emerge, discover | `wiki-read` | Advisory stdout; host files the report |
| cos-brief | `cos-read` | Approved operator/project context; advisory stdout |
| ingest, enhance | `wiki-write` | Wiki changes only; approved sources stay protected |
| project-runner | `project-write` with `--project <slug>` | That one project only |
| verify (manual) | `source-read` | Approved source and wiki context; advisory stdout |

Scheduled ingest also grants its exact selected source using `--read-path`.
Its builder ignores source and inbox links, including linked directories, so a
link cannot turn consent-queue material into an approved input. A source PDF
remains due until a wiki source page cites it; ingest does not promote or move
raw files. Network research is separate policy and is disabled unless explicitly
configured. Every profile's search index must contain only the material exposed
to that run. Scheduled host `qmd update`/`cleanup` maintain the operator's index;
they do not grant it to a model session.

## Rate limits and request budget

The available quota is provider- and plan-dependent, so the dispatcher does not
hardcode a number. Two facts drive the design:
- Treat quota as runtime state reported by the selected CLI.
- **Each agentic run is many model turns** (tool calls), so one `cos brief` or
  `contradict` can consume many requests, not one. Budget accordingly.

The dispatcher classifies CLI exit output and recovery state as follows:

| Class | Signal | Behavior |
|---|---|---|
| Transient / network | 5xx or ordinary CLI failure | retry next tick (ledger not advanced) |
| **Unconfirmed cancellation** | deadline, signal death, interruption, or unresolved prior invocation | cancel the host process group when possible; preserve logs; persist a latch blocking remaining and later LLM work |
| Short rate-limit | whole-token `429` / "rate limit" / "too many requests" | mark backend identity `limited_until` (backoff 30m -> 1h -> 2h); defer |
| **Quota exhausted** | "quota" (also inside codes such as `insufficient_quota`) / "premium request" / "upgrade", Claude session/weekly/usage limits, "hit your limit", or spend/credit limits | mark backend identity `limited_until` (probe again in ~24h; do not compute an exact reset); defer |
| **Backend limited** | selected identity is cooling down | defer the job and rest of the LLM batch; notify |

Classification ignores Python traceback frame lines (and the source line under each) and
matches `quota` and `429` only as whole tokens, so a line number or identifier in a crash
trace cannot start a cooldown for the whole LLM batch.

There is one configured backend identity. The dispatcher never switches provider
or credentials automatically.

Scheduled launchers run from the dispatcher's checkout root, through the absolute
`tools/agents/wiki-agent.py` path. A missing launcher fails in that checkout; caller
shell configuration cannot select another private vault as a fallback.

On a dispatcher deadline, the native launcher's process group receives SIGTERM, then SIGKILL.
Output goes to private temporary log files under the schedule log directory, so descendants cannot
hold a captured pipe open indefinitely. Partial stdout/stderr paths are recorded in the persistent
`cancellation_pending` ledger entry. Successful invocations remove these extra capture files;
failure/timeout captures remain available for operator review.

An in-flight marker is persisted before every model launch and cleared only after normal wrapper
completion. A dispatcher interruption or restart with that marker blocks unattended model work;
a signal-terminated wrapper also retains the block. An existing unreadable or corrupt ledger
fails closed instead of resetting scheduler state, as does a ledger whose `last_ok` or
`limited_until` is not a timezone-aware ISO time. Recover that ledger explicitly before resuming.

Host process exit alone does not establish that every descendant stopped. The
dispatcher therefore keeps the same conservative latch for the remaining batch
and future unattended model runs. Normal host health steps may still run. No
global process-name search or unrelated session shutdown is part of cancellation.

Recovery requires the operator to inspect the partial logs, verify that the timed-out inner
workload has stopped (and inspect any partial project edits), then run:

```sh
python3 tools/schedule/dispatch.py acknowledge-cancellation --confirmed-inner-stopped
```

This command is an explicit operator attestation, not a runtime verification. Agents must not
invoke it to unblock their own work. It takes the dispatcher lock and refuses to clear the latch
while another tick holds the lock. `dispatch.py status` displays unresolved cancellation details.

Budget-shaping (build into the job table):
- `enhance` is **off by default**. When explicitly enabled, it is capped at
  **`--iterations 5` across the whole wiki per night** (the biggest consumer; no `--forever`).
- Heavy digests (contradict/emerge/discover) stay **weekly** (Sunday batch).
- Each agentic run is many model turns, so cos brief uses `--effort low`.

## Concrete schedule

The dispatcher ticks at the plist's calendar anchors (01:30, 04:00, 07:05, 09:00, 10:00,
plus once at load) only to check gates + the ledger. Actual work:

**Nightly batch — once per night, window 01:00-11:00, first anchor 01:30 (pmset wake 01:25), AC-gated, in order:**
1. `lint` + `index` + `qmd update` (offline, host-native pre-check), then weekly
   `qmd cleanup` for inactive documents and orphan chunks on AC power. Semantic
   embedding is manual because it is too resource-intensive for the scheduled host.
2. `ingest` **if** `raw/inbox` / `raw/sources` has unprocessed files
   (checked here, **once a night**, not per tick)
3. **Sundays only:** `contradict` + `emerge` + `discover` (read-only digests)
4. `project-runner` — one invocation per non-frozen, opted-in (`enabled: true`) project with a
   due `AGENDA.md` task (capped `MAX_PROJECTS_PER_NIGHT`). User-facing work claims
   budget before any optional enhancement; writes `projects/<slug>/` (not wiki/),
   applied-not-committed, with a pre-run snapshot per project for undo
5. **Only when `VAULTLENS_SCHEDULE_ENHANCE=1`:** `enhance --iterations 5
   --strategy alternate` across the whole wiki, last in the nightly batch and before the
   morning-only brief

All LLM steps use the configured `--cli` and optional `--model`, and defer if a
usage limit is hit or if offline. The whole batch runs at most once per night; if a night is missed
(battery / asleep), the ledger catches it up on the next AC night.

**Daily morning — 07:00-12:00 window, battery OK:**
- `cos brief` (`--effort low`). The only LLM job outside the nightly batch.

Weekly digests land Sunday night so Monday's brief can reference them. A weekly job is
due once at least 7 calendar days have passed since its last success and the tick falls on
a Sunday, or at 8 or more days if that Sunday was missed. Days are counted as local
calendar dates, so an early-in-the-day tick does not push the job to a later weekday.

## Monitoring

- Built-in: `launchctl list | grep com.brain` (loaded? last exit), `launchctl print
  gui/$(id -u)/com.brain.schedule` (full state), `pmset -g sched` (scheduled wakes),
  `log show --last 2h --predicate 'process == "dispatch.py"'`.
- Domain-specific (preferred): **`python3 tools/schedule/dispatch.py status`** ->
  table of job | last run | next due | last result | cooldown/quota. Raw ledger:
  `jq . ~/.brain/schedule-state.json`.
- Logs: `~/.brain/logs/`. The dispatcher deletes its own `schedule-<date>.log` and
  `agent-*.stdout.log`/`agent-*.stderr.log` files older than `LOG_RETENTION_DAYS` (30)
  each tick. It keeps agent captures while a cancellation is unresolved, because the
  recovery message points at them, and never touches launchd's own `launchd.*.log`.
- Optional GUI: LaunchControl (third-party) browses all LaunchAgents/Daemons.

## Why a dispatcher and not calendar jobs

A laptop is asleep, offline, or lid-closed exactly when a fixed-time job is due.
Instead of N calendar jobs that silently miss, one dispatcher runs at several
anchors a day and asks per job: *overdue? in window? gates pass?* Missed windows just run at the
next eligible tick. Sleep / offline / closed-lid become non-events.

## Components

| # | Component | Path | Notes |
|---|---|---|---|
| 1 | LaunchAgent plist | `~/Library/LaunchAgents/com.brain.schedule.plist` | User LaunchAgent in the GUI session for provider login and iCloud access. `RunAtLoad` + `StartCalendarInterval` anchors span the nightly and morning windows. launchd reruns missed anchors on wake; later anchors permit same-day gate retries. |
| 2 | Dispatcher | `tools/schedule/dispatch.py` | stdlib only (matches the rest of `tools/`). Reads job table, checks gates, runs due jobs, writes ledger, captures + files output. |
| 3 | Ledger + lock | `~/.brain/schedule-state.json`, `~/.brain/schedule.lock` | per-job last-run timestamps in the ledger; a `flock` on the separate lock file so dispatcher ticks never overlap. Outside the iCloud vault to avoid sync conflict copies. |
| 4 | Job table | `build_steps()` in `dispatch.py` | declarative: command, cadence, window, gates, invocation path. |
| 5 | pmset wake | one-time `sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00` | wakes the Mac before the overnight heavy window; AC gate in the dispatcher decides whether to actually run. |

## Invocation paths

- **Tier 0 (pure python):** dispatcher calls `python3 tools/wiki.py <cmd>` directly
  on the host. No provider session is involved.
- **LLM agents:** dispatcher calls the native Python launcher with its frozen
  provider, model and effort, plus a named access profile. The launcher creates a
  selected workspace and applies the sandbox before document context is read.
  The policy and provider adapters are shared with interactive Brain launchers.
- **Preflight:** runtime availability is a read-only gate. A missing runtime,
  unsupported pinned version or invalid access configuration defers model jobs;
  the dispatcher never installs a dependency or substitutes unconfined execution.

## Completion and native session lifetime

A source PDF remains due for ingestion until a `wiki/sources/` page cites that
PDF. Files in `raw/sources-text/` only show that preprocessing ran; they do not
prove that a source page or concept updates were produced.

Scheduled lint emits JSON. Exit code 1 with a valid report containing lint
errors counts as a completed check and triggers the findings notification.
Other nonzero exits, malformed reports, missing executables, timeouts, and
failed index or qmd commands retain the prior `last_ok`, increase the failure
streak, and remain eligible for retry. Index failures must not be treated as
content findings.

Every scheduled model invocation owns its native launcher process group. It does
not discover global runtime sessions or stop concurrent interactive work. The
process group is the cancellation target; unresolved descendant termination
still requires explicit recovery, as described above. There is no shared service
lifetime to manage between scheduled jobs.

## Job table (WHICH + WHEN)

| Job | Command | Cadence / window | Gates | Output |
|---|---|---|---|---|
| lint | `wiki.py lint --json` | **nightly** (batch step 1) | offline-ok, host-native | notify only on errors |
| index | `wiki.py index --rebuild` | **nightly** (batch step 1) | offline-ok, host-native | log |
| qmd update | `qmd update` | **nightly** (batch step 1) | offline-ok, host-native | lexical search index |
| qmd cleanup | `qmd cleanup` | **weekly**, after update | offline-ok, host-native, **AC** | remove inactive documents/orphan chunks; compact derived index |
| qmd embed | `qmd embed` | **manual only** | run explicitly on a suitable machine | semantic vectors |
| links | `wiki.py links --fix` | weekly *(manual — not in the dispatcher; writes wiki/, needs the author profile)* | offline-ok, host-native | log |
| coverage snapshot | `wiki.py coverage --json` | weekly *(manual — not in the dispatcher)* | offline-ok, host-native | feeds enhance |
| **cos brief** | native `cos --mode brief` | daily, 07:00-12:00 window | online, runtime, icloud, battery-ok | `wiki/reports/agents/scheduled/` + macOS notify |
| contradict | native `contradict` | weekly, overnight AC window | online, runtime, icloud, **AC** | `wiki/reports/agents/scheduled/` |
| emerge | native `emerge` | weekly | online, runtime, icloud, **AC** | `wiki/reports/agents/scheduled/` + notify |
| discover | native `discover` | weekly | online, runtime, icloud, **AC** | `wiki/reports/agents/scheduled/` + notify |
| verify *(optional)* | native `verify --source <changed>` | weekly, on recently-changed source pages | online, runtime, icloud | report |
| ingest | native `ingest --source <new>` | nightly, only if approved source/inbox files are unprocessed | online, runtime, icloud, **AC** | wiki changes; source protected |
| project-runner | native `project-run --project <slug>` (one per due, opted-in project) | nightly, after the digests | online, runtime, icloud, **AC** | one project (applied without commit; pre-run snapshot) + roll-up |
| enhance *(opt-in)* | native `enhance --iterations 5 --strategy alternate` | nightly only when `VAULTLENS_SCHEDULE_ENHANCE=1`, last nightly step | online, runtime, icloud, **AC** | wiki changes |

In this table, **native** means the absolute Python launcher invocation described
above, with the matching named access profile and frozen batch arguments.

**Scheduled (in `build_steps`), in run order:** lint, index, qmd update, qmd cleanup, ingest,
contradict, emerge, discover, project-runner, optional enhance, cos brief. **Documented but not yet wired into the
dispatcher (run manually):** links, coverage snapshot, (optional) verify.

The `project-runner` builder (`_project_runner_targets`) is pure-python: it reads each
project's `AGENDA.md` via `tools/agenda.py`, skips frozen projects, dormant (`enabled: false`), and
review-paused projects, and emits one `project-run --project <slug>` arg-vector per
enabled project that is **due** (capped at `MAX_PROJECTS_PER_NIGHT`). A project is due
when it is enabled AND has either a clear, due task **or** loose `## Inbox` content
awaiting grooming (`agenda.project_is_due` / `inbox_has_groomable_content`) — so routed
handoffs and ad-hoc Inbox dumps are picked up the next night even before they have
been groomed into Tasks. The dispatcher
clones each project to `~/.brain/project-snapshots/<date>/` before the run (the apply-don't-commit
undo, since `projects/` is gitignored) and writes one aggregated roll-up. Snapshots are staged
and published only after a complete copy. A sibling `.<project>.complete.json` marker
records the published directory's identity; reuse requires that matching marker.
Existing legacy snapshots without a marker, malformed markers, and marker publication
failures defer the writer and preserve existing snapshot contents for operator review.
The marker stays outside the snapshot tree so restoration contains only project files.
A failed snapshot defers that project's writer
and records `snapshot-failed`; other projects with valid snapshots may still run.
The roll-up prints a `tools/schedule/restore_project.py --snapshot ... --project ...`
command for each executed project. The helper first copies the complete snapshot into a
staging directory, then retains the current project under `projects/.restore-backups/`
and replaces the whole project tree. This restores removed files and removes run-created
files from the active tree while preserving current operator edits in the retained backup.
Copy failures leave the current project untouched; installation failures attempt to restore
it immediately. Retained restore backups are never pruned by the dispatcher.
**Egress note:** research domains belong to an explicit access-profile policy.
An ordinary note-analysis run permits only configured provider/login endpoints.
A task requiring another host stays blocked until an approved research profile
permits it. Project write scope comes from `--project <slug>` and the runtime
policy, so scheduler operation does not depend on host shell functions.
**On-demand only — never scheduled** (need human input): `challenge` (a position),
`connect` (two domains), `search` (a query). `emerge`/`discover` may *suggest*
running these, but never auto-fire them.

## Gate definitions (HOW the messy conditions are handled)

| Gate | Detection | Behavior when failing |
|---|---|---|
| online | `nc -z -G 5 $VAULTLENS_LLM_HEALTH_HOST 443` (provider default if unset) | **defer** LLM jobs (ledger not advanced → retried next tick). Tier 0 unaffected. |
| runtime | `local_runtime.runtime_available(root=ROOT, cli=CLI)` validates the installed native boundary | Defer model jobs; never auto-install, start a service or fall back. Host maintenance still runs. |
| icloud | `wiki/` exists; a normal tick asks `brctl download wiki/reports` (best effort, not awaited), a dry run only checks that `wiki/reports/` exists | defer when `wiki/` is missing; materialization is not verified. |
| AC | `pmset -g batt` shows `AC Power` | qmd cleanup, ingest, contradict, emerge, discover, project-runner and enhance defer; lint, index, qmd update and the cos brief proceed. |
| battery-ok | battery ≥ 20% (`MIN_BATTERY_PCT`); desktops and unreadable output pass | only the cos brief carries this gate; it defers below the threshold. |
| not-already-done | ledger: daily = no success yet on today's local date; weekly = see Concrete schedule | skip if recently run. |
| no-overlap | `flock` on `~/.brain/schedule.lock` | At most one dispatcher run. Manual `brain-wiki enhance` processes do not acquire this host lock and can overlap scheduled enhancement; do not run them during the nightly window. |

### Behavior in the three named scenarios

- **Closed lid:** on AC (clamshell / never-sleep) it runs normally; on battery it
  sleeps and the ledger catches up when you reopen.
- **No connectivity:** Tier 0 maintenance keeps running; every LLM job defers (no
  ollama fallback) and retries on the next tick once `online` passes.
- **Sleep cycles:** the idempotent ledger means any wake triggers exactly one
  catch-up of whatever is overdue. Forced wakes (`pmset repeat wake`) exist only
  to guarantee the overnight heavy window; the AC gate means a battery wake
  (e.g. in a bag) does nothing and the Mac re-sleeps. PowerNap micro-wakes
  do not create extra runs; the ledger still controls whether work is due.

## Output, notifications, failure

- **Reports:** dispatcher writes `wiki/reports/agents/scheduled/scheduled-<job>-<YYYY-MM-DD>.md`
  from successful agent stdout only. Launcher and runtime diagnostics on stderr
  stay out of successful reports; both streams remain available when a run fails.
  The reserved `wiki/reports/agents/` subtree is excluded from every access
  profile and scoped search. The readable `wiki/reports/schedule-status.md`
  contains health metadata only; raw errors and cancellation output remain in
  host diagnostics. (The vault `wiki/reports/` is gitignored personal content.)
- **Retention:** each tick the dispatcher prunes dated `scheduled-<type>-*.md` to
  the latest `REPORT_RETENTION` (14) per type, except `cos-brief`, which retains
  only the latest generated report. Only regular `scheduled-*` files inside
  `wiki/reports/agents/scheduled/` are touched; linked files, older reports in
  `wiki/reports/`, `schedule-status.md`, and hand-written reports are untouched.
  During migration, archive reviewed legacy dated outputs under
  `wiki/reports/agents/scheduled/legacy/` with recoverable copies and a hash ledger.
  That archive is excluded from agent reads and is outside automatic retention.
- **Host write scope:** report writes require this deployment's real
  `wiki/reports/` directory. Content writes use its `agents/scheduled/` subtree;
  only the metadata status page uses the parent directory. Directory descriptors
  and atomic replacement reject linked directories and report targets. Model
  output cannot choose a filename or redirect a host report write.
- **Notifications:** `osascript -e 'display notification …'` when a step that files a
  report finishes (contradict, emerge, discover, project-runner, cos brief), when lint
  reports findings, when handoffs are routed, when a usage limit defers the batch, and
  once when a job has failed `FAIL_STREAK_ALERT` (3) runs in a row. Other single failures
  appear only in the log and `schedule-status.md`.
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
- only real, non-frozen projects with `AGENDA.md enabled: true` receive handoffs;
- path traversal, absolute targets and links in project/agenda paths are rejected.

Items carry `[from:<source>]` provenance. A handoff only queues into an inbox
picked up by an already-scheduled project run; it never triggers another ad-hoc
agent run.
Routed text is untrusted model output: the receiving runner's role files every
`[from:<source>]` item as `needs-clarification`, so it runs only after operator approval.
That gate is enforced by the role, not by code.

Tested: successful-output selection, Chief of Staff proposal stripping (the proposal
parser and router were removed with the unused routing path),
`parse_handoffs`, `resolve_proposal_dest`, `format_work_item`, and `RoutingGuard`
in `test_schedule.py`; inbox append and due-state behavior in `test_agenda.py`.

## Open implementation questions / risks

1. Provider login and runtime executable discovery in launchd's reduced
   environment need a bounded deployment check.
2. Standalone sandbox runtime behavior and platform support remain a deployment
   requirement. Fixture tests establish routing and recovery logic; they do not
   prove operating-system isolation or provider authentication.
3. `pmset repeat wake` needs one-time sudo and cannot itself be AC-conditioned;
   the AC gate in the dispatcher is what enforces "AC only."
4. iCloud eviction of report-target dirs — ensure `wiki/reports/agents/scheduled/` is materialized
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
tools/schedule/install.sh --enable-prepared         # validates then loads the prepared job
sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00     # restore the overnight wake
launchctl list | grep com.brain                      # confirm loaded
python3 tools/schedule/dispatch.py status            # confirm ledger + backend identity health
```

To switch the shared backend without starting it, run `brain-provider claude|codex`.
To prepare an explicitly pinned backend without starting it, run
`VAULTLENS_LLM_CLI=<claude|codex> tools/schedule/install.sh --prepare-disabled`.
The least-privilege lid-close sudoers rule, if installed, is untouched by
deactivation and needs no action.
