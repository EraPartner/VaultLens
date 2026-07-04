#!/usr/bin/env bash
# Shared host-side helpers for the sandbox launchers (Vision bin/claude,
# Watchman bin/claude, Brain bin/agent, git-agent bin/git-agent, sandbox
# bin/dev). Vendored into each .devcontainer/ by LockBox/sync.sh and
# sourced by the launcher as "$(dirname "$0")/../launcher-common.sh".
#
# WHY: stage_claude_config(), the Keychain credential block, and the
# autosync-on-exit trap were copy-pasted near-verbatim across all launchers and
# had begun to drift. This is the single source; per-launcher specifics
# (git-agent's push token + ssh signing, the charter) stay in the individual
# launchers and run alongside these.
#
# Must stay POSIX-bash-3.2 compatible (macOS /bin/bash). No associative arrays,
# no ${var^^}. Functions use a shared global EXEC_ENV array by design.

# --- Stage a sanitized ~/.claude into the RO bind-mount staging dir -----------
# Usage: sandbox_stage_claude_config <profile>
#   Produces  $HOME/.claude-sandbox/stage/<profile>/dot-claude/   (config tree)
#   and       $HOME/.claude-sandbox/stage/<profile>/claude.json   (.claude.json)
# Copies only the safe, declarative config; rewrites host plugin paths to the
# container path; strips hooks from the staged settings (the PreToolUse guard
# reaches the box out-of-band via the root-owned managed-settings.json bind).
sandbox_stage_claude_config() {
  local profile="$1"
  local src dst item jf f
  src="$(cd "$HOME/.claude" 2>/dev/null && pwd -P || true)"
  dst="$HOME/.claude-sandbox/stage/$profile"
  rm -rf "$dst/dot-claude"; mkdir -p "$dst/dot-claude"
  if [[ -n "$src" && -d "$src" ]]; then
    for item in settings.json keybindings.json CLAUDE.md agents rules commands skills statusline status-line.sh plugins; do
      [[ -e "$src/$item" ]] && cp -a "$src/$item" "$dst/dot-claude/" 2>/dev/null || true
    done
    find "$dst/dot-claude/statusline" "$dst/dot-claude/plugins" -name .git -type d -prune -exec rm -rf {} + 2>/dev/null || true
    for jf in known_marketplaces.json installed_plugins.json; do
      f="$dst/dot-claude/plugins/$jf"
      [[ -f "$f" ]] && sed -i '' -e "s#$HOME/.claude#/home/dev/.claude#g" -e "s#$src#/home/dev/.claude#g" "$f" 2>/dev/null || true
    done
    find "$dst/dot-claude" -name '.DS_Store' -delete 2>/dev/null || true
    if [[ -f "$dst/dot-claude/settings.json" ]] && command -v jq >/dev/null 2>&1; then
      local hjt; hjt="$(mktemp)"
      jq 'del(.hooks)' "$dst/dot-claude/settings.json" >"$hjt" 2>/dev/null && mv "$hjt" "$dst/dot-claude/settings.json" || rm -f "$hjt"
    fi
  fi
  # Stage .claude.json, stripping the blocks the container must never see:
  #   .oauthAccount — host identity (account email, account/org UUIDs, display
  #                   name, plan tier).
  #   .projects     — a full map of every host project PATH plus that project's
  #                   prior prompt/history text and per-project tool grants. The
  #                   container has no use for the host's project list, and a
  #                   hostile in-container source doc could otherwise read it and
  #                   exfiltrate the host filesystem layout + past prompts over an
  #                   allowlisted host. Claude Code recreates its own .projects
  #                   entry for /workspaces/... in-container as needed.
  #   .installMethod / .autoUpdatesProtectedForNative — the HOST claude is a NATIVE
  #                   install (~/.local/bin/claude); the in-container claude is an
  #                   npm-global install (/usr/local/share/npm-global/bin/claude).
  #                   Leaking installMethod=native makes the container's claude
  #                   check ~/.local/bin/claude and warn "missing or broken" on
  #                   every start (the dir doesn't exist). Stripped so the container
  #                   claude detects its own (npm) install. autoUpdates=false is KEPT
  #                   (we never auto-update a pin-verified sandbox).
  # Mirrors the .hooks strip above. Falls back to a plain copy if jq is absent.
  if [[ -f "$HOME/.claude.json" ]]; then
    if command -v jq >/dev/null 2>&1; then
      jq 'del(.oauthAccount, .projects, .installMethod, .autoUpdatesProtectedForNative)' "$HOME/.claude.json" >"$dst/claude.json" 2>/dev/null \
        || cp "$HOME/.claude.json" "$dst/claude.json" 2>/dev/null || true
    else
      cp "$HOME/.claude.json" "$dst/claude.json" 2>/dev/null || true
    fi
  fi
}

