#!/usr/bin/env python3
"""Golden tests for wiki-agent.py permission/argv construction.

The orchestrator had zero tests (see tools/AUDIT-2026-06-10.md, finding D); the
B1/B7 findings about `--allowedTools` would be caught here. Pure helpers only, so
no CLI is spawned. Run:

    python3 tools/tests/test_agent.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
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
    check(
        "read-only agent still gets Read+Grep+Glob (B7)", ro == ["Read", "Grep", "Glob"]
    )
    check(
        "read-only agent gets no Bash/Edit/Write",
        not any(t.startswith(("Bash(", "Edit", "Write", "NotebookEdit")) for t in ro),
    )

    sh = wa._build_allowed_tools({"shell": True, "write": False})
    bash_rules = [t for t in sh if t.startswith("Bash(")]
    check("shell agent gets Bash rules", len(bash_rules) > 0)
    check(
        "Bash rules use the space prefix-form, not colon (B1 verified)",
        all(t.endswith(" *)") for t in bash_rules)
        and not any(":*" in t for t in bash_rules),
    )
    check(
        "read-only shell agent cannot Edit/Write",
        not any(t in sh for t in ("Edit", "Write")),
    )

    wr = wa._build_allowed_tools({"shell": True, "write": True})
    check("write agent gets Edit/Write", all(t in wr for t in ("Edit", "Write")))
    check(
        "write agent gets no NotebookEdit (markdown vault, no notebooks)",
        "NotebookEdit" not in wr,
    )
    check(
        "write agent gets more Bash rules than a read-only one",
        len([t for t in wr if t.startswith("Bash(")]) > len(bash_rules),
    )

    print("config integrity:")
    check(
        "every agent in AGENT_FILES has a permission profile",
        set(wa.AGENT_FILES) == set(wa.AGENT_PERMISSIONS),
        str(set(wa.AGENT_FILES) ^ set(wa.AGENT_PERMISSIONS)),
    )

    print("provider command construction:")
    claude_cmd = wa.build_cli_command(
        "claude", "sonnet", "high", "ROLE", "TASK", {"shell": False, "write": False}
    )
    check("Claude uses print mode", claude_cmd[:2] == ["claude", "-p"])
    check(
        "Claude receives role as system prompt",
        "--system-prompt" in claude_cmd and claude_cmd[-1] == "TASK",
    )
    check("Claude model is explicit", ["--model", "sonnet"] == claude_cmd[2:4])

    codex_ro = wa.build_cli_command(
        "codex", "", "medium", "ROLE", "TASK", {"shell": True, "write": False}
    )
    check("Codex uses non-interactive exec", codex_ro[:2] == ["codex", "exec"])
    check("Codex read role gets read-only sandbox", "read-only" in codex_ro)
    check("Codex disables nested agents", "agents.enabled=false" in codex_ro)
    check("Codex default model stays unpinned", "--model" not in codex_ro)
    check(
        "Codex prompt contains role and task",
        "ROLE" in codex_ro[-1] and "TASK" in codex_ro[-1],
    )

    codex_wr = wa.build_cli_command(
        "codex", "gpt-test", "high", "ROLE", "TASK", {"shell": True, "write": True}
    )
    check("Codex writer gets workspace-write sandbox", "workspace-write" in codex_wr)
    check(
        "Codex explicit model is preserved",
        "--model" in codex_wr and codex_wr[codex_wr.index("--model") + 1] == "gpt-test",
    )

    print("generated adapters:")
    generator = Path(__file__).resolve().parents[1] / "agents" / "generate-adapters.py"
    generated = subprocess.run(
        [sys.executable, str(generator), "--check"], capture_output=True, text=True
    )
    check(
        "Claude and Codex manifests match canonical roles",
        generated.returncode == 0,
        (generated.stdout + generated.stderr).strip(),
    )
    codex_agents = wa.ROOT / ".codex" / "agents"
    parsed_agents = [
        tomllib.loads(path.read_text(encoding="utf-8"))
        for path in sorted(codex_agents.glob("*.toml"))
    ]
    check(
        "all Codex manifests parse as TOML", len(parsed_agents) == len(wa.AGENT_FILES)
    )
    check(
        "Codex manifests contain required identity and instructions",
        all(
            {"name", "description", "developer_instructions"} <= data.keys()
            for data in parsed_agents
        ),
    )
    check(
        "Codex manifests distinguish role scope from hard isolation",
        all(
            "instruction boundary" in data["developer_instructions"]
            and "matching container profile" in data["developer_instructions"]
            for data in parsed_agents
        ),
    )

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
