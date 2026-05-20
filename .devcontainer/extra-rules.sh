#!/usr/bin/env bash
# Brain-only egress exception, run by the canonical egress-firewall after the
# base allows and before the catch-all deny: allow direct egress to the host
# Ollama daemon (host.docker.internal:11434). ollama inference is delegated to
# the host (model weights + Metal GPU); the container reaches it over this hole
# with NO_PROXY=host.docker.internal. Exactly one host service is reachable.
OLLAMA_PORT="11434"
# IPv4 only: getent hosts can return the IPv6 host-gateway first, which IPv4
# iptables rejects ("host/network not found"). ahostsv4 forces the IPv4 address.
HOST_GW="$(getent ahostsv4 host.docker.internal 2>/dev/null | awk '{print $1; exit}')"
if [[ -n "$HOST_GW" ]]; then
  iptables -A OUTPUT -d "$HOST_GW" -p tcp --dport "$OLLAMA_PORT" -j ACCEPT
  echo "[firewall] ollama hole: allow -> ${HOST_GW}:${OLLAMA_PORT} (host.docker.internal)."
else
  echo "[firewall] WARN: could not resolve host.docker.internal — ollama egress denied." >&2
fi