# --- Forward the Claude LLM token into a shared EXEC_ENV array ----------------
# Usage: sandbox_forward_llm_creds <keychain-service>
# Appends NAME-ONLY `-e KEY` flags to the caller's EXEC_ENV array (declare it
# first) and exports the value into the launcher's own environment. `container
# exec -e KEY` (no =VALUE) forwards the value from the launcher's env, so
# the secret never appears in the exec argv — i.e. it is not visible via `ps`/
# /proc/<pid>/cmdline the way `-e KEY=VALUE` would be. Prefers the named macOS
# Keychain item; falls back to the host env vars.
sandbox_forward_llm_creds() {
  local kc_service="$1" tok var a have_claude=0
  if command -v security >/dev/null 2>&1; then
    tok="$(security find-generic-password -s "$kc_service" -w 2>/dev/null || true)"
    [[ -n "$tok" ]] && { export CLAUDE_CODE_OAUTH_TOKEN="$tok"; EXEC_ENV+=(-e CLAUDE_CODE_OAUTH_TOKEN); }
  fi
  for a in ${EXEC_ENV[@]+"${EXEC_ENV[@]}"}; do [[ "$a" == "CLAUDE_CODE_OAUTH_TOKEN" ]] && have_claude=1; done
  if (( ! have_claude )); then
    for var in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN; do
      [[ -n "${!var:-}" ]] && { export "${var?}"; EXEC_ENV+=(-e "$var"); }
    done
  fi
  # Explicit success: the final `[[ -n ... ]] &&` above can leave $? = 1 (e.g. when
  # no keychain token exists and the last env var is unset), which would otherwise
  # abort a `set -e` launcher that calls this as a bare statement.
  return 0
}

# --- Install an exit trap: push config back, then optionally power down --------
# Usage: sandbox_install_autosync_trap <profile> <container-id> <autosync:0|1> [stop_on_exit:0|1]
# On shell exit the trap runs, at most once:
#   1) if <autosync>=1, `<profile>-claude-sync push <cid>` via fish (idempotent;
#      no-op if fish is absent) — pushes in-container config back to the host.
#   2) if [stop_on_exit]=1, `container stop <cid>` — powers the sandbox VM down.
# WHY stop_on_exit: the launchers start the box detached (`container run -d`) with a
# keep-alive PID 1, so it NEVER stops itself; left running it pins its whole `-m`
# allocation (4–6 GB) as a live VM until the next reboot. It defaults to 0 so an
# INTERACTIVE session keeps the container WARM for instant reuse; headless/scheduled
# launchers pass 1 to reclaim that RAM when the run ends. The push (step 1) runs
# BEFORE the stop (step 2) because the sync needs the container still up.
sandbox_install_autosync_trap() {
  local profile="$1" cid="$2" enabled="$3" stop_on_exit="${4:-0}"
  _SANDBOX_AUTOSYNC_PROFILE="$profile"
  _SANDBOX_AUTOSYNC_CID="$cid"
  _SANDBOX_AUTOSYNC_ENABLED="$enabled"
  _SANDBOX_STOP_ON_EXIT="$stop_on_exit"
  _SANDBOX_AUTOSYNC_DONE=0
  trap _sandbox_autosync_push EXIT
}
_sandbox_autosync_push() {
  [[ "${_SANDBOX_AUTOSYNC_DONE:-0}" == 1 ]] && return 0
  _SANDBOX_AUTOSYNC_DONE=1
  # 1) Config-sync push (opt-in; needs the container still running).
  if [[ "${_SANDBOX_AUTOSYNC_ENABLED:-0}" == 1 && -n "${_SANDBOX_AUTOSYNC_CID:-}" ]] \
     && command -v fish >/dev/null 2>&1; then
    fish -c "${_SANDBOX_AUTOSYNC_PROFILE}-claude-sync push ${_SANDBOX_AUTOSYNC_CID}" \
      || echo "launcher: auto config-sync push failed — run '${_SANDBOX_AUTOSYNC_PROFILE}-claude-sync push' to retry." >&2
  fi
  # 2) Power down the detached VM (opt-in) so it stops pinning RAM. PID 1 traps
  # SIGTERM for a graceful shutdown; `container stop` is a no-op if already stopped.
  if [[ "${_SANDBOX_STOP_ON_EXIT:-0}" == 1 && -n "${_SANDBOX_AUTOSYNC_CID:-}" ]] \
     && command -v container >/dev/null 2>&1; then
    container stop "${_SANDBOX_AUTOSYNC_CID}" >/dev/null 2>&1 \
      || echo "launcher: 'container stop ${_SANDBOX_AUTOSYNC_CID}' failed — stop it manually to free RAM." >&2
  fi
}

