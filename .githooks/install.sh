#!/usr/bin/env bash
# Point this repo at the tracked .githooks/ directory and make the hooks
# executable. core.hooksPath is set RELATIVE so it resolves correctly both on
# the host and inside the devcontainer (where the repo root is /workspaces/repo).
# Because .git/config is shared between host and sandbox, running this once
# activates the hooks in both.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

prev="$(git config --local --get core.hooksPath || true)"
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/commit-msg .githooks/install.sh

echo "core.hooksPath -> .githooks"
[ -n "$prev" ] && [ "$prev" != ".githooks" ] && echo "  (replaced previous value: $prev)"
echo "Hooks active for host and devcontainer. Skip a single commit with: git commit --no-verify"
