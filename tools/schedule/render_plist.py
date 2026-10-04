#!/usr/bin/env python3
"""Render or validate a scheduler plist without loading launchd or starting work."""

from __future__ import annotations

import argparse
import os
import plistlib
import sys
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_provider import resolve_provider  # noqa: E402


PROVIDER_KEYS = (
    "VAULTLENS_LLM_CLI",
    "VAULTLENS_LLM_MODEL",
    "VAULTLENS_LLM_HEALTH_HOST",
    "VAULTLENS_LLM_IDENTITY",
)


def dispatcher_config_path(data: dict) -> Path:
    """Locate configuration beside the dispatcher that launchd will execute."""
    arguments = data.get("ProgramArguments")
    if (
        not isinstance(arguments, list)
        or len(arguments) < 2
        or any(not isinstance(argument, str) or not argument for argument in arguments)
    ):
        raise ValueError("Scheduler ProgramArguments must include a dispatcher path")
    dispatcher = Path(arguments[1])
    if (
        not dispatcher.is_absolute()
        or dispatcher.parts[-3:] != ("tools", "schedule", "dispatch.py")
        or any(Path(argument).name == "dispatch.py" for argument in arguments[2:])
    ):
        raise ValueError(
            "Scheduler ProgramArguments must name one absolute "
            "tools/schedule/dispatch.py target immediately after the Python executable"
        )
    dispatcher = dispatcher.resolve()
    if dispatcher.parts[-3:] != ("tools", "schedule", "dispatch.py"):
        raise ValueError(
            "Resolved scheduler dispatcher must be tools/schedule/dispatch.py"
        )
    return dispatcher.parent.parent / "llm.local.json"


def validate_plist(data: dict, *, config_path: Path | None = None) -> str:
    if not isinstance(data, dict):
        raise ValueError("Scheduler plist must contain a dictionary")
    env = data.get("EnvironmentVariables", {})
    if not isinstance(env, dict) or any(not isinstance(v, str) for v in env.values()):
        raise ValueError("Scheduler EnvironmentVariables must map names to strings")
    overrides = {key: env[key] for key in PROVIDER_KEYS if key in env}
    path = config_path if config_path is not None else dispatcher_config_path(data)
    provider = resolve_provider(environ=overrides, path=path)
    enhancement = env.get("VAULTLENS_SCHEDULE_ENHANCE", "0").strip().lower()
    if enhancement not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError("VAULTLENS_SCHEDULE_ENHANCE must be a boolean flag")
    return provider.cli


def render_plist(
    data: dict,
    overrides: Mapping[str, str],
    *,
    config_path: Path | None = None,
    python_executable: str | None = None,
) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Scheduler plist must contain a dictionary")
    if not isinstance(data.get("EnvironmentVariables", {}), dict):
        raise ValueError("Scheduler EnvironmentVariables must map names to strings")
    result = dict(data)
    if python_executable is not None:
        executable = Path(python_executable)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("Python executable must name an absolute executable file")
        dispatcher_config_path(result)
        result["ProgramArguments"] = [str(executable), *data["ProgramArguments"][1:]]
    env = dict(data.get("EnvironmentVariables", {}))
    for key in PROVIDER_KEYS:
        env.pop(key, None)
        if key in overrides:
            env[key] = overrides[key]
    if "VAULTLENS_SCHEDULE_ENHANCE" in overrides:
        env["VAULTLENS_SCHEDULE_ENHANCE"] = overrides["VAULTLENS_SCHEDULE_ENHANCE"]
    result["EnvironmentVariables"] = env
    validate_plist(result, config_path=config_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument(
        "--python-executable", help="Python executable launchd should use"
    )
    args = parser.parse_args(argv)
    try:
        data = plistlib.loads(args.source.read_bytes())
        if args.validate:
            provider = validate_plist(data)
            print(f"Prepared provider: {provider}")
        else:
            if args.destination is None:
                parser.error("destination is required unless --validate is set")
            result = render_plist(
                data, os.environ, python_executable=args.python_executable
            )
            args.destination.write_bytes(plistlib.dumps(result, sort_keys=False))
            mode = (
                "explicit environment override"
                if any(key in os.environ for key in PROVIDER_KEYS)
                else "shared tools/llm.local.json"
            )
            print(f"Rendered {args.destination} ({mode})")
    except (ValueError, OSError, plistlib.InvalidFileException) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
