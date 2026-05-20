# Brain (VaultLens) devcontainer

Hardened sandbox for running the wiki-builder agents on this vault. The Python
orchestrator (`tools/wiki.py`, `tools/agents/wiki-agent.py`) and the four agent
CLIs it drives — **claude**, **copilot**, **opencode**, **ollama** — plus the
**qmd** local search engine all run inside the container. The agents run with
`--dangerously-skip-permissions` / headless flags, so the container is the
isolation boundary.

Modelled on the Vision/Watchman devcontainers; the differences are this
project's stack (no web app, no database), the four-CLI agent toolchain, the
qmd cache seeding, and a single egress hole to the host Ollama daemon.

## What's inside

| Component | Where it runs | Notes |
| --- | --- | --- |
| Python 3.12 orchestrator | `tools/wiki.py`, `tools/agents/wiki-agent.py` | stdlib only, no pip deps |
| `claude` CLI | claude-code devcontainer feature | default agent (`sonnet`) |
| `copilot` CLI | npm `@github/copilot` (post-create) | `gpt-5.2`; auth via `GH_TOKEN` |
| `opencode` CLI | npm `opencode-ai` (post-create) | `github-copilot/gpt-5.2` |
| `ollama` CLI | host daemon via `host.docker.internal:11434` | not installed in-container |
| `qmd` search (MCP) | npm `@tobilu/qmd` (post-create) | index + models seeded from host |
| PDF ingest | `pdftotext` (poppler) + `qpdf` (apt) | for `wiki.py preprocess` |
| GitHub CLI (`gh`) | apt | push over HTTPS via `GH_TOKEN` |

Base image: digest-pinned `debian:bookworm-slim`. Container user: `dev` (UID 1000).

## How to use

Prerequisite (one-time): `npm install -g @devcontainers/cli`.

Host fish functions (in `~/.config/fish/functions/`, version-controlled in your
dotfiles). They walk up to find `.devcontainer/`, fall back to `$BRAIN_HOME`,
then the vault path:

| Command | Runs in the container |
| --- | --- |
| `brain-wiki <agent> [args]` | `python3 tools/agents/wiki-agent.py <agent> …` (agent = quality/verify/ingest/contradict/search/enhance) |
| `brain-wiki <subcommand> [args]` | `python3 tools/wiki.py <subcommand> …` (coverage, preprocess, search, log, …) |
| `brain-claude [args]` | `claude …` |
| `brain-copilot [args]` | `copilot …` |
| `brain-opencode [args]` | `opencode …` |
| `brain-shell [cmd]` | a bash shell, or an arbitrary in-container command |
| `brain-claude-sync pull\|push\|status` | sync `~/.claude` between host and container |

Examples:

```fish
brain-wiki enhance --strategy coverage          # agent runner
brain-wiki enhance --background --forever &      # long detached loop (survives via the keep-alive)
brain-wiki coverage --json                       # tools/wiki.py subcommand
brain-claude --dangerously-skip-permissions
brain-shell .devcontainer/bin/doctor             # readiness check
```

## One-time host setup

```sh
# 1) @devcontainers/cli (if not already installed)
npm install -g @devcontainers/cli

# 2) Claude OAuth token (long-lived; uses your subscription)
claude setup-token        # prints sk-ant-… ; copy it
security add-generic-password -s brain-claude-code-token -a "$USER" -w   # paste it

# 3) GitHub token — authenticates BOTH gh and copilot. Must be a fine-grained
#    PAT with the "Copilot Requests" permission, or the gh OAuth token (a
#    classic ghp_ token will NOT work for copilot).
gh auth token | security add-generic-password -s brain-gh-token -a "$USER" -w
#    (or paste a fine-grained PAT instead of `gh auth token`)

# 4) opencode credential (the github-copilot OAuth blob it stores on the host)
security add-generic-password -s brain-opencode-auth -a "$USER" \
  -w "$(cat ~/.local/share/opencode/auth.json)"

# 5) Make sure the signing key is loaded in your host ssh-agent
ssh-add ~/.ssh/github
```

The `brain-*` wrappers retrieve these from the Keychain on every invocation and
forward them at `devcontainer exec` time. The Claude and gh tokens go in as env
vars (`CLAUDE_CODE_OAUTH_TOKEN`, `GH_TOKEN`/`GITHUB_TOKEN`/`COPILOT_GITHUB_TOKEN`);
the opencode blob is written to the container's `~/.local/share/opencode/auth.json`
only if missing (opencode refreshes it in-place in the volume thereafter). No
long-lived credential is ever written to a plaintext file on the host.

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
   `NODE_USE_ENV_PROXY=1`, so `claude`, `copilot`, `opencode`, `qmd`, `npm`,
   `git`, `gh`, and `python` all egress through it.
