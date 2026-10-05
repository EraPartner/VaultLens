#!/usr/bin/env python3
"""Render or validate scheduler templates without loading launchd or starting work.

The tracked plist and sudoers files are templates: `@BRAIN_ROOT@` (this checkout),
`@BRAIN_HOME@` (the operator's home) and `@BRAIN_USER@` (the operator's account) are
filled in at render time, so no host-specific path or user name is committed.
"""

from __future__ import annotations

import argparse
import getpass
import os
import plistlib
import re
import sys
from pathlib import Path
from collections.abc import Mapping
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_provider import resolve_provider  # noqa: E402


PROVIDER_KEYS = (
    "VAULTLENS_LLM_CLI",
    "VAULTLENS_LLM_MODEL",
    "VAULTLENS_LLM_HEALTH_HOST",
    "VAULTLENS_LLM_IDENTITY",
)


PlistDict = dict[str, object]

_PLACEHOLDER_RE = re.compile(r"@BRAIN_[A-Z_]+@")
# Portable account-name grammar; also keeps a hostile name out of a sudoers line.
_ACCOUNT_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def default_substitutions() -> dict[str, str]:
    """Placeholder values for this checkout and the account running the renderer."""
    return {
        "@BRAIN_ROOT@": str(Path(__file__).resolve().parents[2]),
        "@BRAIN_HOME@": str(Path.home()),
        "@BRAIN_USER@": getpass.getuser(),
    }


def _fill(value: object, substitutions: Mapping[str, str]) -> object:
    """Replace placeholders in every string of a plist value; reject leftovers."""
    if isinstance(value, str):
        for placeholder, replacement in substitutions.items():
            value = value.replace(placeholder, replacement)
        unresolved = _PLACEHOLDER_RE.search(value)
        if unresolved:
            raise ValueError(f"Unresolved template placeholder {unresolved.group()}")
        return value
    if isinstance(value, list):
        items = cast("list[object]", value)  # plist arrays hold plist values
        return [_fill(item, substitutions) for item in items]
    if isinstance(value, dict):
        table = cast("PlistDict", value)  # plist dictionary keys are always strings
        return {key: _fill(item, substitutions) for key, item in table.items()}
    return value


def render_sudoers(template: str, user: str) -> str:
    """Fill the sudoers template for `user`; refuse names that are not plain accounts."""
    if not _ACCOUNT_RE.fullmatch(user) or user == "root":
        raise ValueError(f"Not a valid non-root account name for sudoers: {user!r}")
    result = template.replace("@BRAIN_USER@", user)
    unresolved = _PLACEHOLDER_RE.search(result)
    if unresolved:
        raise ValueError(f"Unresolved template placeholder {unresolved.group()}")
    return result


def _plist_dict(data: object) -> PlistDict:
    if not isinstance(data, dict):
        raise ValueError("Scheduler plist must contain a dictionary")
    return cast("PlistDict", data)  # plist dictionary keys are always strings


def _environment(data: PlistDict) -> PlistDict | None:
    """The EnvironmentVariables table, or None when it is not a dictionary."""
    env = data.get("EnvironmentVariables", {})
    if not isinstance(env, dict):
        return None
    return cast("PlistDict", env)  # plist dictionary keys are always strings


def dispatcher_config_path(data: PlistDict) -> Path:
    """Locate configuration beside the dispatcher that launchd will execute."""
    raw_arguments = data.get("ProgramArguments")
    if not isinstance(raw_arguments, list):
        raise ValueError("Scheduler ProgramArguments must include a dispatcher path")
    items = cast("list[object]", raw_arguments)  # each item is checked below
    arguments = [
        argument for argument in items if isinstance(argument, str) and argument
    ]
    if len(arguments) < 2 or len(arguments) != len(items):
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


def validate_plist(data: object, *, config_path: Path | None = None) -> str:
    plist = _plist_dict(data)
    table = _environment(plist)
    if table is None or any(not isinstance(v, str) for v in table.values()):
        raise ValueError("Scheduler EnvironmentVariables must map names to strings")
    env = cast("dict[str, str]", table)  # every value was checked to be a str
    overrides = {key: env[key] for key in PROVIDER_KEYS if key in env}
    path = config_path if config_path is not None else dispatcher_config_path(plist)
    provider = resolve_provider(environ=overrides, path=path)
    enhancement = env.get("VAULTLENS_SCHEDULE_ENHANCE", "0").strip().lower()
    if enhancement not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError("VAULTLENS_SCHEDULE_ENHANCE must be a boolean flag")
    return provider.cli


def render_plist(
    data: object,
    overrides: Mapping[str, str],
    *,
    config_path: Path | None = None,
    python_executable: str | None = None,
    substitutions: Mapping[str, str] | None = None,
) -> PlistDict:
    plist = _plist_dict(data)
    if substitutions is not None:
        plist = _plist_dict(_fill(plist, substitutions))
    table = _environment(plist)
    if table is None:
        raise ValueError("Scheduler EnvironmentVariables must map names to strings")
    result = dict(plist)
    if python_executable is not None:
        executable = Path(python_executable)
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("Python executable must name an absolute executable file")
        dispatcher_config_path(result)
        # dispatcher_config_path above proved ProgramArguments is a list of strings.
        arguments = cast("list[str]", plist["ProgramArguments"])
        result["ProgramArguments"] = [str(executable), *arguments[1:]]
    env: PlistDict = dict(table)
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
        "--sudoers",
        action="store_true",
        help="render the sudoers template for the current account instead of a plist",
    )
    parser.add_argument(
        "--python-executable", help="Python executable launchd should use"
    )
    args = parser.parse_args(argv)
    try:
        if args.sudoers:
            if args.validate or args.destination is None:
                parser.error("--sudoers needs a destination and excludes --validate")
            text = render_sudoers(
                args.source.read_text(encoding="utf-8"), getpass.getuser()
            )
            # The operator installs this file as root. Refuse an existing path or
            # symlink so another account cannot pre-create or redirect it.
            descriptor = os.open(
                args.destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"Rendered {args.destination} for {getpass.getuser()}")
            return 0
        data = plistlib.loads(args.source.read_bytes())
        if args.validate:
            provider = validate_plist(data)
            print(f"Prepared provider: {provider}")
        else:
            if args.destination is None:
                parser.error("destination is required unless --validate is set")
            result = render_plist(
                data,
                os.environ,
                python_executable=args.python_executable,
                substitutions=default_substitutions(),
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
