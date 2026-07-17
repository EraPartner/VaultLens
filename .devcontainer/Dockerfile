# VaultLens sandbox image — devcontainer-FREE (built by `container build`).
#
# Self-contained image (no separate root Dockerfile). The former devcontainer-CLI
# build relied on devcontainer FEATURES (node:1, anthropics/claude-code:1.0)
# layered on by the CLI; apple/container has no feature system, so this file
# installs Node + the Claude CLI itself. Hardening, the egress proxy, and the
# baked privileged scripts are unchanged from that build.
#
# Hardened for Claude CLI in --dangerously-skip-permissions mode:
#   - no privilege escalation: setuid/setgid bits stripped, no sudo (apple/container has no --security-opt)
#   - a root ENTRYPOINT does all privileged setup, then drops to a keep-alive
#   - all egress is forced through an in-container squid proxy enforcing a
#     HOSTNAME allowlist (peek+splice SNI, no TLS interception)

# Pin the base image by digest for reproducibility + supply-chain integrity.
FROM debian:bookworm-slim@sha256:0104b334637a5f19aa9c983a91b54c89887c0984081f2068983107a6f6c21eeb

ARG TZ=UTC
ENV TZ="$TZ"
ENV DEBIAN_FRONTEND=noninteractive

# Base utilities + a non-root `dev` user (UID 1000). sudo intentionally absent.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg lsb-release xz-utils \
      git less procps jq locales openssh-client unzip rsync \
      iptables dnsutils openssl \
      squid-openssl \
      build-essential pkg-config cmake \
      poppler-utils qpdf \
  && rm -rf /var/lib/apt/lists/* \
  && useradd --create-home --uid 1000 --shell /bin/bash dev \
  && echo "C.UTF-8 UTF-8" > /etc/locale.gen && locale-gen

# GitHub CLI from the official cli.github.com apt repo.
RUN install -d -m 0755 /etc/apt/keyrings \
  && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null \
  && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
  && apt-get update && apt-get install -y --no-install-recommends gh \
  && rm -rf /var/lib/apt/lists/*

# --- Node.js (replaces the devcontainer `node:1` feature) --------------------
# The feature installed Node 24 via nvm under /usr/local/share/nvm; we install
# the same version it resolved (24.15.0) from the official static build into
# /usr/local, plus a dev-OWNED global npm prefix so the `dev` user can still
# `npm i -g` at runtime (safe-chain in post-create) — the equivalent of the
# feature's group-writable nvm dir. Bump NODE_VERSION deliberately to update.
ARG NODE_VERSION=24.15.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) na=x64 ;; arm64) na=arm64 ;; *) echo "unsupported arch $arch" >&2; exit 1 ;; esac; \
    curl -fsSLo /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${na}.tar.xz"; \
    curl -fsSLo /tmp/node-SHASUMS256.txt "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"; \
    want="$(grep " node-v${NODE_VERSION}-linux-${na}.tar.xz\$" /tmp/node-SHASUMS256.txt | cut -d' ' -f1)"; \
    echo "${want}  /tmp/node.tar.xz" | sha256sum -c -; \
    rm /tmp/node-SHASUMS256.txt; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
      --exclude='*/CHANGELOG.md' --exclude='*/LICENSE' --exclude='*/README.md'; \
    rm /tmp/node.tar.xz; \
    mkdir -p /usr/local/share/npm-global; \
    node --version; npm --version

