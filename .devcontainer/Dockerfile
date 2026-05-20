# Brain (VaultLens) devcontainer — runs the wiki-builder agent stack natively
# inside the container: the Python orchestrator (tools/wiki.py, wiki-agent.py)
# plus the four agent CLIs it shells out to (claude, copilot, opencode, ollama),
# the qmd local search engine, and the PDF ingest tooling. No Docker-in-Docker.
#
# Hardened for the agent CLIs in --dangerously-skip-permissions / headless mode:
#   - runs with --security-opt=no-new-privileges (no sudo, no setuid escalation)
#   - a root ENTRYPOINT does all privileged setup (perms, egress proxy,
#     firewall), then drops to a keep-alive; dev sessions have no path to root
#   - all egress is forced through an in-container squid proxy enforcing a
#     HOSTNAME allowlist (peek+splice SNI, no TLS interception), EXCEPT a single
#     hole to host.docker.internal:11434 so the local `ollama` daemon on the
#     host is reachable (deliberate, user-approved isolation tradeoff).
#
# Tools installed by features (devcontainer.json): node, python, claude-code.
# The remaining agent CLIs (copilot, opencode, qmd) are npm globals installed at
# runtime by post-create.sh — the Node feature that provides npm runs in a build
# stage AFTER this Dockerfile, so they can't be installed here.
# ollama is NOT installed here — inference is delegated to the host daemon.

# Pin the base image by digest for reproducibility + supply-chain integrity.
FROM debian:bookworm-slim@sha256:0104b334637a5f19aa9c983a91b54c89887c0984081f2068983107a6f6c21eeb

ARG TZ=UTC
ENV TZ="$TZ"
ENV DEBIAN_FRONTEND=noninteractive

# Base utilities + a non-root `dev` user (UID 1000) so the bind-mounted vault
# ownership lines up with the host user. NOTE: sudo is intentionally NOT
# installed — the container runs with no-new-privileges (sudo couldn't gain root
# anyway) and all privileged setup happens in the root ENTRYPOINT.
#   poppler-utils -> pdftotext   (wiki.py PDF ingest)
#   qpdf          -> decrypt copy-protected PDFs before pdftotext
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg lsb-release \
      git less procps jq locales openssh-client unzip rsync \
      iptables dnsutils openssl \
      squid-openssl \
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

# Bake privileged scripts into the image at /usr/local/sbin (root-owned, not
# writable from inside the container). The repo copies under .devcontainer/ are
# source-only — a rewrite there cannot affect the running container, which
# closes an escalation path (rewrite workspace script -> trigger root re-apply).
COPY init-firewall.sh /usr/local/sbin/egress-firewall
COPY perms-fix.sh     /usr/local/sbin/brain-perms-fix
COPY entrypoint.sh    /usr/local/sbin/brain-entrypoint
COPY squid.conf       /etc/squid/squid.conf
COPY allowlist.txt    /etc/squid/allowlist.txt
RUN chmod 0755 /usr/local/sbin/egress-firewall /usr/local/sbin/brain-perms-fix /usr/local/sbin/brain-entrypoint \
  && chown root:root /usr/local/sbin/egress-firewall /usr/local/sbin/brain-perms-fix /usr/local/sbin/brain-entrypoint

# Brain-only egress exception: the host-Ollama hole, applied by the canonical
# egress-firewall via its /etc/egress/extra-rules.sh hook (host.docker.internal:11434).
COPY extra-rules.sh /etc/egress/extra-rules.sh
RUN chmod 0755 /etc/egress/extra-rules.sh

# Pre-build the squid TLS-bump cert + cert DB so first start is fast (the
# entrypoint regenerates either if missing, e.g. on a tmpfs). The key (and the
# .pem that contains it) are 0600 — squid reads them at startup as root, so dev
# never needs to. (This bump CA is in no trust store and allowed traffic is
# spliced, so it can't MITM anything, but a private key shouldn't be world-readable.)
RUN mkdir -p /etc/squid/certs /var/lib/squid /var/log/squid /var/spool/squid \
  && openssl req -new -newkey rsa:2048 -days 3650 -nodes -x509 \
       -subj "/CN=brain-egress-proxy" \
       -keyout /etc/squid/certs/bump.key -out /etc/squid/certs/bump.crt \
  && cat /etc/squid/certs/bump.key /etc/squid/certs/bump.crt > /etc/squid/certs/bump.pem \
  && chmod 600 /etc/squid/certs/bump.key /etc/squid/certs/bump.pem \
  && /usr/lib/squid/security_file_certgen -c -s /var/lib/squid/ssl_db -M 4MB \
  && chown -R proxy:proxy /etc/squid/certs /var/lib/squid /var/log/squid /var/spool/squid

# NOTE: we deliberately do NOT write an apt proxy config. devcontainer features
# (node, python, claude-code) run their own apt installs in a later build stage
# that inherits this filesystem; an apt proxy pointing at squid (which only runs
# at container runtime) would make those feature builds fail to fetch packages.
# Runtime apt isn't needed anyway (dev has no sudo under no-new-privileges). If
# you ever need runtime apt, route it through the proxy explicitly:
#   apt-get -o Acquire::http::Proxy=http://127.0.0.1:3128 ...

# Pre-create dirs backed by named volumes so a fresh volume inherits dev
# ownership on first mount (the entrypoint's perms-fix is the runtime backstop).
#   .claude         claude config (sanitized-stage seeded)
#   .copilot        copilot CLI config + repo permission allowlist
#   .config         qmd / gh config
#   .cache          qmd index + embedding models (seeded from host)
#   .local          opencode data + auth.json (token forwarded by the wrapper)
RUN install -d -o dev -g dev -m 0700 /home/dev/.claude /home/dev/.copilot \
 && install -d -o dev -g dev -m 0755 /home/dev/.config /home/dev/.config/gh \
                                     /home/dev/.cache /home/dev/.local /home/dev/.local/share

# Strip ALL setuid/setgid bits image-wide. Inert under no-new-privileges, but
# clean defense-in-depth — and a generic find won't go stale like a hand-list
# (it also catches ssh-keysign, unix_chkpwd, squid/pinger). We use `runuser`
# (not setuid) for privilege drops and squid's pinger is unused (no cache peers),
# so nothing here needs elevated bits.
RUN find / -xdev -type f -perm /6000 -exec chmod -s {} + 2>/dev/null || true

# Container process (PID 1 / entrypoint) runs as root so it can apply iptables
# and start the proxy; devcontainer.json sets remoteUser=dev for sessions and
# --security-opt=no-new-privileges so dev cannot escalate.
WORKDIR /workspaces/Brain

# Health: the egress proxy must be running AND the firewall must still be
# enforcing default-deny (the firewall apply is best-effort/non-fatal, so this
# turns a silent fail-open into a visible `unhealthy`). Uses pgrep, not a socket
# connect, so it doesn't spam the squid access log with empty-probe errors.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD bash -c 'pgrep -x squid >/dev/null && /usr/sbin/iptables -S OUTPUT 2>/dev/null | grep -q "^-P OUTPUT DROP"'

ENTRYPOINT ["/usr/local/sbin/brain-entrypoint"]
