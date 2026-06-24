# Brain (VaultLens) sandbox image — devcontainer-FREE (built by `container build`).
#
# Self-contained image (no separate root Dockerfile). The former devcontainer-CLI
# build relied on devcontainer FEATURES (node:1, python:1, anthropics/claude-code:1.0)
# plus the local ./features/agent-clis (qmd). apple/container has no feature
# system, so this file installs all of them itself. PDF tooling, hardening, the
# egress proxy, and the baked scripts are unchanged from that build.

FROM debian:bookworm-slim@sha256:0104b334637a5f19aa9c983a91b54c89887c0984081f2068983107a6f6c21eeb

ARG TZ=UTC
ENV TZ="$TZ"
ENV DEBIAN_FRONTEND=noninteractive

# Base utilities + a non-root `dev` user (UID 1000). sudo intentionally absent.
#   poppler-utils -> pdftotext (wiki.py PDF ingest); qpdf -> decrypt PDFs.
#   build-essential/pkg-config/cmake -> qmd native modules (better-sqlite3,
#   tree-sitter, node-llama-cpp) — these were installed by the agent-clis feature.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg lsb-release xz-utils \
      git less procps jq locales openssh-client unzip rsync \
      iptables dnsutils openssl \
      squid-openssl \
      poppler-utils qpdf \
      build-essential pkg-config cmake \
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

# --- Node.js (replaces the `node:1` feature) — official static build ---------
# The tarball is verified against Node's published SHASUMS256.txt before extract
# (detects a corrupt or tampered download). Residual supply-chain gap: SHASUMS256.txt
# is fetched over the same HTTPS channel and is not GPG-verified against Node's keys.
ARG NODE_VERSION=24.15.0
RUN set -eux; \
    case "$(dpkg --print-architecture)" in amd64) na=x64 ;; arm64) na=arm64 ;; *) exit 1 ;; esac; \
    nf="node-v${NODE_VERSION}-linux-${na}.tar.xz"; \
    curl -fsSLo "/tmp/${nf}" "https://nodejs.org/dist/v${NODE_VERSION}/${nf}"; \
    curl -fsSLo /tmp/node-SHASUMS256.txt "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"; \
    (cd /tmp && grep " ${nf}\$" node-SHASUMS256.txt | sha256sum -c -); \
    tar -xJf "/tmp/${nf}" -C /usr/local --strip-components=1 --no-same-owner \
      --exclude='*/CHANGELOG.md' --exclude='*/LICENSE' --exclude='*/README.md'; \
    rm "/tmp/${nf}" /tmp/node-SHASUMS256.txt; \
    mkdir -p /usr/local/share/npm-global

# --- Python 3.12 (replaces the `python:1` feature) — python-build-standalone --
# Prebuilt relocatable CPython (the build uv/rye use): python3 + pip + venv + ssl,
# no compile. Downloaded DIRECTLY from a pinned release asset, NOT resolved via the
# GitHub API: the unauthenticated api.github.com 60-req/hr limit gets exhausted when
# several images build in a row, returning 403 and failing the build (curl exit 22).
# Asset downloads from the releases/download host don't count against that limit.
# Bump by editing PY_RELEASE/PY_VERSION (asset = cpython-<ver>+<release>-<arch>...).
# The asset is verified against the release's combined SHA256SUMS before extract
# (same residual gap as Node: SHA256SUMS is HTTPS-fetched, not signature-verified).
ARG PY_RELEASE=20260510
ARG PY_VERSION=3.12.13
RUN set -eux; \
    case "$(dpkg --print-architecture)" in arm64) pa=aarch64 ;; amd64) pa=x86_64 ;; *) exit 1 ;; esac; \
    asset="cpython-${PY_VERSION}+${PY_RELEASE}-${pa}-unknown-linux-gnu-install_only.tar.gz"; \
    base="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}"; \
    echo "python-build-standalone: ${base}/${asset}"; \
    curl -fsSLo "/tmp/${asset}" "${base}/${asset}"; \
    curl -fsSLo /tmp/py-SHA256SUMS "${base}/SHA256SUMS"; \
    (cd /tmp && grep " ${asset}\$" py-SHA256SUMS | sha256sum -c -); \
    mkdir -p /opt/python; \
    tar -xzf "/tmp/${asset}" --strip-components=1 -C /opt/python; \
    rm "/tmp/${asset}" /tmp/py-SHA256SUMS; \
    [ -x /opt/python/bin/python ] || ln -s python3 /opt/python/bin/python; \
    /opt/python/bin/python3 --version; /opt/python/bin/python3 -m venv --help >/dev/null

# Build-time global prefix for the agent CLIs (claude/qmd). Kept
# ROOT-owned (see the install below) so the unprivileged dev user can't overwrite
# the pinned binaries post-launch (the verify-pins gate fingerprints these paths).
# Runtime `npm i -g` (safe-chain) uses a SEPARATE dev-owned prefix,
# /home/dev/.npm-global, set after the install. Root prefix is FIRST on PATH so the
# CLIs resolve to the pinned copies.
ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=/usr/local/share/npm-global/bin:/home/dev/.npm-global/bin:/opt/python/bin:/usr/local/bin:$PATH
RUN printf 'export PATH="/usr/local/share/npm-global/bin:/home/dev/.npm-global/bin:/opt/python/bin:/usr/local/bin:$PATH"\n' \
      > /etc/profile.d/00-sandbox-path.sh

