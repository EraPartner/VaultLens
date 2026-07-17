#!/usr/bin/env bash
# /usr/local/sbin/vaultlens-perms-fix
#
# Image-baked helper that performs ONLY the specific ownership / permission
# repairs the devcontainer needs at start time. It is invoked by the root
# ENTRYPOINT (the container has no sudo — it runs with no-new-privileges, and
# all privileged setup happens in the entrypoint). The repo copy at
# .devcontainer/perms-fix.sh is the source — the Dockerfile COPYs it in
# read-only to dev.
#
# Takes no arguments and performs no operations parameterised by the caller.

set -euo pipefail

fix_dir_owner() {
  local dir="$1"
  local owner="$2"
  if [[ -d "$dir" ]] && [[ "$(stat -c %U "$dir")" != "$owner" ]]; then
    chown -R "$owner:$owner" "$dir"
  fi
}

# Named-volume mountpoints come up as root:root on first mount, regardless of
# the image-side directory perms. Repair to dev ownership so dev can write.
fix_dir_owner /home/dev/.claude  dev
fix_dir_owner /home/dev/.config  dev

exit 0
