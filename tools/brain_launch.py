#!/usr/bin/env python3
"""Plan and run Brain commands through the local access-profile runtime."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from llm_provider import ROOT, load_config, resolve_provider
from agent_profiles import AGENT_FILES, resolve_role_settings


AGENT_NAMES = set(AGENT_FILES)


@dataclass(frozen=True)
class Launch:
    profile: str
    write_path: str
    scan_scope: str
    provider: str
    command: list[str]
    project: str | None = None
    read_paths: tuple[str, ...] = ()

    def fields(self) -> list[str]:
        return [
            f"profile={self.profile}",
            f"write_path={self.write_path}",
            f"scan_scope={self.scan_scope}",
            f"provider={self.provider}",
            *self.command,
        ]


def _provider_args(args: list[str]) -> tuple[str | None, list[str]]:
    """Consume both flag forms so the runtime receives one explicit CLI."""
    cli: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            remaining.extend(args[index:])
            break
        if value == "--cli":
            index += 1
            if index >= len(args):
                raise ValueError("--cli requires claude or codex")
            cli = args[index]
        elif value.startswith("--cli="):
            cli = value.partition("=")[2]
        else:
            remaining.append(value)
        index += 1
    return cli, remaining


def _project_slug(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or any(character in value for character in "/\\:\n\r")
    ):
        raise ValueError("a project requires one valid --project slug")
    return value


def _active_project(root: Path, cwd: Path) -> str | None:
    try:
        parts = cwd.resolve().relative_to(root.resolve()).parts
    except ValueError:
        return None
    if len(parts) >= 2 and parts[0] == "projects":
        slug = _project_slug(parts[1])
        if (root / "projects" / slug / "project.md").is_file():
            return slug
    return None


def _access_args(
    args: list[str],
) -> tuple[str | None, str | None, tuple[str, ...], list[str]]:
    """Keep launcher policy options out of the native provider argument list."""
    values: dict[str, str] = {}
    read_paths: list[str] = []
    remaining: list[str] = []
    index = 0
    flags = {"--access-profile", "--project", "--read-path"}
    while index < len(args):
        value = args[index]
        if value == "--":
            remaining.extend(args[index:])
            break
        flag, separator, attached = value.partition("=")
        if flag in flags:
            if separator:
                argument = attached
            else:
                index += 1
                if index >= len(args):
                    raise ValueError(f"{flag} requires a value")
                argument = args[index]
            if not argument:
                raise ValueError(f"{flag} requires a nonempty value")
            if flag == "--read-path":
                read_paths.append(argument)
            else:
                values[flag] = argument
        else:
            remaining.append(value)
        index += 1
    project = values.get("--project")
    if project is not None:
        project = _project_slug(project)
    return values.get("--access-profile"), project, tuple(read_paths), remaining


def _flag_value(args: list[str], flag: str) -> str | None:
    result = None
    for index, value in enumerate(args):
        if value == "--":
            break
        if value == flag:
            if index + 1 >= len(args):
                raise ValueError(f"{flag} requires a value")
            result = args[index + 1]
        elif value.startswith(flag + "="):
            result = value.partition("=")[2]
    return result


def _has_native_model(args: list[str], cli: str) -> bool:
    if _flag_value(args, "--model") is not None:
        return True
    if cli == "codex":
        for value in args:
            if value == "--":
                break
            if value == "-m" or value.startswith("-m") and len(value) > 2:
                return True
    return False


def plan_launch(
    mode: str,
    args: list[str],
    *,
    root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Launch:
    env = os.environ if environ is None else environ
    config = root / "tools" / "llm.local.json"
    if mode == "cos":
        mode, args = "wiki", ["cos", *args]
    if mode in {"agent", "shell"}:
        profile, project, read_paths, remaining = _access_args(args)
        project = project or _active_project(root, cwd or Path.cwd())
        profile = profile or ("project-write" if project else "wiki-read")
        write_path = (
            f"projects/{project}"
            if profile == "project-write" and project
            else "wiki"
            if profile == "wiki-write"
            else ""
        )
        if mode == "shell":
            return Launch(
                profile,
                write_path,
                "",
                "",
                ["/bin/zsh", "-f", *remaining],
                project,
                read_paths,
            )
        override, remaining = _provider_args(remaining)
        provider = resolve_provider(override, path=config, environ=env)
        # Interactive sessions keep their native model unless the operator pins one.
        saved = load_config(config).get("models", {})
        if (
            not _has_native_model(remaining, provider.cli)
            and ("VAULTLENS_LLM_MODEL" in env or provider.cli in saved)
            and provider.model
        ):
            remaining = ["--model", provider.model, *remaining]
        return Launch(
            profile,
            write_path,
            "",
            provider.cli,
            [provider.cli, *remaining],
            project,
            read_paths,
        )
    if mode != "wiki":
        raise ValueError(f"Unknown launch mode: {mode}")

    command = args[0] if args else ""
    profile, write_path, scan_scope, selected = "", "", "", ""
    if command in AGENT_NAMES:
        from local_runtime import default_access_profile

        profile = default_access_profile(command, root=root)
        override, remaining = _provider_args(args)
        explicit_model = _flag_value(remaining, "--model")
        provider, effort = resolve_role_settings(
            command,
            override,
            explicit_model,
            _flag_value(remaining, "--effort"),
            root=root,
            environ=env,
        )
        selected = provider.cli
        if command == "project-run":
            slug = _flag_value(remaining, "--project")
            _project_slug(slug or "")
            write_path = f"projects/{slug}"
        elif command in {"contradict", "emerge", "discover"}:
            scan_scope = "none"
        # Preserve an explicit model; otherwise freeze this invocation's resolved model.
        if explicit_model is None:
            remaining.extend(["--model", provider.model])
        if _flag_value(remaining, "--effort") is None:
            remaining.extend(["--effort", effort])
        remaining[1:1] = ["--cli", provider.cli]
        child = ["python3", "tools/agents/wiki-agent.py", *remaining]
    else:
        # These are explicit deterministic operator commands. They do not invoke
        # a model, so they do not need provider authentication or agent sandboxing.
        child = ["python3", "tools/wiki.py", *args]
    return Launch(profile, write_path, scan_scope, selected, child)


def execute_launch(launch: Launch, *, root: Path = ROOT) -> int:
    if launch.command[0] in {"claude", "codex"}:
        from local_runtime import launch_interactive

        return launch_interactive(
            root,
            launch.provider,
            launch.command[1:],
            profile=launch.profile,
            project=launch.project,
            read_paths=launch.read_paths,
        )
    if launch.command[0] == "/bin/zsh":
        command = [
            sys.executable,
            str(root / "tools" / "local_runtime.py"),
            "exec",
            "--root",
            str(root),
            "--profile",
            launch.profile,
        ]
        if launch.project:
            command.extend(["--project", launch.project])
        for path in launch.read_paths:
            command.extend(["--read-path", path])
        command.extend(["--", *launch.command])
    else:
        command = [sys.executable, str(root / launch.command[1]), *launch.command[2:]]
    return subprocess.run(command, cwd=root, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    effective = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="run the resolved launch"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("mode", choices=["wiki", "cos", "agent", "shell"])
    # Parse only our prefix. Keep the native remainder byte-for-byte, including
    # a leading -- and empty model arguments.
    index = 0
    while index < len(effective) and effective[index].startswith("-"):
        value = effective[index]
        index += 2 if value == "--root" else 1
    args = parser.parse_args(effective[: index + 1])
    try:
        root = args.root.resolve()
        launch = plan_launch(args.mode, effective[index + 1 :], root=root)
        if args.execute:
            return execute_launch(launch, root=root)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    sys.stdout.buffer.write("\0".join(launch.fields()).encode() + b"\0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
