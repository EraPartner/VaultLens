#!/usr/bin/env bash
# Install / refresh the Brain scheduled-agent LaunchAgent.
#
# User-level only (no sudo): renders the plist template for this checkout and
# account (render_plist.py), installs the result into ~/Library/LaunchAgents and
# (re)bootstraps it into the per-user GUI domain. The overnight forced-wake
# (pmset) and the sudoers rule need sudo and are NOT run here -- they are
# printed for you to run (--render-sudoers fills in the sudoers template).
#
# Re-run this after editing com.brain.schedule.plist, dispatch.py or
# render_plist.py; the dispatcher itself needs no reinstall for code-only edits
# because launchd runs it straight from this checkout.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.brain.schedule"
SRC="$HERE/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
MODE="${1:---install}"
case "$MODE" in
  --install|--prepare-disabled|--enable-prepared|--render|--render-sudoers) ;;
  *)
    echo "usage: $0 [--install|--prepare-disabled|--enable-prepared|--render OUTPUT|--render-sudoers OUTPUT]" >&2
    exit 2
    ;;
esac

PYTHON="${BRAIN_PYTHON:-/opt/homebrew/bin/python3}"
if [[ -n "${BRAIN_PYTHON:-}" ]]; then
  PYTHON="$(command -v "$BRAIN_PYTHON")" || {
    echo "BRAIN_PYTHON must name an executable Python interpreter" >&2
    exit 2
  }
elif [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)" || {
    echo "Python 3.11 or newer is required; set BRAIN_PYTHON to its executable" >&2
    exit 2
  }
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Detected Python " + sys.version.split()[0])'; then
  echo "Python 3.11 or newer is required; set BRAIN_PYTHON to its executable" >&2
  exit 2
fi

if [[ "$MODE" == "--render" ]]; then
  [[ $# -eq 2 ]] || { echo "--render requires an output path" >&2; exit 2; }
  exec "$PYTHON" "$HERE/render_plist.py" "$SRC" "$2" --python-executable "$PYTHON"
fi

if [[ "$MODE" == "--render-sudoers" ]]; then
  [[ $# -eq 2 ]] || { echo "--render-sudoers requires an output path" >&2; exit 2; }
  exec "$PYTHON" "$HERE/render_plist.py" --sudoers "$HERE/brain-schedule.sudoers" "$2"
fi

if [[ "$MODE" == "--enable-prepared" ]]; then
  [[ -f "$DEST" ]] || { echo "missing prepared plist: $DEST" >&2; exit 1; }
  "$PYTHON" "$HERE/render_plist.py" "$DEST" --validate
  echo "==> enabling prepared $LABEL"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl enable "$DOMAIN/$LABEL"
  launchctl bootstrap "$DOMAIN" "$DEST"
  echo "Enabled. RunAtLoad starts one gate-check now; calendar triggers handle later runs."
  exit 0
fi

[[ -f "$SRC" ]] || { echo "missing $SRC" >&2; exit 1; }
# Validate and render before touching the installed job or its enable state.
PREPARED="$(mktemp "${TMPDIR:-/tmp}/brain-schedule.XXXXXX")"
trap 'rm -f "$PREPARED"' EXIT
"$PYTHON" "$HERE/render_plist.py" "$SRC" "$PREPARED" --python-executable "$PYTHON"

echo "==> creating ~/.brain/logs"
mkdir -p "$HOME/.brain/logs"
chmod 700 "$HOME/.brain"   # logs and project snapshots are private

echo "==> installing $DEST"
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PREPARED" "$DEST"

echo "==> unloading any existing $LABEL from $DOMAIN"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

if [[ "$MODE" == "--prepare-disabled" ]]; then
  launchctl disable "$DOMAIN/$LABEL"
  echo "Prepared and disabled. Provider selection is validated above."
  echo "Enable later with: tools/schedule/install.sh --enable-prepared"
  exit 0
fi

echo "==> (re)bootstrapping $LABEL into $DOMAIN"
launchctl enable "$DOMAIN/$LABEL"
launchctl bootstrap "$DOMAIN" "$DEST"
echo "RunAtLoad starts one gate-check now; calendar triggers handle later runs."

cat <<EOF

Installed. Useful commands:
  launchctl print $DOMAIN/$LABEL          # full agent state
  python3 "$HERE/dispatch.py" status      # ledger / accounts / wakes
  python3 "$HERE/dispatch.py" run --dry-run

Provider selection normally follows tools/llm.local.json on each new tick:
  python3 tools/llm_provider.py select claude
  python3 tools/llm_provider.py select codex   # after one-time profile logins
  tools/schedule/install.sh --prepare-disabled
  tools/schedule/install.sh --enable-prepared
  tools/schedule/install.sh --render /tmp/brain-schedule.plist   # preview only
Explicit VAULTLENS_LLM_CLI, VAULTLENS_LLM_MODEL, VAULTLENS_LLM_HEALTH_HOST,
or VAULTLENS_LLM_IDENTITY overrides are captured in the installed plist and
take precedence over the shared file. Reinstall without them to remove them.
Nightly wiki enhancement is paused by default; opt in
when installing with VAULTLENS_SCHEDULE_ENHANCE=1.

To enable the overnight forced wake (AC-gated in the dispatcher), run with sudo:
  sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00
  pmset -g sched                          # verify
To remove the wake later:
  sudo pmset repeat cancel

To run the nightly batch with the LID CLOSED on AC (no external display needed),
install the least-privilege sudoers rule (3 exact pmset calls, nothing else):
  rule_dir="\$(mktemp -d)"                         # private directory, not shared /tmp
  "$HERE/install.sh" --render-sudoers "\$rule_dir/brain-schedule.sudoers"
  sudo visudo -cf "\$rule_dir/brain-schedule.sudoers"      # must print "parsed OK"
  sudo install -m 0440 -o root -g wheel "\$rule_dir/brain-schedule.sudoers" /etc/sudoers.d/brain-schedule
Without it, lid-closed nights are skipped and caught up when you next open on AC.
To remove it:
  sudo rm /etc/sudoers.d/brain-schedule

To uninstall the agent:
  launchctl bootout $DOMAIN/$LABEL
  rm "$DEST"
EOF
