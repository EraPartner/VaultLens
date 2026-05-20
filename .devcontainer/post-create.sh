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

# --- Agent CLIs (npm globals; honor HTTPS_PROXY → squid → registry.npmjs.org) -
# Pin versions here if you want reproducible installs (e.g. opencode-ai@x.y.z).
echo "[post-create] Installing agent CLIs (copilot, opencode, qmd)..."
npm install -g @github/copilot opencode-ai @tobilu/qmd \
  || echo "[post-create] ⚠ WARN: one or more agent CLIs failed to install — see \`.devcontainer/bin/doctor\`." >&2

# --- qmd: seed cache from the host, then register collections ----------------
# The host ~/.cache/qmd is bind-mounted read-only at ~/.qmd-seed. Copy the
# embedding models (so nothing is downloaded from HuggingFace) and the existing
# index as a head start (you chose seed-both). The cache itself is a named
# volume, so this only runs on first create.
SEED=/home/dev/.qmd-seed
QMD_CACHE=/home/dev/.cache/qmd
if [[ -d "$SEED" && ! -e "$QMD_CACHE/index.sqlite" ]]; then
  echo "[post-create] Seeding qmd cache (models + index) from host..."
  mkdir -p "$QMD_CACHE"
  cp -an "$SEED/models" "$QMD_CACHE/" 2>/dev/null || true
  cp -an "$SEED"/index.sqlite* "$QMD_CACHE/" 2>/dev/null || true
  echo "[post-create] Seeded $(du -sh "$QMD_CACHE" 2>/dev/null | cut -f1) into ~/.cache/qmd."
fi

if command -v qmd >/dev/null 2>&1; then
  echo "[post-create] Registering qmd collections for the container paths..."
  qmd collection add wiki/ --name wiki 2>/dev/null || echo "[post-create]   wiki collection already present."
  qmd collection add raw/  --name raw  2>/dev/null || echo "[post-create]   raw collection already present."
  # Reconcile the seeded index with the mounted vault (incremental; reuses the
  # seeded embeddings for unchanged docs, so no full re-embed/download).
  qmd update 2>/dev/null || echo "[post-create]   qmd update skipped (run \`qmd update\` manually if search is stale)."
fi

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

# --- git: writable ~/.gitconfig that includes the host config ----------------
# - includes the read-only bind-mounted host gitconfig (user.name, user.email,
#   commit.gpgsign, gpg.format=ssh, etc. carry over without hardcoding)
# - marks /workspaces/Brain a safe.directory (the bind mount has non-dev
#   ownership, which git would otherwise reject)
# - overrides user.signingkey to the in-container public-key path
# - rewrites the SSH remote (git@github.com:) to HTTPS so `git push` uses the
#   forwarded GH_TOKEN — the SSH transport is blocked (only the ssh-agent socket
#   is forwarded, for commit SIGNING, not git transport).
if [[ ! -f /home/dev/.gitconfig || ! -s /home/dev/.gitconfig ]]; then
  cat > /home/dev/.gitconfig <<'EOF'
[include]
    path = /home/dev/.gitconfig-host
[safe]
    directory = /workspaces/Brain
[user]
    signingkey = /home/dev/.ssh/host-signing.pub
[url "https://github.com/"]
    insteadOf = git@github.com:
EOF
fi

# --- Seed ~/.claude from the SANITIZED stage the host wrapper produced --------
# The host wrapper strips secrets (.credentials.json) before staging into
# ~/.claude-brain-stage (bind RO at /home/dev/.claude-stage).
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