2. **`iptables` egress lock**: only the `proxy` UID may originate outbound
   packets. IPv6 is default-deny. Denied egress is rate-limited-logged
   (`dmesg | grep brain-deny`).

**Allowlist** (`allowlist.txt`, baked into the image — edit and rebuild to
change): Anthropic + Claude Code, `registry.npmjs.org`, GitHub, GitHub Copilot
(`.githubcopilot.com`, `collector.github.com`, `default.exp-tas.com`),
`opencode.ai`, the safe-chain malware list, and context7.

**The one egress hole:** the firewall allows direct TCP to
`host.docker.internal:11434` so the container reaches the **host Ollama daemon**
(the Mac has the weights + Metal GPU). `OLLAMA_HOST` points there and `NO_PROXY`
keeps it off squid. This is the only host service the container can touch.

**Supply-chain scanning:** `post-create` installs Aikido safe-chain and
`BASH_ENV` wires it into every shell, so `npm`/`pip` installs the agents run
mid-session are screened against `malware-list.aikido.dev`.

**Observability:** the definitive log is `/var/log/squid/access.log`
(`dev`-readable; `TCP_DENIED`/`NONE` = blocked). Run `.devcontainer/bin/doctor`
for a one-shot readiness check (egress lock, toolchain, qmd, ollama, auth).

**Not covered:** WebSearch/WebFetch run agent-side, not through the proxy. ECH
(encrypted SNI) destinations fail closed.

## Persistence

| Source | Container path | Holds |
| --- | --- | --- |
| `brain-claude-<id>` (volume) | `/home/dev/.claude` | Claude config, seeded from the sanitized stage |
| `brain-copilot-<id>` (volume) | `/home/dev/.copilot` | copilot config + repo permission allowlist + qmd MCP |
| `brain-local-<id>` (volume) | `/home/dev/.local` | opencode data + `auth.json` |
| `brain-config-<id>` (volume) | `/home/dev/.config` | qmd / gh config |
| `brain-cache-<id>` (volume) | `/home/dev/.cache` | qmd index + embedding models |
| `~/.claude-brain-stage` (host) | `/home/dev/.claude-stage` | **RO** sanitized stage (no secrets) |
| `~/.cache/qmd` (host) | `/home/dev/.qmd-seed` | **RO** seed source for qmd models + index |

The vault is bind-mounted at `/workspaces/Brain`, so wiki edits appear on the
host (and sync via iCloud) immediately. The Claude config is **not** live-shared
(a raw bind would expose host secrets and corrupt `~/.claude.json` under
concurrent writes): the container gets its own writable copy seeded from the
sanitized stage, refreshed read-only on each start. Use `brain-claude-sync push`
to pull container-side config changes back to the host.

### qmd cache seeding

On first create, `post-create` copies the embedding models **and** the existing
index from the host's `~/.cache/qmd` (bind-mounted RO) into the cache volume, so
nothing is downloaded from HuggingFace and search works immediately. It then
re-registers the `wiki` and `raw` collections at the container paths and runs
`qmd update` to reconcile against the mounted vault. Refresh embeddings later
with `brain-shell qmd embed`.

## Git, GitHub, signed commits

| Operation | Works? | Notes |
| --- | --- | --- |
| `git status` / `diff` / `commit` | ✅ | on the bind-mounted vault |
| `git commit -S` (SSH-signed) | ✅ | private key never enters the container; signing via the forwarded host ssh-agent (unlock it with `ssh-add` first) |
| `git push` / `gh pr` | ✅ | the SSH remote is rewritten to HTTPS in-container so push uses the forwarded `GH_TOKEN`; `github.com` is allowlisted |
| `git push` over SSH transport | ❌ | `~/.ssh` isn't mounted; the agent socket is for signing only. HTTPS is used instead. |

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
- **copilot headless permissions.** `copilot` ignores `--allow-tool` in `-p`
  mode (upstream bug); `post-create` seeds `~/.copilot/permissions-config.json`
  for `/workspaces/Brain` via `tools/scripts/setup-copilot-perms.sh`.

## Safety note

The container runs as non-root (`dev`) with `no-new-privileges` and no sudo, so
the CLIs accept `--dangerously-skip-permissions`. Anthropic still warns: a
malicious source document or repo can exfiltrate anything **inside** the
container, including the credential volumes. Treat this as *"host is isolated
from the agents,"* not *"the agents are isolated from a hostile input."* Only
ingest sources you trust.
