#!/usr/bin/env bash
# Point this repo at the tracked .githooks/ directory and make the hooks
# executable. core.hooksPath is set RELATIVE so it resolves from any checkout
# location and any worktree.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

prev="$(git config --local --get core.hooksPath || true)"
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/commit-msg .githooks/pre-push .githooks/install.sh

echo "core.hooksPath -> .githooks"
[ -n "$prev" ] && [ "$prev" != ".githooks" ] && echo "  (replaced previous value: $prev)"
echo "Hooks active. Skip a single commit with: git commit --no-verify"
