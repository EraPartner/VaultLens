#!/usr/bin/env python3
"""Golden tests for wiki-agent.py permission/argv construction.

The orchestrator had zero tests (see tools/AUDIT-2026-06-10.md, finding D); the
B1/B7 findings about `--allowedTools` would be caught here. Pure helpers only, so
no CLI is spawned. Run:

    python3 tools/tests/test_agent.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "wiki_agent", str(Path(__file__).resolve().parents[1] / "agents" / "wiki-agent.py")
)
wa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wa)

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    print("_build_allowed_tools:")
    ro = wa._build_allowed_tools({"shell": False, "write": False})
    check("read-only agent still gets Read+Grep+Glob (B7)", ro == ["Read", "Grep", "Glob"])
    check("read-only agent gets no Bash/Edit/Write",
          not any(t.startswith(("Bash(", "Edit", "Write", "NotebookEdit")) for t in ro))

    sh = wa._build_allowed_tools({"shell": True, "write": False})
    bash_rules = [t for t in sh if t.startswith("Bash(")]
    check("shell agent gets Bash rules", len(bash_rules) > 0)
    check("Bash rules use the space prefix-form, not colon (B1 verified)",
          all(t.endswith(" *)") for t in bash_rules) and not any(":*" in t for t in bash_rules))
    check("read-only shell agent cannot Edit/Write", not any(t in sh for t in ("Edit", "Write")))

    wr = wa._build_allowed_tools({"shell": True, "write": True})
    check("write agent gets Edit/Write/NotebookEdit",
          all(t in wr for t in ("Edit", "Write", "NotebookEdit")))
    check("write agent gets more Bash rules than a read-only one",
          len([t for t in wr if t.startswith("Bash(")]) > len(bash_rules))

    print("config integrity:")
    check("every agent in AGENT_FILES has a permission profile",
          set(wa.AGENT_FILES) == set(wa.AGENT_PERMISSIONS),
          str(set(wa.AGENT_FILES) ^ set(wa.AGENT_PERMISSIONS)))

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
