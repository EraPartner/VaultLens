#!/usr/bin/env python3
"""Golden tests for wiki-agent.py permission/argv construction.

The orchestrator had zero tests (see tools/AUDIT-2026-06-10.md, finding D); the
B1/B7 findings about `--allowedTools` would be caught here. Pure helpers only, so
no CLI is spawned. Run:

    python3 tools/tests/test_agent.py
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
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

    print("Chief of Staff queue context:")
    original_root = wa.ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wa.ROOT = Path(tmp)
            inbox = wa.ROOT / "raw" / "inbox"
            review = wa.ROOT / "raw" / "review-inbox"
            inbox.mkdir(parents=True)
            review.mkdir(parents=True)
            (inbox / "ready.md").write_text("READY BODY", encoding="utf-8")
            (review / "ask-first.md").write_text("PRIVATE BODY", encoding="utf-8")
            context = wa._gather_cos_context("brief", None)
            check(
                "brief lists normal and review queue names",
                "ready.md" in context and "ask-first.md" in context,
            )
            check(
                "review queue is consent-gated and not previewed",
                "Consent required" in context and "PRIVATE BODY" not in context,
            )
    finally:
        wa.ROOT = original_root

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
    manifest_paths = sorted(codex_agents.glob("*.toml"))
    expected_manifests = {
        f"{Path(filename).stem}.toml" for filename in wa.AGENT_FILES.values()
    }
    check(
        "Codex manifest filenames match canonical roles",
        {path.name for path in manifest_paths} == expected_manifests,
        str(sorted(path.name for path in manifest_paths)),
    )
    parsed_agents = []
    parse_errors = []
    for path in manifest_paths:
        try:
            parsed_agents.append(tomllib.loads(path.read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as error:
            parse_errors.append(f"{path.name}: {error}")
    check("all Codex manifests parse as TOML", not parse_errors, "; ".join(parse_errors))
    check(
        "Codex manifests contain required identity and instructions",
        all(
            {"name", "description", "developer_instructions"} <= data.keys()
            for data in parsed_agents
        ),
    )
    conflict_copies = sorted(
        path.relative_to(wa.ROOT).as_posix()
        for directory in (wa.ROOT / ".agents", wa.ROOT / ".claude", wa.ROOT / ".codex")
        for path in directory.rglob("*")
        if path.is_file() and re.search(r" \d+(?=\.[^.]+$)", path.name)
    )
    check(
        "provider configuration has no conflict copies",
        not conflict_copies,
        ", ".join(conflict_copies),
    )
    check(
        "Codex manifests distinguish role scope from hard isolation",
        all(
            "instruction boundary" in data["developer_instructions"]
            and "matching container profile" in data["developer_instructions"]
            for data in parsed_agents
        ),
    )

    print("project provider parity:")
    projects = sorted(
        project_md.parent
        for project_md in (wa.ROOT / "projects").glob("*/project.md")
    )
    expected_claude_adapter = (
        "@AGENTS.md\n"
        "@project.md\n\n"
        "# Claude Code compatibility\n\n"
        "`AGENTS.md` is the provider-neutral project instruction source.\n"
    )
    missing_neutral_sources = [
        project.name for project in projects if not (project / "AGENTS.md").is_file()
    ]
    check(
        "every project has provider-neutral AGENTS.md",
        not missing_neutral_sources,
        ", ".join(missing_neutral_sources),
    )
    stale_claude_adapters = [
        project.name
        for project in projects
        if not (project / "CLAUDE.md").is_file()
        or (project / "CLAUDE.md").read_text(encoding="utf-8")
        != expected_claude_adapter
    ]
    check(
        "every project Claude adapter is thin and canonical",
        not stale_claude_adapters,
        ", ".join(stale_claude_adapters),
    )

    mcp_projects = [
        project
        for project in projects
        if (project / ".mcp.json").exists()
        or (project / ".codex" / "config.toml").exists()
    ]
    mcp_errors: list[str] = []
    arxiv_servers: list[tuple[str, dict[str, object]]] = []
    for project in mcp_projects:
        claude_path = project / ".mcp.json"
        codex_path = project / ".codex" / "config.toml"
        if not claude_path.is_file() or not codex_path.is_file():
            mcp_errors.append(f"{project.name}: missing one provider MCP config")
            continue
        try:
            claude_data = json.loads(claude_path.read_text(encoding="utf-8"))
            codex_data = tomllib.loads(codex_path.read_text(encoding="utf-8"))
            claude_servers = claude_data["mcpServers"]
            codex_servers = codex_data["mcp_servers"]
        except (json.JSONDecodeError, tomllib.TOMLDecodeError, KeyError) as error:
            mcp_errors.append(f"{project.name}: {error}")
            continue
        if not isinstance(claude_servers, dict) or not isinstance(codex_servers, dict):
            mcp_errors.append(f"{project.name}: MCP server tables must be objects")
            continue
        if set(claude_servers) != set(codex_servers):
            mcp_errors.append(f"{project.name}: provider server names differ")
            continue
        for name in sorted(claude_servers):
            claude_server = claude_servers[name]
            codex_server = codex_servers[name]
            if not isinstance(claude_server, dict) or not isinstance(codex_server, dict):
                mcp_errors.append(f"{project.name}/{name}: server config must be an object")
                continue
            claude_launch = (claude_server.get("command"), claude_server.get("args", []))
            codex_launch = (codex_server.get("command"), codex_server.get("args", []))
            if claude_launch != codex_launch:
                mcp_errors.append(f"{project.name}/{name}: launch command differs")
            if name == "arxiv":
                arxiv_servers.append((project.name, codex_server))
    check(
        "project MCP launch configuration matches across providers",
        not mcp_errors,
        "; ".join(mcp_errors),
    )

    arxiv_errors: list[str] = []
    for project_name, server in arxiv_servers:
        args = server.get("args", [])
        if server.get("command") != "uvx" or not isinstance(args, list) or not args:
            arxiv_errors.append(f"{project_name}: arxiv must launch with uvx")
            continue
        if not re.fullmatch(r"arxiv-mcp-server==\d+\.\d+\.\d+", str(args[0])):
            arxiv_errors.append(f"{project_name}: arxiv package is not exactly pinned")
        try:
            storage_path = str(args[args.index("--storage-path") + 1])
        except (ValueError, IndexError):
            arxiv_errors.append(f"{project_name}: storage path is missing")
            continue
        if (
            Path(storage_path).is_absolute()
            or storage_path.startswith("~")
            or "$" in storage_path
        ):
            arxiv_errors.append(f"{project_name}: storage path is host-dependent")
    check(
        "arxiv MCP is version-pinned with a portable cache path",
        not arxiv_errors,
        "; ".join(arxiv_errors),
    )

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