# --- Warn if a reused/started container's BAKED egress allowlist is stale ------
# Usage: sandbox_warn_stale_allowlist <container> <canonical-allowlist-file> <rebuild-hint>
# For the IMAGE-baked launchers the allowlist is COPY'd into the image, so the
# reuse-running / start-stopped paths keep the OLD baked copy: a sync.sh allowlist
# change (e.g. a NARROWING) silently does not take effect until a rebuild. Compare
# the running container's /etc/squid/allowlist.txt against the freshly-vendored
# canonical file and warn. Never auto-recreates — a rebuild is the operator's call
# (the generic dev-sandbox bind-mounts its allowlist and uses `squid -k
# reconfigure` instead; this is only for the baked-image launchers). (F14)
sandbox_warn_stale_allowlist() {
  local cname="$1" canonical="$2" hint="$3" tmp
  [[ -f "$canonical" ]] || return 0
  tmp="$(mktemp)" || return 0
  if container exec "$cname" cat /etc/squid/allowlist.txt >"$tmp" 2>/dev/null && ! cmp -s "$tmp" "$canonical"; then
    echo "launcher: WARN — the running container's BAKED egress allowlist differs from the" >&2
    echo "  current $canonical. The baked copy is what squid enforces; rebuild to apply it:" >&2
    echo "  $hint" >&2
  fi
  rm -f "$tmp"
}

# --- Lock host-executed git paths READ-ONLY (.git + relocated hooks) ----------
# Usage: sandbox_git_ro_mounts <host_repo_root> <container_workspace_path>
# Populates the global GIT_RO_MOUNTS array with `-v …:ro` flags the caller splices
# into its `container run`. Declare-safe expand at the call site:
#   sandbox_git_ro_mounts "$PROJECT_ROOT" /workspaces/Foo
#   ... ${GIT_RO_MOUNTS[@]+"${GIT_RO_MOUNTS[@]}"} ...
#
# WHY: git hooks run on the HOST. The documented workflow is commit/push on your
# Mac (the box carries no push credential and mounts .git RO), so any hook script
# an in-container agent can write into the RW workspace would later execute on the
# host, as you, with Keychain + ssh-agent + gh token in reach — a VM→host escape
# that bypasses the hypervisor and fires on a routine `git commit`. This closes it:
#
#   1) .git RO           — blocks history rewrite AND planting a .git/hooks/* script.
#   2) core.hooksPath RO — `core.hooksPath` RELOCATES hooks OUTSIDE .git (Vision,
#      Watchman, Brain and this repo all set it to `.githooks`), so the RO .git
#      mount does NOT cover them; they inherit the writable workspace. Lock the
#      effective hooks dir too. An absolute hooksPath (outside the workspace) or one
#      already under .git needs no extra mount. If it is SET but the dir is ABSENT,
#      an agent could create it in the RW workspace and plant a hook — overlay an
#      empty RO dir so that path can't be written. (Cost, verified against
#      apple/container: mounting a nonexistent subpath of the RW workspace makes the
#      runtime create the mountpoint, leaving a spurious EMPTY `<hooksPath>/` in the
#      host workspace — cosmetic, and only in this misconfigured edge case; real
#      hooks dirs mount cleanly.) Relative values resolve against the repo root
#      (where git runs the hooks from); `..` is rejected.
#
# Generalizes git-agent's empty-hooks overlay for the RW-workspace launchers.
# bash-3.2: indexed array, no `local -n`; `git` may be absent (falls back to .git).
sandbox_git_ro_mounts() {
  local repo="$1" ws="$2" hp abs src empty
  GIT_RO_MOUNTS=()
  [[ -n "$repo" && -e "$repo/.git" ]] || return 0
  GIT_RO_MOUNTS+=(-v "$repo/.git:$ws/.git:ro")

  hp="$(git -C "$repo" config core.hooksPath 2>/dev/null || true)"
  [[ -n "$hp" ]] || return 0
  case "$hp" in
    /*)          return 0 ;;  # absolute → outside the workspace mount, not ours
    .git|.git/*) return 0 ;;  # already under the RO .git mount above
    *..*)        return 0 ;;  # reject path traversal (defense in depth)
  esac
  abs="$repo/$hp"
  if [[ -d "$abs" ]]; then
    src="$abs"                # real versioned hooks: RO blocks host-side tampering
  else
    empty="$HOME/.claude-sandbox/empty-hooks"  # absent dir: block creating one
    mkdir -p "$empty" 2>/dev/null || return 0
    src="$empty"
  fi
  GIT_RO_MOUNTS+=(-v "$src:$ws/${hp}:ro")
}
