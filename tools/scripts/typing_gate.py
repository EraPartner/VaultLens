#!/usr/bin/env python3
"""Typing ratchet for tools/**: strict mypy plus ruff ANN on every file not in the debt list.

Debt (tools/typing-debt.txt) may only shrink. A new .py file is not in the debt list, so it is
gated from its first commit. A debt entry that no longer exists or already passes is an error,
which forces the list to be trimmed as files are fixed. mypy and ruff are dev-only tools.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEBT_FILE = ROOT / "tools" / "typing-debt.txt"
CONFIG = ROOT / "tools" / "mypy.ini"


def read_debt(text: str) -> set[str]:
    """Parse the debt list: one repo-relative path per line, '#' comments and blanks ignored."""
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def split_files(all_files: set[str], debt: set[str]) -> tuple[list[str], list[str]]:
    """Return (gated files, debt entries that name no existing file)."""
    return sorted(all_files - debt), sorted(debt - all_files)


def tracked_python_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in (root / "tools").rglob("*.py")
        if "__pycache__" not in path.parts
    }


def run(command: list[str]) -> int:
    print("+", " ".join(command[:6]), "..." if len(command) > 6 else "")
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main() -> int:
    debt = read_debt(DEBT_FILE.read_text(encoding="utf-8"))
    gated, missing = split_files(tracked_python_files(ROOT), debt)
    status = 0
    for entry in missing:
        print(f"typing-debt.txt names a missing file: {entry}", file=sys.stderr)
        status = 1
    status |= run([sys.executable, "-m", "mypy", "--config-file", str(CONFIG), *gated])
    status |= run([sys.executable, "-m", "ruff", "check", "--select", "ANN", *gated])
    # Ratchet: a debt file that is already clean must leave the list.
    for entry in sorted(debt - set(missing)):
        probe = subprocess.run(
            [sys.executable, "-m", "mypy", "--config-file", str(CONFIG), entry],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            print(f"{entry} now passes mypy --strict: remove it from typing-debt.txt", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
