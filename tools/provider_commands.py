"""Native provider commands, separate from access policy and runtime isolation.

The runtime must supply an isolated HOME and provider state directory before
executing these commands. CLI permissions restrict available tools; they do not
enforce the filesystem or network boundary. Models and effort are opaque inputs.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from agent_capabilities import (
    Capabilities,
    claude_builtin_tools,
    claude_tools,
    codex_sandbox,
)


@dataclass(frozen=True)
class ProviderCommandRequest:
    model: str
    effort: str | None
    role_prompt: str
    task_prompt: str
    cwd: Path
    shell: bool
    write: bool
    python_shell: bool = False
    interactive: bool = False
    writable_roots: tuple[Path, ...] = ()
    mcp_config: Path | None = None
    web_search: bool = False
    network_access: bool = False
    # Set only for execution inside a verified whole-process runtime. This
    # delegates OS isolation; the outer runtime still owns every access grant.
    os_isolation_delegated: bool = False

    def __post_init__(self) -> None:
        if type(self.os_isolation_delegated) is not bool:
            raise ValueError("OS isolation delegation must be an explicit boolean")
        object.__setattr__(self, "cwd", Path(self.cwd))
        object.__setattr__(
            self, "writable_roots", tuple(Path(p) for p in self.writable_roots)
        )
        if self.mcp_config is not None:
            object.__setattr__(self, "mcp_config", Path(self.mcp_config))
        paths = (self.cwd, *self.writable_roots)
        if self.mcp_config is not None:
            paths += (self.mcp_config,)
        if any(not path.is_absolute() for path in paths):
            raise ValueError("Provider command paths must be absolute")

    def permissions(self) -> Capabilities:
        """A fresh legacy capability mapping for existing tool grant helpers."""
        return {
            "shell": self.shell,
            "write": self.write,
            "python_shell": self.python_shell,
        }


class ProviderAdapter(Protocol):
    name: str

    def build_command(
        self, request: ProviderCommandRequest, *, executable: str
    ) -> list[str]: ...


_ADAPTERS: dict[str, ProviderAdapter] = {}


def register_provider_adapter(adapter: ProviderAdapter) -> None:
    """Add a provider without changing the launcher or access profile schema."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", adapter.name):
        raise ValueError(
            "Provider adapter names must use lowercase letters, digits or hyphens"
        )
    if adapter.name in _ADAPTERS:
        raise ValueError(f"Provider adapter already registered: {adapter.name}")
    _ADAPTERS[adapter.name] = adapter


def provider_names() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def build_provider_command(
    cli: str,
    request: ProviderCommandRequest,
    *,
    executable: str | None = None,
) -> list[str]:
    try:
        adapter = _ADAPTERS[cli]
    except KeyError as exc:
        raise ValueError(f"Unsupported CLI: {cli}") from exc
    return adapter.build_command(request, executable=executable or cli)


def _scoped_mcp_servers(path: Path | None) -> dict[str, dict[str, object]]:
    """Only accept explicitly prepared local stdio servers, never ambient MCP."""
    if path is None:
        return {}
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read scoped MCP configuration {path}: {exc}") from exc
    # JSON object keys are always str, so the narrowed dict is dict[str, object].
    data = cast("dict[str, object]", loaded) if isinstance(loaded, dict) else None
    if data is None or set(data) != {"mcpServers"}:
        raise ValueError("Scoped MCP configuration must contain only mcpServers")
    raw_servers = data["mcpServers"]
    if not isinstance(raw_servers, dict):
        raise ValueError("Scoped MCP servers must be an object")
    servers = cast("dict[str, object]", raw_servers)  # JSON keys are always str
    checked: dict[str, dict[str, object]] = {}
    for name, entry in servers.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise ValueError("Scoped MCP server names must be plain identifiers")
        if not isinstance(entry, dict):
            raise ValueError(f"{name}: only local stdio MCP servers are supported")
        config = cast("dict[str, object]", entry)  # JSON keys are always str
        if set(config) - {
            "type",
            "command",
            "args",
            "cwd",
        }:
            raise ValueError(f"{name}: only local stdio MCP servers are supported")
        if config.get("type", "stdio") != "stdio":
            raise ValueError(f"{name}: scoped MCP server must use stdio")
        command = config.get("command")
        args = config.get("args", [])
        if not isinstance(command, str) or not Path(command).is_absolute():
            raise ValueError(f"{name}: MCP command must be an absolute path")
        if not isinstance(args, list) or any(
            not isinstance(arg, str) for arg in cast("list[object]", args)
        ):
            raise ValueError(f"{name}: MCP arguments must be strings")
        cwd = config.get("cwd")
        if "cwd" in config and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
            raise ValueError(f"{name}: MCP working directory must be an absolute path")
        checked[name] = config
    return checked


