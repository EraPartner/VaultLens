#!/usr/bin/env bash
# Regenerates two aggregator views over projects/<slug>/TODO.md files.
#
# Per-project TODOs use the Obsidian Tasks plugin emoji format. Priority:
#   🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest.
# Dates: 📅 due, 🛫 start, ⏳ scheduled, 🔁 recurrence, ➕/✅/❌ created/done/cancelled.
#
# Files written:
#
#   projects/TODO.md         Live, embed-based. Each project section is a
#                            `![[projects/<slug>/TODO]]` embed that Obsidian
#                            resolves at render time, so edits propagate
#                            instantly in the desktop app. Embeds and Tasks
#                            query blocks do NOT render in the iOS Obsidian
#                            widget; use the widget file there instead.
#
#   projects/TODO-widget.md  Selection of items relevant for the iOS widget:
#                            anything with a 📅 due date OR ⏫ high / 🔺 highest
#                            priority. Subtasks indented under a matching
#                            parent are included. Projects with no matches
#                            are omitted entirely. Inlined so the widget
#                            renders real checkboxes.
#
# Some per-project files are symlinks into product repos (vision, watchman).
# `[ -f ]` follows symlinks, so they're handled the same way.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PROJECTS_DIR="$ROOT/projects"
LIVE="$PROJECTS_DIR/TODO.md"
WIDGET="$PROJECTS_DIR/TODO-widget.md"

# === Live embedded aggregator (desktop) ===
{
  echo "# Projects TODO (live)"
  echo
  echo "Live aggregator: each section embeds the per-project \`TODO.md\` so edits propagate instantly in desktop Obsidian. The iOS Obsidian widget cannot render embeds, point it at \`TODO-widget.md\` instead. Per-project files use the Obsidian Tasks plugin emoji format: priority 🔺/⏫/🔼/🔽/⏬, dates 📅/🛫/⏳."
  echo
  for dir in "$PROJECTS_DIR"/*/; do
    slug="$(basename "$dir")"
    todo="$dir/TODO.md"
    [ -f "$todo" ] || continue
    echo "## $slug"
    echo "![[projects/$slug/TODO]]"
    echo
  done
} > "$LIVE"

# === Widget aggregator (filtered, inlined) ===
{
  echo "# Projects TODO (widget)"
  echo
  echo "Filtered selection for the iOS Obsidian widget: items with a 📅 due date or ⏫/🔺 priority (plus their subtasks). Projects with no matches are omitted. Run \`tools/scripts/rebuild-projects-todo.sh\` to refresh."
  echo
  for dir in "$PROJECTS_DIR"/*/; do
    slug="$(basename "$dir")"
    todo="$dir/TODO.md"
    [ -f "$todo" ] || continue
    # Keep top-level items that have a due date or high/highest priority
    # emoji, plus any indented subtask under such an item.
    matched=$(awk '
      /^- \[/ {
        keep = ($0 ~ /⏫/ || $0 ~ /🔺/ || $0 ~ /📅/)
        if (keep) print
        next
      }
      /^[ \t]+- \[/ {
        if (keep) print
        next
      }
      { next }
    ' "$todo")
    if [ -n "$matched" ]; then
      echo "## $slug"
      echo
      printf '%s\n' "$matched"
      echo
    fi
  done
} > "$WIDGET"

echo "Wrote $LIVE"
echo "Wrote $WIDGET"
