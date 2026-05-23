#!/usr/bin/env bash
# Runs once when the devcontainer is first created, as the `dev` user.
# Installs the agent CLIs, seeds qmd's cache from the host, and prepares git +
# copilot for headless runs.
#
# NOTE: all privileged setup (perms repair, egress proxy, firewall) happens in
# the root ENTRYPOINT (/usr/local/sbin/brain-entrypoint) BEFORE this runs. The
# container has no-new-privileges, so there is no sudo here.

set -euo pipefail
cd /workspaces/Brain

# Wait for the egress proxy (started by the root entrypoint) before any network
# install — postCreate can race the entrypoint's proxy startup, and with egress
# locked to the proxy UID, installs fail until 127.0.0.1:3128 is listening.
echo "[post-create] Waiting for egress proxy on 127.0.0.1:3128..."
for _ in $(seq 1 30); do
  (exec 3<>/dev/tcp/127.0.0.1/3128) 2>/dev/null && break
  sleep 1
done

# --- Supply-chain protection: Aikido safe-chain ------------------------------
# `safe-chain setup` writes shell wrappers; BASH_ENV (devcontainer.json) sources
# them into every bash session so npm/pip installs the agents run mid-session
# are screened against malware-list.aikido.dev before running.
echo "[post-create] Installing safe-chain (supply-chain protection)..."
sc_ok=0
for attempt in 1 2 3; do
  if npm install -g @aikidosec/safe-chain >/dev/null 2>&1 && command -v safe-chain >/dev/null 2>&1; then
    sc_ok=1; break
  fi
  echo "[post-create] safe-chain install attempt $attempt failed; retrying..." >&2
  sleep $(( attempt * 2 ))
done
if (( sc_ok )); then
  safe-chain setup >/dev/null 2>&1 || true
  echo "[post-create] safe-chain installed (screens npm/pip in later sessions)."
else
  echo "[post-create] ⚠ WARN: safe-chain install FAILED after retries — package installs are NOT" >&2
  echo "[post-create]   supply-chain screened. \`.devcontainer/bin/doctor\` will flag this." >&2
fi

# --- Agent CLIs: installed at BUILD time by the agent-clis feature -----------
# copilot, opencode, and qmd are baked into the image by
# .devcontainer/features/agent-clis (the build phase has free network + a
# toolchain). They are NOT installed here: post-create runs after egress is
# locked, where qmd's native modules (better-sqlite3 / tree-sitter /
# node-llama-cpp) can't fetch node headers or compile. Verify they're present.
for c in qmd copilot opencode; do
  command -v "$c" >/dev/null 2>&1 \
    || echo "[post-create] ⚠ WARN: '$c' missing — rebuild the container so the agent-clis feature runs. See \`.devcontainer/bin/doctor\`." >&2
done

# --- qmd: index + models are wired up by post-start (runs on every start) -----
# Option B (search-only, embed-on-host): the index is snapshot-copied from the
# host's live cache and the models are symlinked read-only to it. That logic
# lives in post-start.sh so a host re-embed propagates on the next start, not
# just at first create. Nothing qmd-related is seeded here.

# --- copilot: seed the headless permission allowlist + qmd MCP ---------------
# copilot ignores --allow-tool in headless (-p) mode (upstream bug), so the wiki
# agents rely on a pre-seeded ~/.copilot/permissions-config.json keyed by the
# repo path. This script registers the read/write shell command set.
if [[ -x tools/scripts/setup-copilot-perms.sh ]]; then
  echo "[post-create] Seeding copilot permissions for /workspaces/Brain..."
  tools/scripts/setup-copilot-perms.sh || echo "[post-create]   copilot perms seed failed (non-fatal)." >&2
fi
# Register qmd as an MCP server for copilot (search/enhance agents use it).
if [[ ! -f /home/dev/.copilot/mcp-config.json ]]; then
  mkdir -p /home/dev/.copilot
  cat > /home/dev/.copilot/mcp-config.json <<'JSON'
{
  "mcpServers": {
    "qmd": {
      "type": "stdio",
      "command": "qmd",
      "args": ["mcp"]
    }
  }
}
JSON
fi

# --- git: minimal ~/.gitconfig — read-only git only --------------------------
# Just marks /workspaces/Brain a safe.directory so read-only git ops
# (log/diff/status) work despite the bind mount's non-dev ownership. No identity,
# signing, or push config: commits & pushes happen on the HOST, and the
# in-container .git is read-only.
if [[ ! -f /home/dev/.gitconfig || ! -s /home/dev/.gitconfig ]]; then
  cat > /home/dev/.gitconfig <<'EOF'
[safe]
    directory = /workspaces/Brain
EOF
fi

# --- Seed ~/.claude from the SANITIZED stage the host wrapper produced --------
# The host wrapper strips secrets (.credentials.json) before staging into
# ~/.claude-sandbox/stage/brain (bind RO at /home/dev/.claude-stage).
STAGE=/home/dev/.claude-stage
if [[ ! -f /home/dev/.claude/settings.json && -d "$STAGE/dot-claude" ]]; then
  echo "[post-create] Seeding ~/.claude from sanitized stage..."
  if ! rsync -a --ignore-errors "$STAGE/dot-claude/" /home/dev/.claude/; then
    echo "[post-create] WARN: ~/.claude rsync seed had errors (some files may be missing)." >&2
  fi
  echo "[post-create] Seeded $(find /home/dev/.claude -mindepth 1 -maxdepth 1 | wc -l) entries into ~/.claude."
fi
if [[ ! -f /home/dev/.claude.json && -f "$STAGE/claude.json" ]]; then
  cp "$STAGE/claude.json" /home/dev/.claude.json
  chmod 0600 /home/dev/.claude.json
fi

# gh authenticates via GH_TOKEN forwarded from the host Keychain by the wrapper
# (no persistent token volume). The same token authenticates copilot.
if ! gh auth status >/dev/null 2>&1; then
  cat <<'NOTE'
[post-create] gh is not authenticated. The wrapper forwards GH_TOKEN from your
              host Keychain entry `brain-gh-token` if present. To set it up,
              on the HOST run once:
                gh auth token | security add-generic-password \
                  -s brain-gh-token -a "$USER" -w
              (or paste a fine-grained PAT with the "Copilot Requests"
              permission — that token also authenticates the copilot CLI).
              Then re-run `brain-wiki` / `brain-claude`.
NOTE
fi

echo "[post-create] Done."
echo "[post-create] Run the agents with, e.g.:"
echo "[post-create]   python3 tools/agents/wiki-agent.py enhance --strategy coverage"
echo "[post-create]   claude --dangerously-skip-permissions"