# --- Python 3.12 — python-build-standalone (prebuilt relocatable CPython) -----
# The security-guidance plugin's hooks (SessionStart + PostToolUse/Stop) shell
# out to python3 via sg-python.sh; with no python3 on PATH every session start
# errors "no working Python 3 interpreter found." Same pinned-asset approach as
# the Vision/sandbox images (NOT the GitHub API — its 60-req/hr unauthenticated
# limit fails parallel builds). python3 + pip + venv + ssl, no compile.
# The deep SDK/LLM reviewer pip-installs claude-agent-sdk on first start, which
# needs PyPI egress; if the allowlist doesn't grant it that layer degrades
# gracefully (exits 0) and the stdlib-only regex pattern checks still run.
ARG PY_RELEASE=20260510
ARG PY_VERSION=3.12.13
RUN set -eux; \
    case "$(dpkg --print-architecture)" in arm64) pa=aarch64 ;; amd64) pa=x86_64 ;; *) exit 1 ;; esac; \
    url="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/cpython-${PY_VERSION}+${PY_RELEASE}-${pa}-unknown-linux-gnu-install_only.tar.gz"; \
    echo "python-build-standalone: $url"; \
    curl -fsSLo /tmp/py.tar.gz "$url"; \
    curl -fsSLo /tmp/py-SHA256SUMS "https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/SHA256SUMS"; \
    want="$(grep " cpython-${PY_VERSION}+${PY_RELEASE}-${pa}-unknown-linux-gnu-install_only.tar.gz\$" /tmp/py-SHA256SUMS | cut -d' ' -f1)"; \
    echo "${want}  /tmp/py.tar.gz" | sha256sum -c -; \
    rm /tmp/py-SHA256SUMS; \
    mkdir -p /opt/python; \
    tar -xzf /tmp/py.tar.gz --strip-components=1 -C /opt/python; \
    rm /tmp/py.tar.gz; \
    [ -x /opt/python/bin/python ] || ln -s python3 /opt/python/bin/python; \
    /opt/python/bin/python3 --version; /opt/python/bin/python3 -m venv --help >/dev/null

# python-build-standalone bakes CC=clang into sysconfig; this image has gcc (via
# build-essential), not clang. Pin CC=gcc so any source C-extension build works.
ENV CC=gcc CXX=g++
# Build-time global prefix for the agent CLI (claude). Kept ROOT-owned (see the
# claude install below) so the unprivileged dev user can't overwrite the pinned
# claude/npm binaries post-launch (the verify-pins gate fingerprints these paths).
# Use the ENV var, NOT /etc/npmrc: npm's real "global" config is $PREFIX/etc/npmrc,
# so a plain /etc/npmrc is ignored. Runtime `npm i -g` (safe-chain) uses a SEPARATE
# dev-owned prefix, /home/dev/.npm-global, set after the install. Root prefix is
# FIRST on PATH so claude resolves to the pinned copy.
ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=/usr/local/share/npm-global/bin:/home/dev/.npm-global/bin:/opt/python/bin:/usr/local/bin:$PATH
# Login shells (bash -l) reset PATH via /etc/profile, dropping the ENV above;
# re-add the tool dirs so node/npm/claude/safe-chain resolve in login shells too.
RUN printf 'export PATH="/usr/local/share/npm-global/bin:/home/dev/.npm-global/bin:/opt/python/bin:/usr/local/bin:$PATH"\n' \
      > /etc/profile.d/00-npm-global.sh

# --- Agent CLIs: Claude Code + qmd (the wiki search engine) ------------------
# Installed as root into the root-owned global prefix and deliberately NOT chown'd
# to dev — so the pinned claude/qmd/npm cannot be replaced by the dev user. qmd is
# VaultLens's search engine (registered as an MCP server in .mcp.json); its native
# modules (better-sqlite3, tree-sitter, node-llama-cpp) compile against the
# build-essential + cmake installed above. Build network is unrestricted here.
RUN npm install -g @anthropic-ai/claude-code@2.1.173 @tobilu/qmd \
  && claude --version && command -v qmd

# Ruff — VaultLens's Python linter (tools/ruff.toml; run in CI). Installed into the
# root-owned python prefix so `ruff check tools/` resolves on PATH. Not a pinned /
# security-critical binary, so it is intentionally not in the verify-pins manifest.
RUN /opt/python/bin/python3 -m pip install --no-cache-dir ruff \
  && ruff --version

# Runtime global installs (safe-chain in post-create) go to a SEPARATE dev-owned
# prefix, so they never need write access to the pinned root prefix above.
RUN install -d -o dev -g dev -m 0755 /home/dev/.npm-global
ENV NPM_CONFIG_PREFIX=/home/dev/.npm-global

