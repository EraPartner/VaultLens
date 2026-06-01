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
#                            instantly in the desktop app (including completed
#                            items). Embeds do NOT render in the iOS Obsidian
#                            widget; use the widget file there instead. NOT
#                            git-tracked (see .gitignore).
#
#   projects/TODO-widget.md  Selection of OPEN items for the iOS widget: an
#                            incomplete task ('- [ ]') with a 📅 due date OR ⏫ high
#                            / 🔺 highest priority. Completed ('- [x]'/'- [X]') and
#                            cancelled ('- [-]') tasks are excluded so rebuilds
#                            never re-add done items. Incomplete subtasks under a
#                            kept parent are included. Projects with no matches are
#                            omitted. Inlined so the widget renders real checkboxes.
#                            NOT git-tracked (regenerated per device; see .gitignore).
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
  echo "Live aggregator: each section embeds the per-project \`TODO.md\` so edits propagate instantly in desktop Obsidian (completed items included). The iOS Obsidian widget cannot render embeds, point it at \`TODO-widget.md\` instead. Per-project files use the Obsidian Tasks plugin emoji format: priority 🔺/⏫/🔼/🔽/⏬, dates 📅/🛫/⏳."
  echo
  for dir in "$PROJECTS_DIR"/*/; do
    slug="$(basename "$dir")"
    todo="$dir/TODO.md"
    [ -f "$todo" ] || continue
    # Skip projects with no real task (no heading-only files, no empty
    # `- [ ]` placeholders): require a checkbox followed by actual text.
    grep -qE '^[[:space:]]*- \[.\][[:space:]]*[^[:space:]]' "$todo" || continue
    echo "## $slug"
    echo "![[projects/$slug/TODO]]"
    echo
  done
} > "$LIVE"

# === Widget aggregator (filtered, inlined) ===
# Flatten all matching blocks across projects, then sort alphabetically by
# the parent line. Subtasks stay glued to their parent: each block is
# emitted on a single line with internal newlines encoded as \v (vertical
# tab), then decoded back to \n after sort.
{
  echo "# Projects TODO (widget)"
  echo
  {
    for dir in "$PROJECTS_DIR"/*/; do
      todo="$dir/TODO.md"
      [ -f "$todo" ] || continue
      awk '
        function flush() {
          if (block != "") print block
          block = ""
        }
        /^- \[/ {
          flush()
          done = ($0 ~ /^- \[[xX-]\]/)
          keep = (!done && ($0 ~ /⏫/ || $0 ~ /🔺/ || $0 ~ /📅/))
          if (keep) block = $0
          next
        }
        /^[ \t]+- \[/ {
          if (keep && block != "" && $0 !~ /^[ \t]+- \[[xX-]\]/) block = block "\v" $0
          next
        }
        END { flush() }
      ' "$todo"
    done
  } | sort | tr '\v' '\n'
} > "$WIDGET"

echo "Wrote $LIVE"
echo "Wrote $WIDGET"
