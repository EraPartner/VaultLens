#!/bin/bash
# Interactive login through the verified, note-free native agent runtime.
set -eu
umask 077
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
if [[ -n "${BRAIN_PYTHON:-}" ]]; then
  python="$BRAIN_PYTHON"
elif [[ -x /opt/homebrew/bin/python3 ]]; then
  python=/opt/homebrew/bin/python3
else
  python=python3
fi
if [[ $# != 1 ]]; then
  printf 'Usage: bash %s codex|claude\n' "$0" >&2
  exit 2
fi
case "$1" in codex|claude) ;; *) printf 'Provider must be codex or claude.\n' >&2; exit 2 ;; esac
if [[ ! -t 0 || ! -t 1 ]]; then
  printf 'Run this login helper in your own interactive terminal.\n' >&2
  exit 2
fi
case "$1" in
  codex)
    printf 'Sign in using the displayed Codex device-login instructions.\n'
    exec "$python" "$root/tools/local_runtime.py" exec --private-cwd --profile selected-read --cli codex -- \
      codex -c 'cli_auth_credentials_store="file"' \
      -c 'check_for_update_on_startup=false' login --device-auth
    ;;
  claude)
    printf 'Complete Claude browser login and paste any returned code only in this terminal.\n'
    exec "$python" "$root/tools/local_runtime.py" exec --private-cwd --profile selected-read --cli claude -- \
      claude --setting-sources '' \
      --settings '{"forceLoginMethod":"claudeai","disableClaudeAiConnectors":true,"disableAllHooks":true}' \
      auth login --claudeai
    ;;
esac
