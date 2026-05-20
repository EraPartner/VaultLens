#!/usr/bin/env bash
# Runs every time the container starts, as the `dev` user, AFTER the root
# ENTRYPOINT has already done perms repair + started the egress proxy + applied
# the firewall. This hook only refreshes the Claude config from the host stage
# and does the ssh-agent signing sanity check. No sudo (no-new-privileges).

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
  if jq -s '.[1] * .[0]' /home/dev/.claude.json "$STAGE/claude.json" > "$tmp" 2>/dev/null \
     && [[ -s "$tmp" ]]; then
    mv "$tmp" /home/dev/.claude.json
  else
    rm -f "$tmp"
    echo "[post-start] WARN: jq merge of ~/.claude.json failed — container kept stale state." >&2
  fi
fi

# Prune host-origin background-task state that isn't meaningful in the container
# (the ~/.claude volume persists and rsync --update never deletes, so a stale
# volume could still hold these from an older seed).
for p in scheduled-tasks tasks jobs daemon; do
  rm -rf "/home/dev/.claude/$p" 2>/dev/null || true
done

# Sanity-check: is the signing public key actually loaded in the host ssh-agent
# we just forwarded? If not, `git commit -S` will fail with "No private key
# found for public key …" — emit a clear hint instead.
SIGNING_PUB=/home/dev/.ssh/host-signing.pub
if [[ -r "$SIGNING_PUB" ]] && command -v ssh-keygen >/dev/null && command -v ssh-add >/dev/null; then
  want_fp="$(ssh-keygen -lf "$SIGNING_PUB" 2>/dev/null | awk '{print $2}')"
  agent_fps="$(SSH_AUTH_SOCK=/ssh-agent ssh-add -l 2>/dev/null | awk '{print $2}')"
  if [[ -n "$want_fp" ]] && ! grep -qF "$want_fp" <<<"$agent_fps"; then
    cat >&2 <<EOF
[post-start] ⚠  Signing key not loaded in the forwarded host ssh-agent.
  want:  $want_fp  ($(awk '{print $3}' "$SIGNING_PUB"))
  agent: $(SSH_AUTH_SOCK=/ssh-agent ssh-add -l 2>/dev/null | sed 's/^/    /' || echo "    (none)")
  On the host, run e.g.:  ssh-add ~/.ssh/github
  Then signed commits inside the container will work.
EOF
  fi
fi

# Refuse to proceed if the egress firewall didn't verify. The entrypoint writes
# this sentinel only after confirming default-deny is active; its absence means
# the lock may be open. Egress is fail-closed regardless (init-firewall.sh sets
# DROP first), but surface it loudly and fail the lifecycle so it's not missed.
if [[ ! -f /run/brain-firewall-ok ]]; then
  cat >&2 <<'EOF'
[post-start] ✖✖ EGRESS FIREWALL NOT VERIFIED (/run/brain-firewall-ok missing).
[post-start]     The egress lock did not confirm. Check `docker logs` for the
[post-start]     [firewall] error, then restart the container. Do NOT run
[post-start]     --dangerously-skip-permissions until this is resolved.
EOF
  exit 1
fi

echo "[post-start] Ready. Run agents with brain-wiki / brain-claude / brain-copilot / brain-opencode."