# --- Agent CLIs (replaces claude-code feature + the local agent-clis feature) -
# Build network is unrestricted (no proxy env set here), so npm can fetch headers
# and the build tools above compile qmd's native modules. Installed as root into
# the root-owned global prefix and deliberately NOT chown'd to dev — so the pinned
# claude/qmd binaries cannot be replaced by the dev user.
RUN npm install -g @anthropic-ai/claude-code@2.1.173 @tobilu/qmd \
  && claude --version && command -v qmd

# Runtime global installs (safe-chain in post-create) go to a SEPARATE dev-owned
# prefix, so they never need write access to the pinned root prefix above.
RUN install -d -o dev -g dev -m 0755 /home/dev/.npm-global
ENV NPM_CONFIG_PREFIX=/home/dev/.npm-global

# Bake privileged scripts into the image (root-owned, source-only repo copies).
COPY init-firewall.sh /usr/local/sbin/egress-firewall
COPY perms-fix.sh     /usr/local/sbin/brain-perms-fix
COPY entrypoint.sh    /usr/local/sbin/brain-entrypoint
COPY squid.conf       /etc/squid/squid.conf
COPY allowlist.txt    /etc/squid/allowlist.txt
RUN chmod 0755 /usr/local/sbin/egress-firewall /usr/local/sbin/brain-perms-fix /usr/local/sbin/brain-entrypoint \
  && chown root:root /usr/local/sbin/egress-firewall /usr/local/sbin/brain-perms-fix /usr/local/sbin/brain-entrypoint

# Launch-integrity check (the binary fingerprint manifest is baked below).
COPY bin/verify-pins /usr/local/bin/brain-verify-pins
RUN chmod 0755 /usr/local/bin/brain-verify-pins

# Egress extra-rules hook. The host-Ollama hole was removed in the apple/container
# migration, so extra-rules.sh is a no-op stub (kept for build/entrypoint stability).
COPY extra-rules.sh /etc/egress/extra-rules.sh
RUN chmod 0755 /etc/egress/extra-rules.sh

# Pre-build the squid TLS-bump cert + cert DB (entrypoint regenerates if missing).
RUN mkdir -p /etc/squid/certs /var/lib/squid /var/log/squid /var/spool/squid \
  && openssl req -new -newkey rsa:2048 -days 3650 -nodes -x509 \
       -subj "/CN=brain-egress-proxy" \
       -keyout /etc/squid/certs/bump.key -out /etc/squid/certs/bump.crt \
  && cat /etc/squid/certs/bump.key /etc/squid/certs/bump.crt > /etc/squid/certs/bump.pem \
  && chmod 600 /etc/squid/certs/bump.key /etc/squid/certs/bump.pem \
  && /usr/lib/squid/security_file_certgen -c -s /var/lib/squid/ssl_db -M 4MB \
  && chown -R proxy:proxy /etc/squid/certs /var/lib/squid /var/log/squid /var/spool/squid

# Pre-create dirs backed by named volumes so a fresh volume inherits dev ownership.
RUN install -d -o dev -g dev -m 0700 /home/dev/.claude \
 && install -d -o dev -g dev -m 0755 /home/dev/.config /home/dev/.config/gh \
                                     /home/dev/.cache /home/dev/.local /home/dev/.local/share

# Pin the agent's executables: record SHA-256 so brain-verify-pins can
# fail-closed on drift at launch. A legit tool upgrade changes these — rebuild.
RUN set -eux; \
    pinfile=/usr/local/share/brain/binary-pins.txt; \
    mkdir -p /usr/local/share/brain; \
    : > "$pinfile"; \
    for b in node npm claude gh git python3 qmd; do \
      p="$(command -v "$b")"; \
      h="$(sha256sum "$p" | awk '{print $1}')"; \
      printf '%s\t%s\t%s\n' "$b" "$p" "$h" >> "$pinfile"; \
    done; \
    chmod 0644 "$pinfile"; \
    cat "$pinfile"

# Strip ALL setuid/setgid bits image-wide (defense-in-depth).
RUN find / -xdev -type f -perm /6000 -exec chmod -s {} + 2>/dev/null || true

WORKDIR /workspaces/Brain

# NOTE: docker-only. apple/container 1.0.0 has no healthcheck runner, so this
#   HEALTHCHECK is a no-op under the current runtime; real supervision is the
#   entrypoint keep-alive loop (squid auto-restart). Kept for parity / docker use.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD bash -c 'pgrep -x squid >/dev/null && /usr/sbin/iptables -S OUTPUT 2>/dev/null | grep -q "^-P OUTPUT DROP"'

ENTRYPOINT ["/usr/local/sbin/brain-entrypoint"]
