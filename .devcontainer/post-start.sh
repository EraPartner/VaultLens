#!/usr/bin/env bash
# Runs every time the container starts, as the `dev` user, AFTER the root
# ENTRYPOINT has already done perms repair + started the egress proxy + applied
# the firewall. This hook only refreshes the Claude config from the host stage
# and verifies the egress lock. No sudo (no-new-privileges).

set -euo pipefail

STAGE=/home/dev/.claude-stage

# Auto-pull the sanitized host Claude config into the container on every start.
# Reads only from the RO stage; writes to the container's own volume.
if [[ -d "$STAGE/dot-claude" && -d /home/dev/.claude ]]; then
  rsync -a --update --ignore-errors "$STAGE/dot-claude/" /home/dev/.claude/ 2>/dev/null || true
fi
# Merge the staged ~/.claude.json into the container's (container values win on
# key conflict, so host adds new keys without clobbering container edits).
if [[ -f "$STAGE/claude.json" && -f /home/dev/.claude.json ]]; then
  tmp=$(mktemp)
  if jq -s '.[1] * .[0] | del(.installMethod, .autoUpdatesProtectedForNative)' /home/dev/.claude.json "$STAGE/claude.json" > "$tmp" 2>/dev/null \
     && [[ -s "$tmp" ]]; then
    mv "$tmp" /home/dev/.claude.json
  else
    rm -f "$tmp"
    echo "[post-start] WARN: jq merge of ~/.claude.json failed — container kept stale state." >&2
  fi
fi

# Prune only the host-origin state the user did NOT opt into, on every start.
# The ~/.claude *volume* persists across rebuilds and rsync --update never
# deletes, so a stale volume could still hold these from an older seed; removing
# them converges any volume to the intended set. plugins/, hooks, mcpServers,
# statusline/ are intentionally KEPT (the user enabled them — see README
# SECURITY NOTE). Background-task state stays out (not enabled, just noise).
for p in scheduled-tasks tasks jobs daemon; do
  rm -rf "/home/dev/.claude/$p" 2>/dev/null || true
done

# --- Project memory: seed from the host (RO) into the writable volume ----------
# The host copy is bind-mounted RO at ~/.claude-memory-seed. Mirror it (--delete)
# into the real per-project memory path on every start so Claude reads CURRENT host
# memory and anything a prior headless / prompt-injected run wrote is wiped. Only an
# interactive operator session's NEW edits (layered on this clean seed) are pushed
# back to the host by bin/claude on exit. Matches Brain's pattern. (F10/F27)
MEM_SEED=/home/dev/.claude-memory-seed
MEM_DIR=/home/dev/.claude/projects/-workspaces-VaultLens/memory
if [[ -d "$MEM_SEED" ]]; then
  mkdir -p "$MEM_DIR"
  rsync -a --delete --ignore-errors "$MEM_SEED/" "$MEM_DIR/" 2>/dev/null || true
fi

# Refuse to proceed if the egress firewall didn't verify. The entrypoint writes
# this sentinel only after confirming default-deny is active; its absence means
# the lock may be open. Egress is fail-closed regardless (init-firewall.sh sets
# DROP first), but surface it loudly and fail the lifecycle so it's not missed.
if [[ ! -f /run/egress-firewall-ok ]]; then
  cat >&2 <<'EOF'
[post-start] ✖✖ EGRESS FIREWALL NOT VERIFIED (/run/egress-firewall-ok missing).
[post-start]     The egress lock did not confirm. Check `container logs` for the
[post-start]     [firewall] error, then restart the container. Do NOT run
[post-start]     --dangerously-skip-permissions until this is resolved.
EOF
  exit 1
fi

echo "[post-start] Ready. Work on the tooling: python3 tools/wiki.py … · ruff check tools/ · qmd."
