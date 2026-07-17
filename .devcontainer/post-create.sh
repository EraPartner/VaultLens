#!/usr/bin/env bash
# Runs once when the devcontainer is first created, as the `dev` user.
# Installs JS deps, seeds the Claude config, and prepares git for signed commits.
#
# NOTE: all privileged setup (perms repair, egress proxy, firewall) happens in
# the root ENTRYPOINT (/usr/local/sbin/vaultlens-entrypoint) BEFORE this runs.
# The container has no-new-privileges, so there is no sudo here.

set -euo pipefail
cd /workspaces/VaultLens

# Wait for the egress proxy (started by the root entrypoint) before any network
# install — postCreate can race the entrypoint's proxy startup, and with egress
# locked to the proxy UID, installs fail until 127.0.0.1:3128 is listening.
echo "[post-create] Waiting for egress proxy on 127.0.0.1:3128..."
for _ in $(seq 1 30); do
  (exec 3<>/dev/tcp/127.0.0.1/3128) 2>/dev/null && break
  sleep 1
done

# Supply-chain protection: install Aikido safe-chain so package installs are
# screened against its malware list (malware-list.aikido.dev, allowlisted in
# squid). `safe-chain setup` writes shell wrappers; BASH_ENV (set at container run)
# sources them into every bash session so npm/bun/pip installs Claude runs
# mid-session are screened. The project's own pinned deps below are installed
# plain (already vetted via the lockfile) to avoid first-boot fragility.
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
  echo "[post-create] safe-chain installed (screens npm/bun/pip in later sessions)."
else
  echo "[post-create] ⚠ WARN: safe-chain install FAILED after retries — package installs are NOT" >&2
  echo "[post-create]   supply-chain screened. \`.devcontainer/bin/doctor\` will flag this." >&2
fi

# No JS dependency install: VaultLens is Python tooling (tools/*.py) with no root
# package.json / lockfile, so there is nothing to `npm ci`. The toolchain it needs
# is baked into the image — python3, ruff (its linter), qmd (its search engine, an
# MCP server per .mcp.json), poppler-utils/qpdf (wiki.py PDF ingest), and node for
# .claude/hooks/guard.mjs. safe-chain above still screens ad-hoc npm/pip installs.

# Minimal ~/.gitconfig: just mark the bind-mounted workspace safe so read-only
# git ops (log/diff/status) work despite the mount's non-dev ownership. No
# identity/signing/push config — commits & pushes happen on the HOST, and the
# in-container .git is read-only.
if [[ ! -f /home/dev/.gitconfig || ! -s /home/dev/.gitconfig ]]; then
  cat > /home/dev/.gitconfig <<'EOF'
[safe]
    directory = /workspaces/VaultLens
EOF
fi

# Seed the container's ~/.claude + ~/.claude.json from the SANITIZED staging
# dir the host wrapper produced at /home/dev/.claude-stage (bind RO):
#   /home/dev/.claude-stage/dot-claude/   sanitized copy of host ~/.claude
#   /home/dev/.claude-stage/claude.json   sanitized copy of host ~/.claude.json
# The host staging (launcher-common.sh) copies only a curated item allowlist (so
# .credentials.json never enters), strips .hooks from settings.json and
# .oauthAccount/.projects/.installMethod from .claude.json. NOTE: mcpServers and
# enabledPlugins ARE propagated by design (user-enabled servers/plugins — see the
# post-start KEEP note); the PreToolUse guard reaches the box out-of-band via the
# root-owned managed-settings.json bind, not through this staged config.
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


echo "[post-create] Done."
echo "[post-create] Tooling:  python3 tools/wiki.py lint  ·  ruff check tools/  ·  qmd search '<q>'"
echo "[post-create] Start Claude with:  claude --dangerously-skip-permissions"
