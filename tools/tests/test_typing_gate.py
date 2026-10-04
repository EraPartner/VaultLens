#!/usr/bin/env python3
"""Tests for the typing ratchet's pure logic (no mypy or ruff needed)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import typing_gate  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS  {name}")


def main() -> int:
    debt = typing_gate.read_debt("# header\n\ntools/a.py\n  tools/b.py  \n")
    check("read_debt skips comments and blanks", debt == {"tools/a.py", "tools/b.py"})

    gated, missing = typing_gate.split_files({"tools/a.py", "tools/c.py"}, debt)
    check("unlisted file is gated", gated == ["tools/c.py"])
    check("stale debt entry is reported", missing == ["tools/b.py"])

    on_disk = typing_gate.tracked_python_files(typing_gate.ROOT)
    listed = typing_gate.read_debt(typing_gate.DEBT_FILE.read_text(encoding="utf-8"))
    check("debt list names only existing files", not (listed - on_disk))
    check("gate script is itself gated", "tools/scripts/typing_gate.py" not in listed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
