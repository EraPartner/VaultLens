#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"

install -d "$codex_home"
install -m 0644 "$script_dir/AGENTS.md" "$codex_home/AGENTS.md"
touch "$HOME/.bashrc"
grep -Fqx 'export CODEX_SESSION_ENV=cloud' "$HOME/.bashrc" || \
  printf '%s\n' 'export CODEX_SESSION_ENV=cloud' >> "$HOME/.bashrc"

if ! command -v qmd >/dev/null; then
  # Prefer npm for QMD's native Node addons; older Bun releases can omit build helpers.
  if command -v npm >/dev/null; then
    npm install --global --no-fund --no-audit @tobilu/qmd
  elif command -v bun >/dev/null; then
    bun install --global @tobilu/qmd
  else
    printf '%s\n' 'Bun or npm is required to install QMD.' >&2
    exit 1
  fi
fi

cd "$repo_root"
qmd collection add wiki/ --name wiki 2>/dev/null || true
qmd collection add raw/ --name raw 2>/dev/null || true
qmd update

printf '%s\n' 'VaultLens cloud setup complete. Brain was not accessed; embeddings were skipped.'
