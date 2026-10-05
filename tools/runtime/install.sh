#!/bin/sh
# Explicit, pinned installation. Agents never install or upgrade this runtime.
set -eu
runtime_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
runtime_state="$runtime_root/tools/runtime-state"
mkdir -p "$runtime_state/npm-home"
chmod 700 "$runtime_state" "$runtime_state/npm-home"
command -v node >/dev/null || { printf 'Node.js 22.12 or newer is required but node was not found\n' >&2; exit 1; }
command -v npm >/dev/null || { printf 'npm is required but was not found\n' >&2; exit 1; }
node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || major === 22 && minor < 12) { process.stderr.write("Node.js 22.12 or newer is required\n"); process.exit(1); }'
# The whole dependency tree is locked with integrity hashes: npm ci installs exactly
# tools/runtime/package-lock.json and fails if it disagrees with package.json.
# Reviewed pin: '@anthropic-ai/sandbox-runtime@0.0.78'
mkdir -p "$runtime_root/tools/runtime-node"
cp "$runtime_root/tools/runtime/package.json" "$runtime_root/tools/runtime/package-lock.json" \
  "$runtime_root/tools/runtime-node/"
cd "$runtime_state/npm-home"
env -i PATH="$PATH" HOME="$runtime_state/npm-home" \
  npm ci --prefix "$runtime_root/tools/runtime-node" \
  --ignore-scripts --no-audit --no-fund \
  --fetch-retries 0 --fetch-timeout 20000 \
  --userconfig "$runtime_state/npm-user.config" \
  --globalconfig "$runtime_state/npm-global.config" \
  --cache "$runtime_state/npm-cache"
printf 'Installed candidate runtime. Run tools/runtime/probe.py before enabling jobs.\n'
