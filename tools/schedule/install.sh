#!/usr/bin/env bash
# Install / refresh the Brain scheduled-agent LaunchAgent.
#
# User-level only (no sudo): copies the plist into ~/Library/LaunchAgents and
# (re)bootstraps it into the per-user GUI domain. The overnight forced-wake
# (pmset) needs sudo and is NOT run here -- it is printed for you to run.
#
# Re-run this after editing com.brain.schedule.plist or dispatch.py.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.brain.schedule"
SRC="$HERE/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LLM_CLI="${VAULTLENS_LLM_CLI:-claude}"

case "$LLM_CLI" in
  claude|codex) ;;
  *)
    echo "VAULTLENS_LLM_CLI must be 'claude' or 'codex' (got '$LLM_CLI')" >&2
    exit 2
    ;;
esac

[[ -f "$SRC" ]] || { echo "missing $SRC" >&2; exit 1; }

echo "==> creating ~/.brain/logs"
mkdir -p "$HOME/.brain/logs"

echo "==> installing $DEST"
mkdir -p "$HOME/Library/LaunchAgents"
cp "$SRC" "$DEST"
/usr/bin/plutil -insert EnvironmentVariables.VAULTLENS_LLM_CLI \
  -string "$LLM_CLI" "$DEST"
if [[ -n "${VAULTLENS_LLM_MODEL:-}" ]]; then
  /usr/bin/plutil -insert EnvironmentVariables.VAULTLENS_LLM_MODEL \
    -string "$VAULTLENS_LLM_MODEL" "$DEST"
fi
if [[ -n "${VAULTLENS_LLM_HEALTH_HOST:-}" ]]; then
  /usr/bin/plutil -insert EnvironmentVariables.VAULTLENS_LLM_HEALTH_HOST \
    -string "$VAULTLENS_LLM_HEALTH_HOST" "$DEST"
fi
if [[ -n "${VAULTLENS_LLM_IDENTITY:-}" ]]; then
  /usr/bin/plutil -insert EnvironmentVariables.VAULTLENS_LLM_IDENTITY \
    -string "$VAULTLENS_LLM_IDENTITY" "$DEST"
fi
if [[ -n "${VAULTLENS_SCHEDULE_ENHANCE:-}" ]]; then
  /usr/bin/plutil -insert EnvironmentVariables.VAULTLENS_SCHEDULE_ENHANCE \
    -string "$VAULTLENS_SCHEDULE_ENHANCE" "$DEST"
fi

echo "==> scheduled LLM backend: $LLM_CLI"
if [[ "$LLM_CLI" == "codex" ]]; then
  echo "warning: Codex scheduling needs the deferred container launcher support" >&2
fi

echo "==> (re)bootstrapping $LABEL into $DOMAIN"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$DEST"
launchctl enable "$DOMAIN/$LABEL"

echo "==> kickstarting one run now"
launchctl kickstart -k "$DOMAIN/$LABEL" || true

cat <<EOF

Installed. Useful commands:
  launchctl print $DOMAIN/$LABEL          # full agent state
  python3 "$HERE/dispatch.py" status      # ledger / accounts / wakes
  python3 "$HERE/dispatch.py" run --dry-run

Backend selection is captured when this installer runs:
  VAULTLENS_LLM_CLI=claude tools/schedule/install.sh
  VAULTLENS_LLM_CLI=codex tools/schedule/install.sh   # after container launcher support lands
Optional overrides: VAULTLENS_LLM_MODEL, VAULTLENS_LLM_HEALTH_HOST,
VAULTLENS_LLM_IDENTITY. Nightly wiki enhancement is paused by default; opt in
when installing with VAULTLENS_SCHEDULE_ENHANCE=1.

To enable the overnight forced wake (AC-gated in the dispatcher), run with sudo:
  sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00
  pmset -g sched                          # verify
To remove the wake later:
  sudo pmset repeat cancel

To run the nightly batch with the LID CLOSED on AC (no external display needed),
install the least-privilege sudoers rule (3 exact pmset calls, nothing else):
  sudo install -m 0440 -o root -g wheel "$HERE/brain-schedule.sudoers" /etc/sudoers.d/brain-schedule
  sudo visudo -cf /etc/sudoers.d/brain-schedule   # must print "parsed OK"
Without it, lid-closed nights are skipped and caught up when you next open on AC.
To remove it:
  sudo rm /etc/sudoers.d/brain-schedule

To uninstall the agent:
  launchctl bootout $DOMAIN/$LABEL
  rm "$DEST"
EOF
