# Brain (VaultLens) devcontainer

Hardened sandbox for running the wiki-builder agents on this vault. The Python
orchestrator (`tools/wiki.py`, `tools/agents/wiki-agent.py`) and the **claude**
CLI it drives, plus the **qmd** local search engine, run inside the container.
The agents run with `--dangerously-skip-permissions` / headless flags, so the
container is the isolation boundary.

Modelled on the Vision/Watchman devcontainers; the differences are this
project's stack (no web app, no database), the agent toolchain, and the
qmd cache seeding.

## What's inside

| Component | Where it runs | Notes |
| --- | --- | --- |
| Python 3.12 orchestrator | `tools/wiki.py`, `tools/agents/wiki-agent.py` | stdlib only, no pip deps |
| `claude` CLI | npm `@anthropic-ai/claude-code` (Dockerfile, build-time) | default agent (`sonnet`) |
| `qmd` search (MCP) | npm `@tobilu/qmd` (Dockerfile, build-time) | exposed to claude via `.mcp.json`; search-only — index snapshotted + models live-linked from host on start |
| PDF ingest | `pdftotext` (poppler) + `qpdf` (apt) | for `wiki.py preprocess` |
| GitHub CLI (`gh`) | apt | push over HTTPS via `GH_TOKEN` |

Base image: digest-pinned `debian:bookworm-slim`. Container user: `dev` (UID 1000).

