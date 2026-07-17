#!/usr/bin/env bash
# CANONICAL egress firewall — proxy-only model. Shared, identical across all
# devcontainers. DO NOT edit per-project copies: edit THIS file in
# Projects/LockBox/ and run ./sync.sh, then rebuild.
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
# We add no NAT rules of our own; leave the NAT table untouched.
ip6tables -F
ip6tables -X 2>/dev/null || true

# --- IPv6: loopback only ---
ip6tables -A INPUT  -i lo -j ACCEPT
# DNS-exfil hardening parity with the IPv4 loopback rules below: if the base image
# ever ran a resolver bound to ::1:53 (systemd-resolved / dnsmasq can), a non-proxy
# UID could otherwise reach it over loopback as an exfil channel. Drop non-proxy
# loopback DNS before the blanket loopback ACCEPT, exactly as IPv4 does. Reachability
# is near-nil today (Docker's embedded resolver is IPv4 127.0.0.11; apple/container's
# is external), but this keeps IPv6 loopback fail-closed too.
ip6tables -A OUTPUT -o lo -m owner ! --uid-owner "$PROXY_USER" -p udp --dport 53 -j DROP
ip6tables -A OUTPUT -o lo -m owner ! --uid-owner "$PROXY_USER" -p tcp --dport 53 -j DROP
ip6tables -A OUTPUT -o lo -j ACCEPT

# --- IPv4 base allows ---
iptables -A INPUT  -i lo -j ACCEPT
# Loopback OUTPUT lets processes reach squid on 127.0.0.1:3128 (and any local
# service/DB/IPC a project runs on loopback). DNS-exfil hardening: a loopback-
# resident DNS resolver would otherwise be reachable by ANY UID over loopback and
# could front a data-exfil channel (Docker parity: the embedded resolver at
# 127.0.0.11:53). apple/container's resolver is EXTERNAL (the 192.168.64.0/24
# gateway, e.g. 192.168.64.1), so no loopback resolver exists there — a non-proxy
# query to it is already dropped by the default-deny + proxy-UID lock below
# (verified: `dmesg | grep egress-deny` shows DST=192.168.64.1 DPT=53 dropped).
# To stay fail-closed under Docker too, deny NON-PROXY loopback DNS explicitly;
# the proxy UID keeps full loopback (so it can still use such a resolver to serve
# squid), and every other loopback flow is left untouched.
iptables -A OUTPUT -o lo -m owner ! --uid-owner "$PROXY_USER" -p udp --dport 53 -j DROP
iptables -A OUTPUT -o lo -m owner ! --uid-owner "$PROXY_USER" -p tcp --dport 53 -j DROP
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# OUTPUT: accept ESTABLISHED only — NOT RELATED. The proxy UID's blanket ACCEPT
# below already covers every legitimate outbound flow, so non-proxy UIDs lose
# nothing by dropping RELATED; this removes the (remote) chance that a conntrack
# protocol helper could open a RELATED data channel that skips the UID lock.
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED -j ACCEPT

# Defense-in-depth: block the cloud metadata endpoint at L3, AHEAD of the proxy-UID
# ACCEPT below (which would otherwise let the `proxy` UID reach it). squid already
# denies it (squid.conf `http_access deny metadata`), so this is belt-and-suspenders
# — it survives a squid ACL removal/reorder and refuses an SSRF/DNS-rebinding read of
# 169.254.169.254 even then. The IP is never allowlisted, so nothing legitimate drops.
iptables  -A OUTPUT -d 169.254.169.254/32 -j DROP
ip6tables -A OUTPUT -d fd00:ec2::254/128  -j DROP

# Only the proxy UID may originate outbound traffic; everything else uses the
# proxy over loopback.
iptables -A OUTPUT -m owner --uid-owner "$PROXY_USER" -j ACCEPT

