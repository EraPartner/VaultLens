#!/usr/bin/env python3
"""Tests for the typed test-module loader."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _loader import load_module  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS  {name}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "hyphen-name.py"
        path.write_text("VALUE = 1\n\ndef double(n):\n    return n * 2\n")

        module = load_module("loader_fixture", path)
        check("reads module attributes", module.VALUE == 1 and module.double(3) == 6)
        check("does not register by default", "loader_fixture" not in sys.modules)

        module.VALUE = 5
        check("allows rebinding attributes", module.VALUE == 5)

        registered = load_module("loader_fixture_registered", path, register=True)
        try:
            check("registers on request", id(sys.modules["loader_fixture_registered"]) == id(registered))
        finally:
            sys.modules.pop("loader_fixture_registered", None)

        try:
            load_module("loader_fixture_missing", Path(tmp) / "absent.txt")
        except ImportError:
            check("rejects an unloadable path", True)
        else:
            check("rejects an unloadable path", False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
