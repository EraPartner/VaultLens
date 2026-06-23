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

[[ -f "$SRC" ]] || { echo "missing $SRC" >&2; exit 1; }

echo "==> creating ~/.brain/logs"
mkdir -p "$HOME/.brain/logs"

echo "==> installing $DEST"
mkdir -p "$HOME/Library/LaunchAgents"
cp "$SRC" "$DEST"

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
