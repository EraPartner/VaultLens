"""Shared role capabilities for native manifests and unattended launches.

Local runtime access profiles enforce file and network boundaries. Tool grants
keep each role usable without granting arbitrary shell research to readers.
These legacy role classes describe tools; they never select actual file roots.
"""

from __future__ import annotations

from copy import deepcopy

CAPABILITIES = {
    "read": {"shell": False, "write": False, "writable_dirs": []},
    "read-shell": {"shell": True, "write": False, "writable_dirs": []},
    "wiki-write": {"shell": True, "write": True, "writable_dirs": ["wiki"]},
    "project-write": {
        "shell": True,
        "write": True,
        "writable_dirs": ["projects"],
        "python_shell": True,
    },
}
QMD_TOOLS = "mcp__qmd__*"

# Shell command prefixes granted to agents with shell access. This is a
# best-effort usability allowlist, not the security boundary: shell syntax and
# command options can still have side effects. The whole-process runtime's
# access profile is the enforced read/write boundary. Keep each rule as narrow
# as practical for usability and mistakes in role assignment.
READ_ONLY_SHELL_COMMANDS = (
    "ls",
    "grep",
    "cat",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "cut",
    "tr",
    "date",
    "qmd",
)
READ_ONLY_SHELL_RULES = (
    "python3 tools/wiki.py lint",
    "python3 tools/wiki.py lint --strict",
    "python3 tools/wiki.py lint --json",
    "python3 tools/wiki.py lint --strict --json",
    "python3 tools/wiki.py search *",
    "python3 tools/wiki.py coverage *",
    "python3 tools/wiki.py tags *",
    "python3 tools/wiki.py next-id",
    "python3 tools/wiki.py stats",
    "python3 tools/wiki.py sample *",
    "python3 tools/wiki.py validate-log",
    "python3 tools/wiki.py archive list *",
    "python3 tools/wiki.py inventory list *",
    "python3 tools/wiki.py inventory show *",
    "python3 tools/wiki.py project list *",
    "python3 tools/wiki.py project show *",
    "python3 tools/wiki.py project agenda status *",
    "python3 tools/wiki.py project agenda due *",
    "python3 tools/wiki.py project agenda clarifications *",
    "python3 tools/wiki.py project agenda lint *",
    "python3 tools/wiki.py project agenda new-id *",
)
WRITE_SHELL_RULES = ("python3 tools/wiki.py *",)

# Shell commands granted only to write-capable agents. Filesystem mutators
# (mkdir/touch/mv/cp) and text editors used in scripted edits (sed/awk in-place).
# sed/awk are full scripting engines. A `sed -i` can rewrite any writable path.
# The runtime must allow writes only to the resolved wiki or project roots and
# protect raw sources, tools, provider configuration and Git metadata. A native
# manifest alone provides no such boundary.
WRITE_SHELL_COMMANDS = ("touch", "mkdir", "mv", "cp", "sed", "awk")


def profile_capabilities(profile: str) -> dict:
    if profile not in CAPABILITIES:
        raise ValueError(f"Unknown permission profile: {profile}")
    return deepcopy(CAPABILITIES[profile])


def claude_tools(perms: dict, *, scoped_shell: bool = True) -> list[str]:
    """Grant qmd explicitly; native manifests list tools, CLI grants shell rules."""
    tools = ["Read", "Grep", "Glob", QMD_TOOLS]
    if perms["shell"]:
        if not scoped_shell:
            tools.append("Bash")
        else:
            tools.extend(f"Bash({command} *)" for command in READ_ONLY_SHELL_COMMANDS)
            tools.extend(f"Bash({rule})" for rule in READ_ONLY_SHELL_RULES)
            if perms["write"]:
                tools.extend(f"Bash({command} *)" for command in WRITE_SHELL_COMMANDS)
                tools.extend(f"Bash({rule})" for rule in WRITE_SHELL_RULES)
                if perms.get("python_shell"):
                    tools.append("Bash(python3 *)")
    if perms["write"]:
        tools.extend(["Edit", "Write"])
    return tools


def claude_builtin_tools(perms: dict) -> list[str]:
    """Restrict CLI built-ins; MCP tools are controlled by separate rules.

    Claude's --tools accepts built-in names, not scoped permission rules or
    MCP wildcards. Keep this separate from --allowedTools so imported approval
    settings cannot make extra built-in capabilities available.
    """
    return [
        tool
        for tool in claude_tools(perms, scoped_shell=False)
        if not tool.startswith("mcp__")
    ]


def codex_sandbox(perms: dict) -> str:
    return "workspace-write" if perms["write"] else "read-only"