> **Why the CLIs install at build time.** The CLIs (`claude`/`qmd`) and the
> Node/CPython toolchains install in the **`Dockerfile`**,
> not `post-create`/`post-start` (Compose has no devcontainer-feature system, so
> the Dockerfile installs them itself). qmd pulls native modules (`better-sqlite3`,
> `tree-sitter`, `node-llama-cpp`) that need a C/C++ toolchain and Node headers
> from `nodejs.org`. The lifecycle hooks run *after* the egress lock is up, so an
> install there would fail (no toolchain, and `nodejs.org` isn't allowlisted). The
> build phase has an unrestricted network and apt-installs `build-essential`. Node
> and the CPython build are downloaded from their official hosts and **verified
> against the published `SHASUMS256.txt` / `SHA256SUMS`** before extraction; the
> base image is digest-pinned. To change CLI or toolchain versions, edit the
> `RUN npm install -g …` line or the `NODE_VERSION` / `PY_VERSION` ARGs in the
> `Dockerfile` and rebuild.

## How to use

This sandbox runs on **Docker Compose** (no devcontainer CLI). Prerequisite:
Docker Desktop with Compose v2 (`docker compose version`). Each `brain-*` wrapper
calls `.devcontainer/bin/agent`, which resolves the capability profile, selects
the compose files (`compose.yaml` + `compose.scoped.yaml` + a generated RW-hole
override), runs `docker compose up -d --build`, replays the lifecycle, forwards
LLM tokens, and auto-syncs `~/.claude` on exit.

Host fish functions (in `~/.config/fish/functions/`, version-controlled in your
dotfiles). They walk up to find `.devcontainer/`, fall back to `$BRAIN_HOME`,
then the vault path:

| Command | Runs in the container |
| --- | --- |
| `brain-cos [--mode brief\|status\|surface\|inbox] [--project slug]` | Chief of Staff brief / status / commitments / inbox triage (reader profile) |
| `brain-wiki <agent> [args]` | `python3 tools/agents/wiki-agent.py <agent> …` (agent = quality/verify/ingest/contradict/search/enhance/cos/challenge/connect/emerge/discover) |
| `brain-wiki <subcommand> [args]` | `python3 tools/wiki.py <subcommand> …` (coverage, preprocess, search, log, …) |
| `brain-claude [args]` | `claude …` (opens in the subdir you ran it from, e.g. `projects/ict-recht`) |
| `brain-shell [cmd]` | a bash shell, or an arbitrary in-container command |
| `brain-claude-sync pull\|push\|status` | sync `~/.claude` between host and container |

Examples:

```fish
brain-cos                                        # daily chief-of-staff brief
brain-cos --mode status --project thesis         # thesis status report
brain-cos --mode surface                         # surface all commitments
brain-cos --mode inbox                           # triage raw/inbox/ items
brain-wiki enhance --strategy coverage           # wiki agent runner
brain-wiki enhance --background --forever &      # long detached loop (survives via the keep-alive)
brain-wiki coverage --json                       # tools/wiki.py subcommand
brain-claude --dangerously-skip-permissions
brain-shell .devcontainer/bin/doctor             # readiness check
```

**Working directory.** `brain-claude`/`brain-shell`
open in the in-container path matching your host `PWD`, so running `brain-claude`
from `projects/ict-recht` lands in `/workspaces/Brain/projects/ict-recht` and
picks up that project's `CLAUDE.md` / `project.md`. `brain-wiki` and
`brain-cos` always run at the workspace root (they set `BRAIN_NO_CHDIR=1`).

### Credentials

No GitHub token is forwarded at all (commits/pushes happen on the host; the
in-container `.git` is read-only). The only credential entering the container is
the Claude LLM auth forwarded by the wrapper (`brain-claude-code-token` Keychain
entry via `sandbox_forward_llm_creds`).

## One-time host setup

```sh
# 1) Docker Desktop with Compose v2 (verify: docker compose version)

# 2) Claude OAuth token (long-lived; uses your subscription)
claude setup-token        # prints sk-ant-… ; copy it
security add-generic-password -s brain-claude-code-token -a "$USER" -w   # paste it
```

The `brain-*` wrappers retrieve this on every invocation and forward it at
`docker compose exec` time. Only LLM auth crosses the boundary — **no git push
credential**: `CLAUDE_CODE_OAUTH_TOKEN` (from `brain-claude-code-token`).
`GH_TOKEN`/`GITHUB_TOKEN` are **not** forwarded and the root `.git` is mounted
read-only, so no container can commit, rewrite history, or push (do that on the
host). No long-lived credential is ever written to a plaintext file on the host.

> **Keychain "Always Allow".** The first `security` read pops the standard
> prompt. **Allow each time** is the more defensible choice for a
> host-compromise threat model; **Always Allow** trades that for convenience.
> Change later via Keychain Access.app → entry → Access Control.

**Rotating:** `security delete-generic-password -s <service>` then redo the step.

## Network policy

Egress is enforced by the root entrypoint on every start:

1. **In-container SNI proxy** (`squid`, peek+splice). All outbound HTTP(S) must
   traverse squid on `127.0.0.1:3128`; it splices allowlisted hostnames
   (end-to-end TLS, no MITM) and terminates the rest. `HTTP(S)_PROXY` is set and
   `NODE_USE_ENV_PROXY=1`, so `claude`, `qmd`, `npm`,
   `git`, `gh`, and `python` all egress through it.
2. **`iptables` egress lock**: only the `proxy` UID may originate outbound
   packets (including DNS to the embedded Docker resolver, so non-proxy processes
   can't tunnel data out via DNS queries). IPv6 is default-deny. Denied egress is
   rate-limited-logged (`dmesg | grep egress-deny`).

**Allowlist** (`allowlist.txt`, baked into the image — edit and rebuild to
change): Anthropic + Claude Code, `registry.npmjs.org`, GitHub, the safe-chain
malware list, and context7.

**The one egress hole:** the firewall allows direct TCP to
`host.docker.internal:11434` so the container reaches the **host Ollama daemon**
(the Mac has the weights + Metal GPU). `OLLAMA_HOST` points there and `NO_PROXY`
keeps it off squid. This is the only host service the container can touch.

**Supply-chain scanning:** `post-create` installs Aikido safe-chain and
`BASH_ENV` wires it into every shell, so `npm`/`pip` installs the agents run
mid-session are screened against `malware-list.aikido.dev`.

**Launch-integrity pins:** the Dockerfile records the SHA-256 of the agent's
executables (`node`/`npm`/`claude`/`gh`/`git`/`python3`/`qmd`)
into `binary-pins.txt`, and `brain-verify-pins` re-checks them at every launch,
aborting on drift. This reliably **detects unintended upstream tool upgrades**. It
is a true tamper defense only for the root-owned binaries (`node`/`git`/`python3`);
the two npm CLIs live in a dev-writable prefix (so runtime `npm i -g` / safe-chain
can write it), so treat their lines as drift-detection, not a hard anti-tamper
guarantee. See the comment atop `bin/verify-pins`.

**Observability:** the definitive log is `/var/log/squid/access.log`
(`dev`-readable; `TCP_DENIED`/`NONE` = blocked). Run `.devcontainer/bin/doctor`
for a one-shot readiness check (egress lock, toolchain, qmd, ollama, auth).

**Not covered:** WebSearch/WebFetch run agent-side, not through the proxy. ECH
(encrypted SNI) destinations fail closed.

## Persistence

| Source | Container path | Holds |
| --- | --- | --- |
| `brain-claude-<id>` (volume) | `/home/dev/.claude` | Claude config, seeded from the sanitized stage |
| `brain-local-<id>` (volume) | `/home/dev/.local` | `~/.local/share` app state |
| `brain-config-<id>` (volume) | `/home/dev/.config` | qmd / gh config |
| `brain-cache-<id>` (volume) | `/home/dev/.cache` | writable qmd index snapshot (models symlink to the seed) |
| `.devcontainer` (host) | `/workspaces/Brain/.devcontainer` | **RO** overlay on the rw workspace so the sandbox config + host launcher can't be rewritten from inside (see Safety note) |
| `~/.claude-sandbox/stage/brain` (host) | `/home/dev/.claude-stage` | **RO** sanitized stage (no secrets) |
| `~/.cache/qmd` (host) | `/home/dev/.qmd-seed` | **RO** live source: index snapshotted on start, models symlinked |
| `~/.claude/projects/-…-Brain/memory` (host) | `/home/dev/.claude-memory-seed` | **RO** seed of host project memory; `post-start` mirrors it into the `.claude` volume, and the launcher pushes container edits back **only for interactive sessions** (see the project-memory note below) |
| `~/Documents/School` (host) | same path | **RO** project source for symlinked coursework |
| `~/Code/{Vision,Watchman}` (host) | same path | **RO** project source |
| `~/Documents/Personal/Scans/Finance/The Mad King` | same path | **RO** project source |

### Project source mounts (symlinks)

Projects under `projects/` symlink to source dirs that live **outside** the vault
(e.g. `projects/ict-recht/ICT Recht -> ~/Documents/School/ICT Recht`). Those
symlinks store absolute host paths, so they dangle in the container unless the
target exists at the **same** path. Each unique target ancestor is therefore
bind-mounted **read-only at its identical absolute path** (the four rows above),
which makes the existing symlinks resolve transparently — no symlink rewriting.
Egress stays locked, so mounted source can be read for wiki-building but not
exfiltrated; read-only means the agents can't alter your real coursework/source.

**Adding a project with a new symlink target:** if its target isn't already
under one of the mounted ancestors, add a matching
read-only bind mount under `services.app.volumes` in `compose.yaml`
and rebuild. List current targets with:
`find projects -maxdepth 3 -type l -exec readlink {} \; | sort -u`.

The vault is bind-mounted at `/workspaces/Brain`, so wiki edits appear on the
host (and sync via iCloud) immediately. The Claude config is **not** live-shared
(a raw bind would expose host secrets and corrupt `~/.claude.json` under
concurrent writes): the container gets its own writable copy seeded from the
sanitized stage. `brain-claude` now syncs automatically — it pulls the host
config into the running container on launch, and on **session exit** runs the
hardened `brain-claude-sync push` for you, so container-side config edits
land back on the host without a manual command. Pushing only after the session
ends keeps a single writer (no `~/.claude.json` race). It's gated to interactive
runs (`claude`/`bash`); set `BRAIN_AUTOSYNC=1` to force it for headless agent
runs, or `BRAIN_AUTOSYNC=0` to disable. `brain-claude-sync push|pull|status`
remains the manual fallback.

**Project memory** (the `~/.claude/.../memory` files Claude auto-loads as
instructions) is *not* live-shared either, for a specific security reason: a live
read-write bind would let a hostile source document processed by a **headless**
agent plant persistent prompt-injection into your **host** Claude sessions. So the
host memory is bind-mounted read-only as a seed; `post-start` mirrors it into the
writable `.claude` volume (the agent reads current memory and writes new memories
locally); and the launcher (`bin/agent`) pushes container memory back to the host
**only for interactive operator sessions** (`brain-claude`/`brain-shell`, the same
`BRAIN_AUTOSYNC` gate), never for headless `brain-wiki` runs. Bidirectional when
you drive it; closed against the injection path otherwise.

### qmd index + models (search-only, embed on the host)

The container is **search-only**: the host does all embedding/indexing, and the
container reads ready-made vectors. The host `~/.cache/qmd` is bind-mounted RO at
`~/.qmd-seed`, and **`post-start` wires qmd up on every start**:

- **Models** are symlinked to the live RO seed (`~/.cache/qmd/models` →
  `~/.qmd-seed/models`). Immutable GGUFs, no copy, and a host `qmd pull` shows up
  immediately. Nothing is fetched from HuggingFace (which is blocked anyway).
- **The index** is snapshot-**copied** into the writable cache volume when the
  host's `index.sqlite` is newer than the container's (a few-second file copy,
  **not** a re-embed). qmd opens the DB read-write and writes WAL + `llm_cache`
  even on a search, so it can't run off the RO seed directly; copying also avoids
  two writers on one SQLite file across the VM boundary (the long-lived `qmd mcp`
  server holds the DB open all session). A host re-embed therefore propagates on
  the **next container start**.
- Collections keep the host's absolute paths, which is fine: qmd search and
  `get` serve document bodies from the DB `content` table (keyed by content hash),
  not from the filesystem, so they work unchanged in the container. The container
  never re-points collections or runs `qmd update` (indexing is host-only).

**Why not a live RW share of the index?** qmd has no read-only mode
(`openDatabase` opens RW and forces `PRAGMA journal_mode=WAL`), so a RO bind
can't work; a RW bind risks SQLite corruption (concurrent host + container
writers over the VirtioFS mount) and would expose the real host index to the
sandbox. The snapshot-on-start keeps a single writer (the host) and isolates the
container's writes. To pick up fresh host embeddings, just restart the container.

**The embed model must match the host.** `containerEnv` sets `QMD_EMBED_MODEL` to
the same value as the host (`config.fish`: `hf:Qwen/Qwen3-Embedding-0.6B…`), so
the seeded model and the 1024-dim index line up. If it diverged, the container
would default to a model that isn't in the cache and try to fetch it from
HuggingFace (blocked → fail).

**Embed on the host (Metal GPU).** Run `qmd embed` on the host, then restart the
container so `post-start` snapshots the fresh index. A host `qmd status` showing
"N need embedding" / a `qmd doctor` "legacy/stale fingerprint" means the stored
vectors predate the current model/pipeline; fix it on the host first.

## Git (read-only inside; commit & push on the host)

No container — not even `master` — can change git history. The root `.git` is
mounted **read-only**, no git credential (`GH_TOKEN`/`GITHUB_TOKEN`) is forwarded,
and the host ssh-agent is **not** forwarded, so a compromised agent can't commit,
rewrite history, push, or sign/authenticate as you over SSH.

| Operation | Works? | Notes |
| --- | --- | --- |
| `git status` / `diff` / `log` / `show` | ✅ | read-only on the bind-mounted vault (`safe.directory` set) |
| `git commit` / `rebase` / `reset` / `amend` | ❌ | root `.git` is read-only — EROFS, by design |
| `git push` / `gh pr` | ❌ | no credential in the container; `git push` errors with "could not read Username" |
| commit signing (ssh-agent) | ❌ (n/a) | no ssh-agent forwarded — commits happen on the host |

The one exception is `projects/thesis`, which is its own nested repo: a
`project:thesis` agent's RW hole covers `projects/thesis/.git`, so it **can**
commit to the thesis repo. **Make changes inside, commit & push from the host.**

## Known limitations

- **iCloud vault mount.** The vault lives in iCloud Drive. Files evicted to the
  cloud ("dataless") can read as empty through the bind mount — materialize the
  files you'll work on before running, and expect sync churn as agents write
  wiki pages. Bind-mount performance is also slower than a local disk.
- **Ollama hole.** The container can reach exactly one host service
  (`:11434`). This is a deliberate isolation tradeoff; drop the rule in
  `init-firewall.sh` (and rebuild) if you'd rather run Ollama in-container.
- **Changing the egress allowlist** requires editing `allowlist.txt` and
  rebuilding (it's baked into the image).

## Safety note

The container runs as non-root (`dev`) with `no-new-privileges` and no sudo, so
the CLIs accept `--dangerously-skip-permissions`. Anthropic still warns: a
malicious source document or repo can exfiltrate anything **inside** the
container, including the credential volumes. Treat this as *"host is isolated
from the agents,"* not *"the agents are isolated from a hostile input."* Only
ingest sources you trust.

**Why `.devcontainer` is mounted read-only.** The vault is bind-mounted
read-write at `/workspaces/Brain` so the agents can edit wiki content — but that
same mount would otherwise expose the sandbox's own definition (`compose.yaml`,
`Dockerfile`) and, critically, the **host-side launcher**
(`bin/agent`, `bin/claude`, `bin/doctor`). Those run on your **Mac** with your
shell and Keychain. A compromised in-container agent could add a privileged
option or a `docker.sock` mount to `compose.yaml`, or just edit `bin/agent`, and
the next time you ran `brain-*` (which calls `docker compose up` and re-execs the
launcher) it would execute on the host — a trivial full escape. To close that,
`.devcontainer` is re-mounted **read-only on top of** the read-write workspace,
so it is immutable from inside. The container cannot lift this: it has
`cap-drop=ALL` (no `CAP_SYS_ADMIN`, so no remount/unmount), `no-new-privileges`,
and `.devcontainer` is a busy mountpoint that can't be replaced — the protection
re-applies on every `docker compose up`. **Edit `.devcontainer` on the host only,**
then rebuild.