# Inbound on forwarded ports, if this project declares any (host browser -> app).
if [[ -r "$INBOUND_FILE" ]]; then
  # `|| [[ -n "$port" ]]` so a final line with no trailing newline is not dropped;
  # `read` itself trims leading/trailing whitespace, so blank/padded lines need no
  # extra stripping (and interior whitespace stays put to fail validation below).
  # Validate as a TCP port in 1..65535: without the range check a value like `0` or
  # `99999` passes the regex, iptables --dport errors, and (no `set -e`) the port is
  # silently never opened — the service is unreachable with no diagnostic. The regex
  # short-circuits the arithmetic, so `10#` (base-10, so a leading zero like `08` is
  # not misread as octal) only runs on all-digit input.
  while read -r port || [[ -n "$port" ]]; do
    [[ -z "$port" ]] && continue
    if [[ "$port" =~ ^[0-9]+$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )); then
      iptables -A INPUT -p tcp --dport "$((10#$port))" -j ACCEPT
    else
      echo "[firewall] WARN: ignoring invalid inbound port '$port' (must be 1..65535)." >&2
    fi
  done < "$INBOUND_FILE"
fi

# Project-specific extra OUTPUT allows (optional, via an executable extra-rules.sh
# in the image), run AFTER the base allows and BEFORE the catch-all deny. Optional,
# project-owned file — each rule it adds is a deliberate exception.
[[ -x "$EXTRA_RULES" ]] && "$EXTRA_RULES" || true

# Logged-DROP for everything else, rate-limited. Visible via `dmesg | grep egress-deny`.
iptables -N EGRESS_DENY
iptables -A EGRESS_DENY -m limit --limit 10/min -j LOG --log-prefix "egress-deny: " --log-level 4
iptables -A EGRESS_DENY -j DROP
iptables -A OUTPUT -j EGRESS_DENY

# --- Verify the lock took, then drop the sentinel ---
# Independent invariants that must ALL hold (script runs without `set -e`, so a
# mid-script `iptables -A` failure can't be assumed to have aborted):
#   - IPv4 AND IPv6 OUTPUT policies are DROP (the IPv6 check matters because the
#     design leans on `ip6tables -P OUTPUT DROP` as the IPv6 backstop, see squid.conf);
#   - the two NON-PROXY loopback-DNS DROP rules are present (so if either failed to
#     insert, the loopback resolver is NOT silently reopened as an exfil channel
#     while we still claim "verified" — the Docker-parity concern above);
#   - the proxy-UID ACCEPT exists;
#   - the catch-all EGRESS_DENY jump is present (a ruleset that silently lost the
#     deny/audit rule fails closed).
# The `-C` checks pass the byte-identical rule spec used with `-A` above, so they
# match iff that exact rule is installed. (Fail-closed: any mismatch skips the
# sentinel, so the entrypoint's wait-for-sentinel stalls loudly rather than leaking.)
if iptables  -S OUTPUT 2>/dev/null | grep -q '^-P OUTPUT DROP' \
   && ip6tables -S OUTPUT 2>/dev/null | grep -q '^-P OUTPUT DROP' \
   && iptables -C OUTPUT -o lo -m owner ! --uid-owner "$PROXY_USER" -p udp --dport 53 -j DROP 2>/dev/null \
   && iptables -C OUTPUT -o lo -m owner ! --uid-owner "$PROXY_USER" -p tcp --dport 53 -j DROP 2>/dev/null \
   && iptables -C OUTPUT -m owner --uid-owner "$PROXY_USER" -j ACCEPT 2>/dev/null \
   && iptables -C OUTPUT -j EGRESS_DENY 2>/dev/null; then
  if : > "$SENTINEL" 2>/dev/null; then
    echo "[firewall] Egress locked to proxy UID '$PROXY_USER' (IPv4 + IPv6 default-deny, verified)."
  else
    # The lock is up, but the entrypoint waits on this sentinel — a missing sentinel
    # would stall it with misleading "success" logs. Treat the write failure as fatal.
    echo "[firewall] ERROR: egress lock verified but sentinel write to $SENTINEL FAILED" >&2
    echo "[firewall]   (is /run writable?) — failing closed so the stall is diagnosable." >&2
    exit 1
  fi
else
  rm -f "$SENTINEL" 2>/dev/null || true
  echo "[firewall] ERROR: egress-lock verification FAILED — egress stays default-DROP (fail-closed)." >&2
  exit 1
fi

# ─── vendored by LockBox v0.1.0 · canonical sha256:01973fca9fe15413b77d0b74501983800a56fb9a67a36f13edef4e63b49a3e76 ───
# Generated from the canonical source by LockBox/sync.sh — DO NOT EDIT HERE.
# Edit LockBox/init-firewall.sh and re-run ./sync.sh.
