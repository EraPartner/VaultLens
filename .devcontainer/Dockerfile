# Brain (VaultLens) sandbox image — devcontainer-FREE (built by docker compose).
#
# Self-contained twin of ./Dockerfile. The devcontainer build relied on
# devcontainer FEATURES (node:1, python:1, anthropics/claude-code:1.0) plus the
# local ./features/agent-clis (copilot, opencode, qmd). Compose has no feature
# system, so this file installs all of them itself. Everything else (PDF tooling,
# hardening, egress proxy + ollama hole, baked scripts) is identical to Dockerfile.

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
ARG NODE_VERSION=24.15.0
RUN set -eux; \
    case "$(dpkg --print-architecture)" in amd64) na=x64 ;; arm64) na=arm64 ;; *) exit 1 ;; esac; \
    curl -fsSLo /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${na}.tar.xz"; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
      --exclude='*/CHANGELOG.md' --exclude='*/LICENSE' --exclude='*/README.md'; \
    rm /tmp/node.tar.xz; \
    mkdir -p /usr/local/share/npm-global

# --- Python 3.12 (replaces the `python:1` feature) — python-build-standalone --
# Prebuilt relocatable CPython (the build uv/rye use): python3 + pip + venv + ssl,
# no compile. Downloaded DIRECTLY from a pinned release asset, NOT resolved via the
# GitHub API: the unauthenticated api.github.com 60-req/hr limit gets exhausted when
# several images build in a row, returning 403 and failing the build (curl exit 22).
# Asset downloads from the releases/download host don't count against that limit.
# Bump by editing PY_RELEASE/PY_VERSION (asset = cpython-<ver>+<release>-<arch>...).
ARG PY_RELEASE=20260510
ARG PY_VERSION=3.12.13
RUN set -eux; \
    case "$(dpkg --print-architecture)" in arm64) pa=aarch64 ;; amd64) pa=x86_64 ;; *) exit 1 ;; esac; \
    url="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/cpython-${PY_VERSION}+${PY_RELEASE}-${pa}-unknown-linux-gnu-install_only.tar.gz"; \
    echo "python-build-standalone: $url"; \
    curl -fsSLo /tmp/py.tar.gz "$url"; \
    mkdir -p /opt/python; \
    tar -xzf /tmp/py.tar.gz --strip-components=1 -C /opt/python; \
    rm /tmp/py.tar.gz; \
    [ -x /opt/python/bin/python ] || ln -s python3 /opt/python/bin/python; \
    /opt/python/bin/python3 --version; /opt/python/bin/python3 -m venv --help >/dev/null

# Global npm prefix -> a dev-owned dir so runtime `npm i -g` (safe-chain) works.
ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=/usr/local/share/npm-global/bin:/opt/python/bin:/usr/local/bin:$PATH
RUN printf 'export PATH="/usr/local/share/npm-global/bin:/opt/python/bin:/usr/local/bin:$PATH"\n' \
      > /etc/profile.d/00-sandbox-path.sh

# --- Agent CLIs (replaces claude-code feature + the local agent-clis feature) -
# Build network is unrestricted (no proxy env set here), so npm can fetch headers
# and the build tools above compile qmd's native modules. Installed to the global
# prefix as root, then chown'd to dev (so runtime `npm i -g` can also write it).
RUN npm install -g @anthropic-ai/claude-code @github/copilot opencode-ai @tobilu/qmd \
  && claude --version && command -v qmd && command -v copilot && command -v opencode \
  && chown -R dev:dev /usr/local/share/npm-global

# Bake privileged scripts into the image (root-owned, source-only repo copies).
COPY init-firewall.sh /usr/local/sbin/egress-firewall
COPY perms-fix.sh     /usr/local/sbin/brain-perms-fix
COPY entrypoint.sh    /usr/local/sbin/brain-entrypoint
COPY squid.conf       /etc/squid/squid.conf
COPY allowlist.txt    /etc/squid/allowlist.txt
RUN chmod 0755 /usr/local/sbin/egress-firewall /usr/local/sbin/brain-perms-fix /usr/local/sbin/brain-entrypoint \
  && chown root:root /usr/local/sbin/egress-firewall /usr/local/sbin/brain-perms-fix /usr/local/sbin/brain-entrypoint

# Brain-only egress exception: the host-Ollama hole (host.docker.internal:11434).
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
RUN install -d -o dev -g dev -m 0700 /home/dev/.claude /home/dev/.copilot \
 && install -d -o dev -g dev -m 0755 /home/dev/.config /home/dev/.config/gh \
                                     /home/dev/.cache /home/dev/.local /home/dev/.local/share

# Strip ALL setuid/setgid bits image-wide (defense-in-depth).
RUN find / -xdev -type f -perm /6000 -exec chmod -s {} + 2>/dev/null || true

WORKDIR /workspaces/Brain

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD bash -c 'pgrep -x squid >/dev/null && /usr/sbin/iptables -S OUTPUT 2>/dev/null | grep -q "^-P OUTPUT DROP"'

ENTRYPOINT ["/usr/local/sbin/brain-entrypoint"]
