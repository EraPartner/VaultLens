#!/usr/bin/env bash
# /usr/local/sbin/brain-entrypoint
#
# Runs as root (the image's default user) on every container start, BEFORE any
# dev session. Does all privileged setup here so dev sessions have no path to
# root: the image strips all setuid/setgid bits, there's no sudo, and the
# per-container VM boundary contains escalation (apple/container has no
# --security-opt=no-new-privileges equivalent — this is the documented delta).
#
# Order: fix perms -> LOCK EGRESS (firewall, fail-closed) -> start the proxy ->
# hand off to a keep-alive PID 1. The firewall goes up BEFORE the proxy so there
# is no boot window where a racing post-create install could egress unfiltered.
#
# Everything is best-effort and non-fatal: the container must always reach the
# keep-alive so you can exec in and diagnose even if the network is broken.

set -uo pipefail

log() { echo "[entrypoint] $*"; }

# 1) Repair ownership of named-volume mountpoints.
/usr/local/sbin/brain-perms-fix || log "WARN: perms-fix returned non-zero."

# Network pre-flight: if the container lost its external interface (host
# sleep/resume, DD update/reaper), there's no eth0/route and the proxy can't
# resolve anything. Warn with the fix; still lock the firewall and keep the
# container alive so it's diagnosable.
has_iface=0
for iface in /sys/class/net/eth*; do [[ -e "$iface" ]] && has_iface=1; done
default_route=$(awk 'NR>1 && $2=="00000000" {print $1; exit}' /proc/net/route 2>/dev/null)
if (( ! has_iface )) || [[ -z "$default_route" ]]; then
  cat >&2 <<EOF
[entrypoint] ⚠  No external network interface / default route.
[entrypoint]    The proxy won't resolve upstreams until this is fixed.
[entrypoint]    Restart the container:  container stop $HOSTNAME; <launcher>
[entrypoint]    Then restart the container.
EOF
fi

# 2) LOCK EGRESS FIRST. Default-deny + proxy-UID-only (+ the ollama host hole),
#    applied before the proxy (or anything else) can talk to the network. The
#    owner-match rule references the `proxy` user, which exists from image
#    build, so this is valid even before squid starts. fail-closed: see
#    init-firewall.sh.
/usr/local/sbin/egress-firewall || log "WARN: firewall apply returned non-zero (egress stays default-DROP)."

# 3) Ensure the squid TLS-bump cert + cert DB exist, then start squid (it
#    egresses as the proxy UID, which the firewall above permits).
if [[ ! -f /etc/squid/certs/bump.pem ]]; then
  log "Generating squid bump cert..."
  mkdir -p /etc/squid/certs
  openssl req -new -newkey rsa:2048 -days 3650 -nodes -x509 \
    -subj "/CN=brain-egress-proxy" \
    -keyout /etc/squid/certs/bump.key -out /etc/squid/certs/bump.crt >/dev/null 2>&1
  cat /etc/squid/certs/bump.key /etc/squid/certs/bump.crt > /etc/squid/certs/bump.pem
  chmod 600 /etc/squid/certs/bump.key /etc/squid/certs/bump.pem
fi
if [[ ! -d /var/lib/squid/ssl_db ]]; then
  log "Initializing squid ssl_db..."
  mkdir -p /var/lib/squid
  /usr/lib/squid/security_file_certgen -c -s /var/lib/squid/ssl_db -M 4MB >/dev/null 2>&1 || \
    log "WARN: ssl_db init failed."
fi
mkdir -p /var/log/squid /var/spool/squid
chown -R proxy:proxy /etc/squid/certs /var/lib/squid /var/log/squid /var/spool/squid 2>/dev/null || true

log "Starting egress proxy (squid)..."
squid -N >/var/log/squid/boot.log 2>&1 &
for _ in $(seq 1 20); do
  if (exec 3<>/dev/tcp/127.0.0.1/3128) 2>/dev/null; then
    log "Proxy listening on 127.0.0.1:3128."
    break
  fi
  sleep 1
done

# Graceful shutdown on `container stop` (SIGTERM): close squid cleanly so the next
# start doesn't inherit a half-open state. (The launcher's `container run --init`
# runs an init that reaps zombies and forwards SIGTERM here.)
shutdown() { log "shutting down..."; squid -k shutdown 2>/dev/null || true; exit 0; }
trap shutdown TERM INT

log "Setup complete. Container ready (dev sessions via 'container exec')."

# 4) Keep PID 1 alive AND supervise the egress proxy. If squid dies mid-session
#    all egress stops (fail-closed) — restart it so it self-heals. The firewall
#    is independent and stays in force while the proxy is down, so this never
#    opens a gap; it only restores the allowlisted path. Process-existence check
#    (pgrep) — not a socket connect — so it doesn't spam the squid access log.
squid_restarts=0
ACCESS_LOG=/var/log/squid/access.log
LOG_CAP=$(( 50 * 1024 * 1024 ))   # rotate the audit log past ~50 MB
while true; do
  if ! pgrep -x squid >/dev/null 2>&1; then
    squid_restarts=$(( squid_restarts + 1 ))
    log "egress proxy process gone — restarting squid (restart #$squid_restarts)..."
    squid -N >>/var/log/squid/boot.log 2>&1 &
    (( squid_restarts >= 5 )) && \
      log "⚠ squid restarted $squid_restarts times — likely a config error; see /var/log/squid/boot.log"
  fi
  # Bound the egress audit log (logfile_rotate keeps 1 old generation).
  if [[ -f "$ACCESS_LOG" ]] && (( $(stat -c %s "$ACCESS_LOG" 2>/dev/null || echo 0) > LOG_CAP )); then
    squid -k rotate 2>/dev/null || true
  fi
  # Keep the audit log world-readable so `dev` (no longer in the proxy group)
  # can inspect it; squid recreates it 0640 on start/rotate.
  chmod o+r /var/log/squid/access.log* 2>/dev/null || true
  # Background + wait so the TERM trap fires immediately on `container stop`; a bare
  # `sleep 30` would defer graceful squid shutdown up to 30s (risking the kill
  # timeout). (F15)
  sleep 30 & wait $!
done
