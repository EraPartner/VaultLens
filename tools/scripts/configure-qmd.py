#!/usr/bin/env python3
"""Apply Brain-specific qmd collection safeguards to a qmd YAML config."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

REVIEW_IGNORE = '      - "review-inbox/**"'


def ensure_review_inbox_ignored(text: str) -> tuple[str, bool]:
    """Ensure the raw collection ignores review-inbox without reformatting YAML."""
    lines = text.splitlines()
    raw_start = next(
        (index for index, line in enumerate(lines) if line.strip() == "raw:" and line.startswith("  ")),
        None,
    )
    if raw_start is None:
        raise ValueError("qmd config has no collections.raw entry")

    raw_end = len(lines)
    for index in range(raw_start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith("    "):
            raw_end = index
            break

    ignore_start = next(
        (
            index
            for index in range(raw_start + 1, raw_end)
            if lines[index].strip() == "ignore:" and lines[index].startswith("    ")
        ),
        None,
    )
    if ignore_start is None:
        lines[raw_end:raw_end] = ["    ignore:", REVIEW_IGNORE]
        return "\n".join(lines) + "\n", True

    ignore_end = raw_end
    for index in range(ignore_start + 1, raw_end):
        line = lines[index]
        if line.strip() and not line.startswith("      "):
            ignore_end = index
            break

    normalized = {
        line.strip().lstrip("- ").strip("\"'")
        for line in lines[ignore_start + 1 : ignore_end]
    }
    if "review-inbox/**" in normalized:
        return text, False

    lines.insert(ignore_end, REVIEW_IGNORE)
    return "\n".join(lines) + "\n", True


def update_config(path: Path) -> bool:
    current = path.read_text(encoding="utf-8")
    updated, changed = ensure_review_inbox_ignored(current)
    if not changed:
        return False

    mode = path.stat().st_mode
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(updated)
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path.home() / ".config" / "qmd" / "index.yml",
    )
    args = parser.parse_args(argv)
    try:
        changed = update_config(args.config)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(
        "Added raw/review-inbox qmd exclusion."
        if changed
        else "raw/review-inbox qmd exclusion already present."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
