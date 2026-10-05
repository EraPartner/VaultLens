#!/usr/bin/env python3
"""Apply Brain-specific qmd collection safeguards to a qmd YAML config."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

REVIEW_RULE = "review-inbox/**"
REVIEW_IGNORE = f'      - "{REVIEW_RULE}"'


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
            if lines[index].startswith("    ignore:")
        ),
        None,
    )
    if ignore_start is None:
        lines[raw_end:raw_end] = ["    ignore:", REVIEW_IGNORE]
        return "\n".join(lines) + "\n", True

    value = lines[ignore_start][len("    ignore:") :].strip()
    if value.startswith("["):
        return _extend_flow_list(lines, ignore_start, value, text)
    if value and not value.startswith("#"):
        raise ValueError(
            "qmd config collections.raw.ignore is not a list; "
            f"add {REVIEW_RULE!r} to it by hand"
        )

    # Block list. Its items may sit at the key's own indent or deeper.
    item_indent = "      "
    ignore_end = raw_end
    for index in range(ignore_start + 1, raw_end):
        line = lines[index]
        if not line.strip():
            continue
        indent = line[: len(line) - len(line.lstrip(" "))]
        if len(indent) >= 6 or (indent == "    " and line.lstrip().startswith("- ")):
            item_indent = indent
        else:
            ignore_end = index
            break

    normalized = {
        line.strip().lstrip("- ").strip("\"'")
        for line in lines[ignore_start + 1 : ignore_end]
    }
    if REVIEW_RULE in normalized:
        return text, False

    lines.insert(ignore_end, f'{item_indent}- "{REVIEW_RULE}"')
    return "\n".join(lines) + "\n", True


def _extend_flow_list(
    lines: list[str], index: int, value: str, text: str
) -> tuple[str, bool]:
    """Extend ``ignore: [a, b]`` in place; anything fancier is left to the operator."""
    if not value.endswith("]") or "#" in value:
        raise ValueError(
            "qmd config collections.raw.ignore is an inline list this script cannot "
            f"edit safely; add {REVIEW_RULE!r} to it by hand"
        )
    inner = value[1:-1].strip()
    existing = {item.strip().strip("\"'") for item in inner.split(",") if item.strip()}
    if REVIEW_RULE in existing:
        return text, False
    joined = f'{inner}, "{REVIEW_RULE}"' if inner else f'"{REVIEW_RULE}"'
    lines[index] = f"    ignore: [{joined}]"
    return "\n".join(lines) + "\n", True


def update_config(path: Path) -> bool:
    current = path.read_text(encoding="utf-8")
    updated, changed = ensure_review_inbox_ignored(current)
    if not changed:
        return False

    mode = path.stat().st_mode
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(updated)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
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