# Bake privileged scripts into the image at /usr/local/sbin (root-owned, not
# writable from inside the container). The .devcontainer copies are source-only.
COPY init-firewall.sh /usr/local/sbin/egress-firewall
COPY perms-fix.sh     /usr/local/sbin/vaultlens-perms-fix
COPY entrypoint.sh    /usr/local/sbin/vaultlens-entrypoint
COPY squid.conf       /etc/squid/squid.conf
COPY allowlist.txt    /etc/squid/allowlist.txt
RUN chmod 0755 /usr/local/sbin/egress-firewall /usr/local/sbin/vaultlens-perms-fix /usr/local/sbin/vaultlens-entrypoint \
  && chown root:root /usr/local/sbin/egress-firewall /usr/local/sbin/vaultlens-perms-fix /usr/local/sbin/vaultlens-entrypoint

# Launch-integrity check (the binary fingerprint manifest is baked below).
COPY bin/verify-pins /usr/local/bin/vaultlens-verify-pins
RUN chmod 0755 /usr/local/bin/vaultlens-verify-pins

# No inbound ports: the VaultLens sandbox publishes no network service (it is a
# tooling dev box, not a server), so there is no /etc/egress/inbound-ports file.

# Pre-build the squid TLS-bump cert + cert DB so first start is fast.
RUN mkdir -p /etc/squid/certs /var/lib/squid /var/log/squid /var/spool/squid \
  && openssl req -new -newkey rsa:2048 -days 3650 -nodes -x509 \
       -subj "/CN=vaultlens-egress-proxy" \
       -keyout /etc/squid/certs/bump.key -out /etc/squid/certs/bump.crt \
  && cat /etc/squid/certs/bump.key /etc/squid/certs/bump.crt > /etc/squid/certs/bump.pem \
  && chmod 600 /etc/squid/certs/bump.key /etc/squid/certs/bump.pem \
  && /usr/lib/squid/security_file_certgen -c -s /var/lib/squid/ssl_db -M 4MB \
  && chown -R proxy:proxy /etc/squid/certs /var/lib/squid /var/log/squid /var/spool/squid

# Pre-create dirs backed by named volumes so a fresh volume inherits dev ownership.
RUN install -d -o dev -g dev -m 0700 /home/dev/.claude \
 && install -d -o dev -g dev -m 0755 /home/dev/.config /home/dev/.config/gh \
                                     /home/dev/.cache /home/dev/.local /home/dev/.local/share

# Pin the agent's executables: record SHA-256 so vaultlens-verify-pins can
# fail-closed on drift at launch. A legit tool upgrade changes these — rebuild.
RUN set -eux; \
    pinfile=/usr/local/share/vaultlens/binary-pins.txt; \
    mkdir -p /usr/local/share/vaultlens; \
    : > "$pinfile"; \
    for b in node npm claude gh git python3 qmd; do \
      p="$(command -v "$b")"; \
      h="$(sha256sum "$p" | awk '{print $1}')"; \
      printf '%s\t%s\t%s\n' "$b" "$p" "$h" >> "$pinfile"; \
    done; \
    chmod 0644 "$pinfile"; \
    cat "$pinfile"

# Strip ALL setuid/setgid bits image-wide (defense-in-depth; inert under
# no-new-privileges, but a generic find won't go stale like a hand-list).
RUN find / -xdev -type f -perm /6000 -exec chmod -s {} + 2>/dev/null || true

WORKDIR /workspaces/VaultLens

# Health: proxy running AND firewall still default-deny.
# NOTE: docker-only. apple/container 1.0.0 has no healthcheck runner, so this
#   HEALTHCHECK is a no-op under the current runtime; real supervision is the
#   entrypoint keep-alive loop (squid auto-restart). Kept for parity / docker use.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD bash -c 'pgrep -x squid >/dev/null && /usr/sbin/iptables -S OUTPUT 2>/dev/null | grep -q "^-P OUTPUT DROP"'

ENTRYPOINT ["/usr/local/sbin/vaultlens-entrypoint"]
