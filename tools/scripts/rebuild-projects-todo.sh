#!/usr/bin/env bash
# Regenerates two aggregator views over projects/<slug>/TODO.md files.
#
# Per-project TODOs use the Obsidian Tasks plugin emoji format. Priority:
#   🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest.
# Dates: 📅 due, 🛫 start, ⏳ scheduled, 🔁 recurrence, ➕/✅/❌ created/done/cancelled.
#
# Files written:
#
#   projects/TODO.md         Live, embed-based. Every non-frozen project gets a section so
#                            the desktop view is a complete, navigable index.
#                            Projects with a real task embed `![[projects/<slug>/TODO]]`
#                            (Obsidian resolves it at render time, so edits
#                            propagate instantly, completed items included);
#                            taskless projects show a placeholder instead of the
#                            empty scaffold, and upgrade to a live embed on the
#                            next rebuild once they gain a task. Embeds do NOT
#                            render in the iOS Obsidian widget; use the widget
#                            file there instead. NOT git-tracked (see .gitignore).
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
# Projects with `status: frozen` are excluded. The project CLI owns that
# lifecycle rule and emits only visible slugs by default.
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
  echo "Live aggregator: every non-frozen project is listed. Sections with a real task embed the per-project \`TODO.md\` so edits propagate instantly in desktop Obsidian (completed items included); taskless projects show a placeholder until they gain a task. The iOS Obsidian widget cannot render embeds, point it at \`TODO-widget.md\` instead. Per-project files use the Obsidian Tasks plugin emoji format: priority 🔺/⏫/🔼/🔽/⏬, dates 📅/🛫/⏳."
  echo
  while IFS= read -r slug; do
    [ -n "$slug" ] || continue
    dir="$PROJECTS_DIR/$slug"
    todo="$dir/TODO.md"
    [ -f "$todo" ] || continue
    echo "## $slug"
    # Every project is listed so the desktop view is a complete, navigable index.
    # Embed the live per-project TODO when it has a real task (a checkbox followed
    # by actual text); otherwise show a placeholder rather than the empty scaffold
    # template. A taskless project upgrades to a live embed on the next rebuild
    # once it gains its first real task.
    if grep -qE '^[[:space:]]*- \[.\][[:space:]]*[^[:space:]]' "$todo"; then
      echo "![[projects/$slug/TODO]]"
    else
      echo "_No open tasks yet._"
    fi
    echo
  done < <(python3 "$ROOT/tools/wiki.py" project list --slugs)
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
    while IFS= read -r slug; do
      [ -n "$slug" ] || continue
      dir="$PROJECTS_DIR/$slug"
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
    done < <(python3 "$ROOT/tools/wiki.py" project list --slugs)
  } | sort | tr '\v' '\n'
} > "$WIDGET"

echo "Wrote $LIVE"
echo "Wrote $WIDGET"
