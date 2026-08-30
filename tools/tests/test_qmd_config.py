#!/usr/bin/env python3
"""Tests for the qmd review-inbox consent boundary."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "configure-qmd.py"
SPEC = importlib.util.spec_from_file_location("configure_qmd", SCRIPT)
assert SPEC and SPEC.loader
configure_qmd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure_qmd)


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS  {name}")


def main() -> int:
    base = """collections:
  wiki:
    path: /vault/wiki
    pattern: "**/*.md"
  raw:
    path: /vault/raw
    pattern: "**/*.md"
models:
  embed: example
"""
    updated, changed = configure_qmd.ensure_review_inbox_ignored(base)
    check("adds ignore rule to raw collection", changed and "review-inbox/**" in updated)
    check("does not attach rule to wiki", updated.index("review-inbox/**") > updated.index("  raw:"))

    repeated, changed_again = configure_qmd.ensure_review_inbox_ignored(updated)
    check("is idempotent", not changed_again and repeated == updated)

    existing = base.replace(
        "models:", '    ignore:\n      - "drafts/**"\nmodels:'
    )
    extended, changed_existing = configure_qmd.ensure_review_inbox_ignored(existing)
    check(
        "preserves existing ignore rules",
        changed_existing
        and '      - "drafts/**"' in extended
        and '      - "review-inbox/**"' in extended,
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "index.yml"
        path.write_text(base, encoding="utf-8")
        check("atomic file update changes config", configure_qmd.update_config(path))
        check("second file update is a no-op", not configure_qmd.update_config(path))

    print("\n6 passed, 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
