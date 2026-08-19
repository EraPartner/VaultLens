#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.codex/cloud/lib.sh
source "$script_dir/lib.sh"

vaultlens_cloud_prepare maintenance
