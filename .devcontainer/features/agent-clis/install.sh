#!/usr/bin/env bash
# Brain devcontainer feature: install the agent CLIs at IMAGE-BUILD time.
#
# Why a feature and not post-create.sh: post-create runs AFTER the root
# entrypoint locks egress (squid SNI allowlist + iptables), so `npm install -g`
# there cannot reach nodejs.org for node-gyp headers, nor compile native addons
# without a toolchain — qmd's better-sqlite3 / tree-sitter / node-llama-cpp then
# fail and qmd ends up "command not found". Features run during the build phase
# where the network is unrestricted and we can apt-install a compiler, so the
# CLIs are baked into the image and Just Work at the locked-down runtime.
#
# Runs as root during the build. Installs into the same node the runtime uses
# (the node feature's nvm), so the globals land on the runtime PATH.

set -euo pipefail

echo "[agent-clis] Installing native-build toolchain..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
# build-essential + pkg-config + cmake cover node-gyp / node-llama-cpp source
# builds; python3 is what node-gyp invokes. (All inert at runtime under
# no-new-privileges — there is no source-build path once egress is locked.)
apt-get install -y --no-install-recommends build-essential pkg-config cmake python3
rm -rf /var/lib/apt/lists/*

# The proxy env (HTTP(S)_PROXY) is for the locked RUNTIME; it must not leak into
# this build-phase install (the proxy isn't running yet — npm would hang/fail).
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy \
      npm_config_proxy npm_config_https_proxy 2>/dev/null || true

# Locate npm from the node feature (nvm). It is normally on PATH for later
# features, but source nvm as a fallback so install order can't break us.
if ! command -v npm >/dev/null 2>&1 && [ -s /usr/local/share/nvm/nvm.sh ]; then
  export NVM_DIR=/usr/local/share/nvm
  # shellcheck disable=SC1091
  . /usr/local/share/nvm/nvm.sh
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "[agent-clis] ERROR: npm not found — is the node feature listed before this one?" >&2
  exit 1
fi
echo "[agent-clis] Using $(command -v npm) ($(npm --version)), node $(node --version)."

echo "[agent-clis] Installing agent CLIs globally (build network is unrestricted)..."
npm install -g @github/copilot opencode-ai @tobilu/qmd

echo "[agent-clis] Installed globals:"
npm ls -g --depth=0 || true

# Fail the build loudly if qmd's binary didn't materialize (the exact failure
# mode this feature exists to prevent).
if ! command -v qmd >/dev/null 2>&1; then
  echo "[agent-clis] ERROR: qmd install did not produce a runnable binary." >&2
  exit 1
fi
echo "[agent-clis] Done. qmd at $(command -v qmd)."
