#!/usr/bin/env bash
# Brain devcontainer egress firewall — proxy-only model (+ one ollama hole).
#
# Baked into the image at /usr/local/sbin/brain-firewall; invoked by the root
# entrypoint on every container start. The repo copy at
# .devcontainer/init-firewall.sh is the source — edits require a rebuild.
#
# Egress is locked to the squid proxy's UID only. Everything else in the
# container (dev sessions, npm postinstalls, the agent CLIs, …) must go through
# the proxy on 127.0.0.1:3128, where squid enforces the hostname allowlist (see
# squid.conf). A process that ignores HTTPS_PROXY and tries to connect directly
# is dropped here, because its socket UID is not `proxy`.
#
# THE ONE EXCEPTION: host.docker.internal:11434 (the host's ollama daemon) is
# allowed directly for any UID. ollama inference is delegated to the host (the
# Mac has the model weights + Metal GPU); the container's ollama CLI / python
# reach it over this hole, with NO_PROXY=host.docker.internal so the client does
# not try to route it through squid. This is a deliberate, user-approved
# isolation tradeoff — the container can talk to exactly one host service.
#
# This replaces the older IP-allowlist/ipset approach: hostname enforcement now
# lives in the proxy, so there is no DNS-resolution dance and no stale-IP
# problem. iptables just answers "who is allowed to egress at all" (= proxy, +
# the ollama host:port).

set -uo pipefail

PROXY_USER="proxy"
SENTINEL="/run/brain-firewall-ok"
OLLAMA_PORT="11434"

# Stale sentinel must never outlive a re-apply: clear it up front so a partial
# failure below can't leave a "verified" marker from a previous run.
rm -f "$SENTINEL" 2>/dev/null || true

# --- FAIL-CLOSED FIRST ---
# Set default-deny BEFORE flushing/adding anything. set -e is intentionally off
# (best-effort apply), so if any rule below fails mid-way the netns is already
# closed and stays closed — never silently fail-open.
iptables  -P INPUT   DROP
iptables  -P OUTPUT  DROP
iptables  -P FORWARD DROP
ip6tables -P INPUT   DROP
ip6tables -P OUTPUT  DROP
ip6tables -P FORWARD DROP

# --- Reset rules (policies set above stay DROP across a flush) ---
iptables -F
iptables -F BRAIN_DENY 2>/dev/null || true
iptables -X BRAIN_DENY 2>/dev/null || true
iptables -X 2>/dev/null || true
# NOTE: we deliberately do NOT flush the NAT table. We add no NAT rules of our
# own, and on plain Docker/bridge networking the embedded DNS (127.0.0.11) is
# NAT-based — flushing it would break name resolution. Leaving NAT untouched
# keeps this portable beyond Docker Desktop.
ip6tables -F
ip6tables -X 2>/dev/null || true

# --- IPv6: loopback only (everything else stays default-DROP) ---
ip6tables -A INPUT  -i lo -j ACCEPT
ip6tables -A OUTPUT -o lo -j ACCEPT

# --- IPv4 base allows ---
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Only the proxy UID may originate outbound traffic (DNS, 80, 443, …).
# Everything else must use the proxy over loopback.
iptables -A OUTPUT -m owner --uid-owner "$PROXY_USER" -j ACCEPT

# --- The ollama hole: allow direct egress to the host daemon only ---------
# Docker Desktop injects host.docker.internal into /etc/hosts (and via
# --add-host=host.docker.internal:host-gateway in runArgs), so getent resolves
# it without needing the network. Allow TCP to that single IP:11434 for any UID.
HOST_GW="$(getent hosts host.docker.internal 2>/dev/null | awk '{print $1; exit}')"
if [[ -n "$HOST_GW" ]]; then
  iptables -A OUTPUT -d "$HOST_GW" -p tcp --dport "$OLLAMA_PORT" -j ACCEPT
  echo "[firewall] ollama hole: allow -> ${HOST_GW}:${OLLAMA_PORT} (host.docker.internal)."
else
  echo "[firewall] WARN: could not resolve host.docker.internal — ollama egress will be denied." >&2
fi

# Logged-DROP for everything else, rate-limited so a loop can't flood dmesg.
# Visible via `dmesg | grep brain-deny`.
iptables -N BRAIN_DENY
iptables -A BRAIN_DENY -m limit --limit 10/min -j LOG --log-prefix "brain-deny: " --log-level 4
iptables -A BRAIN_DENY -j DROP
iptables -A OUTPUT -j BRAIN_DENY

# --- Verify the lock actually took, then drop the sentinel ---
# post-start.sh refuses to proceed if the sentinel is missing, and the
# Dockerfile HEALTHCHECK independently re-checks the default policy.
if iptables -S OUTPUT 2>/dev/null | grep -q '^-P OUTPUT DROP' \
   && iptables -C OUTPUT -m owner --uid-owner "$PROXY_USER" -j ACCEPT 2>/dev/null; then
  : > "$SENTINEL" 2>/dev/null || true
  echo "[firewall] Egress locked to proxy UID '$PROXY_USER' (IPv4 + IPv6 default-deny, verified)."
else
  rm -f "$SENTINEL" 2>/dev/null || true
  echo "[firewall] ERROR: egress-lock verification FAILED — egress stays default-DROP (fail-closed)." >&2
  exit 1
fi
