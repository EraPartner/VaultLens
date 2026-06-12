#!/usr/bin/env bash
# Brain per-project egress exception hook, run by the canonical egress-firewall
# after the base allows and before the catch-all deny.
#
# NO-OP. Ollama was dropped from the apple/container migration (no host reach on
# apple/container without `container system dns`, which needs sudo + disables
# Private Relay — not worth it for a feature not currently used). With Ollama gone
# there are no per-project OUTPUT exceptions, so this stub adds none and the Brain
# egress lock is identical to the other sandboxes' (proxy-UID-only).
#
# If local inference/embeddings return later, re-add the host hole here (and the
# OLLAMA_HOST env + host reachability in bin/agent) against whatever host-reach
# tooling apple/container provides at that point.
set -uo pipefail
exit 0
