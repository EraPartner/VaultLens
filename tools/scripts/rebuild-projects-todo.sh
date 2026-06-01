#!/usr/bin/env bash
# Regenerates two aggregator views over projects/<slug>/TODO.md files.
#
# Per-project TODOs use the Obsidian Tasks plugin emoji format. Priority:
#   🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest.
# Dates: 📅 due, 🛫 start, ⏳ scheduled, 🔁 recurrence, ➕/✅/❌ created/done/cancelled.
#
# Files written:
#
#   projects/TODO.md         Live aggregator of OPEN tasks via an Obsidian Tasks
#                            query (`not done`, grouped by project). Auto-updates
#                            as project TODOs change. Desktop-only — Tasks queries
#                            do NOT render in the iOS Obsidian widget; use the
#                            widget file there. NOT git-tracked (see .gitignore).
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

# === Live aggregator (desktop, Obsidian Tasks query: open tasks only) ===
{
  echo "# Projects TODO (live)"
  echo
  echo "Live aggregator of OPEN tasks across all projects via an Obsidian Tasks query (auto-updates as you edit each project's \`TODO.md\`; desktop only). The iOS Obsidian widget cannot render queries, point it at \`TODO-widget.md\` instead. Per-project files use the Tasks emoji format: priority 🔺/⏫/🔼/🔽/⏬, dates 📅/🛫/⏳."
  echo
  echo '```tasks'
  echo 'not done'
  echo 'path regex matches /projects\/[^\/]+\/TODO\.md/'
  echo 'group by folder'
  echo 'sort by priority'
  echo '```'
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
