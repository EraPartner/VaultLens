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
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="$HOME/.copilot/permissions-config.json"

# Commands the wiki agents need. Keep in sync with
# READ_ONLY_SHELL_COMMANDS + WRITE_SHELL_COMMANDS in tools/agents/wiki-agent.py.
COMMANDS=(
  qmd
  python3
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

tmp=$(mktemp)
jq --arg path "$REPO_ROOT" --argjson cmds "$cmds_json" '
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

echo "Updated $CONFIG"
echo "Granted commands for $REPO_ROOT:"
jq -r --arg path "$REPO_ROOT" '
  .locations[$path].tool_approvals[]
  | select(.kind == "commands")
  | .commandIdentifiers[]
' "$CONFIG" | sed 's/^/  - /'
