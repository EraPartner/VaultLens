#!/bin/bash
# Copilot CLI permissions seed for this repo.
#
# Why this exists: GitHub Copilot CLI has a known bug where --allow-tool flags
# are silently ignored in non-interactive (-p) mode. wiki-agent.py invokes
# copilot headlessly, so granular --allow-tool=shell(<cmd>:*) flags do nothing
# and the agent stalls trying to prompt for permission it can't ask for.
#
# Workaround: pre-approve commands persistently in
# ~/.copilot/permissions-config.json, keyed by repo path. Copilot honors this
# allowlist in headless mode.
#
# Run once after cloning, or any time the agent shell command set changes.
# Safe to re-run.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Register both the logical path (`pwd`) and the symlink-resolved path
# (`pwd -P`). wiki-agent.py invokes copilot with the resolved path because it
# uses Path.resolve(); ad-hoc `copilot` calls from a shell `cd`'d into the
# logical path use the logical entry. Keeping both entries in sync prevents the
# allowlist from drifting between invocation styles.
REPO_ROOT_LOGICAL="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT_PHYS="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
CONFIG="$HOME/.copilot/permissions-config.json"

# Commands the wiki agents need. Keep in sync with
# READ_ONLY_SHELL_COMMANDS + WRITE_SHELL_COMMANDS in tools/agents/wiki-agent.py.
#
# `set` is included because copilot wraps multi-line shell scripts with
# `set -euo pipefail`, which makes `set` the first command identifier and
# causes the whole script to be denied unless `set` is pre-approved.
COMMANDS=(
  qmd
  python3
  set
  ls find grep cat head tail wc sort uniq cut tr date
  touch mkdir mv cp sed awk
)

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required (brew install jq)." >&2
  exit 1
fi

mkdir -p "$(dirname "$CONFIG")"
if [ ! -f "$CONFIG" ]; then
  echo '{"locations":{}}' > "$CONFIG"
fi

cmds_json=$(printf '%s\n' "${COMMANDS[@]}" | jq -R . | jq -s .)

# De-dupe logical vs physical when they're the same path.
PATHS=("$REPO_ROOT_LOGICAL")
if [ "$REPO_ROOT_PHYS" != "$REPO_ROOT_LOGICAL" ]; then
  PATHS+=("$REPO_ROOT_PHYS")
fi

for path in "${PATHS[@]}"; do
  tmp=$(mktemp)
  jq --arg path "$path" --argjson cmds "$cmds_json" '
    .locations[$path] //= {tool_approvals: []}
    | .locations[$path].tool_approvals //= []
    | (.locations[$path].tool_approvals
        | map(select(.kind == "commands"))) as $existing
    | if ($existing | length) == 0 then
        .locations[$path].tool_approvals += [{kind: "commands", commandIdentifiers: $cmds}]
      else
        .locations[$path].tool_approvals |= map(
          if .kind == "commands"
          then .commandIdentifiers = ((.commandIdentifiers // []) + $cmds | unique)
          else . end
        )
      end
  ' "$CONFIG" > "$tmp" && mv "$tmp" "$CONFIG"
done

echo "Updated $CONFIG"
for path in "${PATHS[@]}"; do
  echo "Granted commands for $path:"
  jq -r --arg path "$path" '
    .locations[$path].tool_approvals[]
    | select(.kind == "commands")
    | .commandIdentifiers[]
  ' "$CONFIG" | sed 's/^/  - /'
done