def _toml(value: object) -> str:
    """Encode inline CLI overrides without confusing JSON with TOML syntax."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or isinstance(value, float) and math.isfinite(value):
        return str(value)
    if isinstance(value, (list, tuple)):
        items = cast("Sequence[object]", value)  # element types are checked per item
        return "[" + ",".join(_toml(item) for item in items) + "]"
    if isinstance(value, dict):
        table = cast("dict[object, object]", value)  # keys and values checked per item
        return (
            "{"
            + ",".join(f"{_toml(key)}={_toml(item)}" for key, item in table.items())
            + "}"
        )
    raise ValueError(f"Unsupported TOML override type: {type(value).__name__}")


class ClaudeAdapter:
    name = "claude"

    def build_command(
        self, request: ProviderCommandRequest, *, executable: str
    ) -> list[str]:
        _scoped_mcp_servers(request.mcp_config)
        command = [executable]
        if not request.interactive:
            command.append("-p")
        if request.model:
            command.extend(["--model", request.model])
        if request.effort:
            command.extend(["--effort", request.effort])
        if not request.interactive:
            command.extend(["--no-session-persistence", "--permission-prompts", "none"])
        permissions = request.permissions()
        builtin_tools = claude_builtin_tools(permissions)
        allowed_tools = claude_tools(permissions)
        if request.web_search:
            builtin_tools.extend(["WebSearch", "WebFetch"])
            allowed_tools.extend(["WebSearch", "WebFetch"])
        command.extend(
            [
                "--setting-sources",
                "",
                "--settings",
                '{"disableAllHooks":true}',
                "--strict-mcp-config",
                "--mcp-config",
                str(request.mcp_config) if request.mcp_config else '{"mcpServers":{}}',
                "--disable-slash-commands",
                "--no-chrome",
                "--system-prompt",
                request.role_prompt,
                "--tools",
                ",".join(builtin_tools),
                "--allowedTools",
                ",".join(allowed_tools),
                "--disallowedTools",
                "Agent,Task",
                "--permission-mode",
                "dontAsk",
            ]
        )
        for root in request.writable_roots:
            command.extend(["--add-dir", str(root)])
        if request.task_prompt:
            # --add-dir is variadic. Separate the positional prompt so it cannot
            # become another directory or be parsed as a CLI option.
            command.extend(["--", request.task_prompt])
        return command


class CodexAdapter:
    name = "codex"

    def build_command(
        self, request: ProviderCommandRequest, *, executable: str
    ) -> list[str]:
        servers = _scoped_mcp_servers(request.mcp_config)
        command = [executable]
        if request.interactive:
            # The TUI has no --ephemeral or --ignore-user-config flag. The runtime
            # supplies a clean disposable CODEX_HOME; never attach its daemon.
            command.append("--no-daemon")
        else:
            command.extend(
                [
                    "exec",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--color",
                    "never",
                    "--skip-git-repo-check",
                ]
            )
        # Codex cannot apply macOS Seatbelt again inside SRT. Its CLI has no
        # external-sandbox mode, so a verified outer runtime owns OS isolation
        # for delegated runs. Approval and tool restrictions remain below.
        sandbox = (
            "danger-full-access"
            if request.os_isolation_delegated
            else codex_sandbox(request.permissions())
        )
        command.extend(["-C", str(request.cwd), "--sandbox", sandbox])
        overrides: dict[str, object] = {
            "approval_policy": "never",
            "agents.enabled": False,
            "features.multi_agent": False,
            "features.hooks": False,
            "features.memories": False,
            "features.remote_plugin": False,
            "features.skill_mcp_dependency_install": False,
            "features.shell_snapshot": False,
            "allow_login_shell": False,
            "project_doc_max_bytes": 0,
            "project_root_markers": [],
            "history.persistence": "none",
            "cli_auth_credentials_store": "file",
            "check_for_update_on_startup": False,
            "web_search": "live" if request.web_search else "disabled",
            # The verified runtime's interactive cwd is an immutable generated
            # view with no native config. Trust that view to avoid an onboarding
            # modal that can replace CLI permissions and enable nested sandboxing.
            f"projects.{_toml(str(request.cwd))}.trust_level": (
                "trusted"
                if request.interactive and request.os_isolation_delegated
                else "untrusted"
            ),
            "mcp_servers": {
                name: {
                    **{key: value for key, value in server.items() if key != "type"},
                    "required": True,
                }
                for name, server in servers.items()
            },
        }
        if request.effort:
            overrides["model_reasoning_effort"] = request.effort
        if request.write:
            overrides["sandbox_workspace_write.network_access"] = request.network_access
            overrides["sandbox_workspace_write.writable_roots"] = [
                str(root) for root in request.writable_roots
            ]
            overrides["sandbox_workspace_write.exclude_slash_tmp"] = True
            overrides["sandbox_workspace_write.exclude_tmpdir_env_var"] = True
        for key, value in overrides.items():
            command.extend(["-c", f"{key}={_toml(value)}"])
        if request.model:
            command.extend(["--model", request.model])
        prompt = f"{request.role_prompt}\n\n# Task\n\n{request.task_prompt}"
        command.extend(["--", prompt])
        return command


register_provider_adapter(ClaudeAdapter())
register_provider_adapter(CodexAdapter())
