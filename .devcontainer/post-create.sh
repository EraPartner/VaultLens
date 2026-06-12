#!/usr/bin/env bash
# Runs once when the devcontainer is first created, as the `dev` user.
# Installs the agent tooling, seeds qmd's cache from the host, and prepares
# git for headless runs.
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
# `safe-chain setup` writes shell wrappers; BASH_ENV (set by bin/agent at
# `container run`) sources them into every bash session so npm/pip installs the
# agents run mid-session are screened against malware-list.aikido.dev before running.
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

# --- Agent CLIs: installed at BUILD time by the Dockerfile -------------------
# claude and qmd are baked into the image by the Dockerfile (npm install during
# the build phase, which has free network + a toolchain). They are NOT installed
# here: post-create runs after egress is locked, where qmd's native modules
# (better-sqlite3 / tree-sitter / node-llama-cpp) can't fetch node headers
# or compile. Verify they're present.
for c in qmd; do
  command -v "$c" >/dev/null 2>&1 \
    || echo "[post-create] ⚠ WARN: '$c' missing — rebuild the image so the Dockerfile bakes it. See \`.devcontainer/bin/doctor\`." >&2
done

# --- qmd: index + models are wired up by post-start (runs on every start) -----
# Option B (search-only, embed-on-host): the index is snapshot-copied from the
# host's live cache and the models are symlinked read-only to it. That logic
# lives in post-start.sh so a host re-embed propagates on the next start, not
# just at first create. Nothing qmd-related is seeded here.

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

# gh is unauthenticated by design: no GitHub credential enters the container
# (commits/pushes happen on the HOST, the in-container .git is read-only).

echo "[post-create] Done."
echo "[post-create] Run the agents with, e.g.:"
echo "[post-create]   python3 tools/agents/wiki-agent.py enhance --strategy coverage"
echo "[post-create]   claude --dangerously-skip-permissions"
