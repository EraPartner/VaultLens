#!/usr/bin/env python3
"""Preview host repairs; --apply writes them with backups and leaves launchd alone."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import plistlib
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT / "tools" / "schedule"))
from render_plist import PROVIDER_KEYS, validate_plist  # noqa: E402

LEGACY_FUNCTIONS = {
    "vaultlens-claude": "brain-claude",
    "vaultlens-codex": "brain-codex",
    "vaultlens-shell": "brain-shell",
}
NATIVE_ALIAS_DESCRIPTION = "Run the native VaultLens access-profile launcher"
NATIVE_HELPER_HEADER = (
    "function __vaultlens_launch --description "
    "'Resolve a VaultLens source root and use its native planner'\n"
)
MANAGED_HELPER_STAMP = re.compile(
    rb"\A# VaultLens native alias helper v1 sha256=([0-9a-f]{64})\n"
)


def _fish_tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def legacy_alias_fallback(name: str, contents: str) -> str | None:
    """Recognize installed legacy wrappers and this tool's exact native aliases.

    A marker in an arbitrary custom function is not permission to replace it.
    The returned literal root retains each alias's existing source routing.
    """
    if name not in LEGACY_FUNCTIONS:
        return None
    try:
        lines = [
            tokens for line in contents.splitlines() if (tokens := _fish_tokens(line))
        ]
    except ValueError:
        return None
    if not lines or lines[-1] != ["end"]:
        return None
    header = lines[0]
    if header != ["function", name] and not (
        len(header) == 4 and header[:3] == ["function", name, "--description"]
    ):
        return None
    provider = name.removeprefix("vaultlens-")
    native_tail = (
        ["shell", "$argv"]
        if provider == "shell"
        else ["agent", "--cli", provider, "$argv"]
    )
    if (
        len(lines) == 3
        and header == ["function", name, "--description", NATIVE_ALIAS_DESCRIPTION]
        and len(lines[1]) == len(native_tail) + 2
        and lines[1][0] == "__vaultlens_launch"
        and lines[1][2:] == native_tail
    ):
        fallback = lines[1][1]
        if Path(fallback).is_absolute() and "$" not in fallback:
            return fallback
        return None
    if (
        len(lines) == 5
        and len(lines[1]) == 4
        and lines[1][:3] == ["set", "-l", "fallback"]
        and lines[2]
        == [
            "set",
            "-q",
            "VAULTLENS_HOME",
            ";",
            "and",
            "set",
            "fallback",
            "$VAULTLENS_HOME",
        ]
        and lines[3]
        == [
            f"__project_{provider}",
            "VaultLens",
            "$fallback",
            "VAULTLENS_PROJECT_ROOT",
            name,
            "$argv",
        ]
    ):
        fallback = lines[1][3]
        if Path(fallback).is_absolute() and "$" not in fallback:
            return fallback
        return None
    if len(lines) == 3:
        invocation = lines[1]
        if len(invocation) == 3 and invocation[0] in {"bash", "sh"}:
            launcher = invocation[1]
            if (
                invocation[2] == "$argv"
                and Path(launcher).is_absolute()
                and launcher.endswith("/.devcontainer/launcher-common.sh")
            ):
                return launcher.removesuffix("/.devcontainer/launcher-common.sh")
    return None


def _fish_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def native_alias(name: str, fallback: str) -> bytes:
    """A source-specific entrypoint forwarding to the shared native planner."""
    provider = name.removeprefix("vaultlens-")
    invocation = "shell" if provider == "shell" else f"agent --cli {provider}"
    return (
        f"function {name} --description '{NATIVE_ALIAS_DESCRIPTION}'\n"
        f"    __vaultlens_launch {_fish_quote(fallback)} {invocation} $argv\nend\n"
    ).encode()


def _native_alias_helper_body() -> bytes:
    return (
        NATIVE_HELPER_HEADER + '    set -l declared "$argv[1]"\n'
        '    set -l fallback "$declared"\n'
        "    set -e argv[1]\n"
        '    set -q VAULTLENS_HOME; and set fallback "$VAULTLENS_HOME"\n'
        '    set -l candidates "$declared" "$fallback"\n'
        '    set -q VAULTLENS_PROJECT_ROOT; and set -a candidates "$VAULTLENS_PROJECT_ROOT"\n'
        "    set -l roots\n"
        "    for candidate in $candidates\n"
        '        if test -d "$candidate"\n'
        '            set -a roots (path resolve -- "$candidate")\n'
        "        end\n"
        "    end\n"
        '    set -l project "$fallback"\n'
        "    set -l current (pwd -P)\n"
        '    while test "$current" != "/" -a "$current" != ""\n'
        '        if contains -- "$current" $roots\n'
        '            set project "$current"\n'
        "            break\n"
        "        end\n"
        '        set current (command dirname "$current")\n'
        "    end\n"
        '    if not test -f "$project/AGENTS.md"; or not test -f "$project/tools/wiki.py"; or \\\n'
        '            not test -f "$project/tools/agents/wiki-agent.py"; or not test -f "$project/tools/brain_launch.py"\n'
        "        printf 'vaultlens: native launcher missing at %s (set VAULTLENS_HOME to override)\\n' \"$project\" >&2\n"
        "        return 1\n"
        "    end\n"
        "    set -l python /opt/homebrew/bin/python3\n"
        "    set -q BRAIN_PYTHON; and set python $BRAIN_PYTHON\n"
        '    if not test -x "$python"\n'
        "        set python (command -s python3)\n"
        "    end\n"
        '    "$python" "$project/tools/brain_launch.py" --execute --root "$project" $argv\n'
        "end\n"
    ).encode()


def native_alias_helper(source: Path | None = None) -> bytes:
    """Generate a managed helper whose roots come only from alias/user choices.

    ``source`` remains accepted for compatibility and never affects routing.
    The content stamp distinguishes an unchanged generated helper from a custom
    edit, so later tool versions can offer a reviewable helper replacement.
    """
    body = _native_alias_helper_body()
    digest = hashlib.sha256(body).hexdigest()
    return f"# VaultLens native alias helper v1 sha256={digest}\n".encode() + body


def managed_alias_helper(payload: bytes) -> bool:
    """Recognize intact stamped helpers and the exact first migration template."""
    stamp = MANAGED_HELPER_STAMP.match(payload)
    if stamp is not None:
        body = payload[stamp.end() :]
        return body.startswith(NATIVE_HELPER_HEADER.encode()) and hashlib.sha256(
            body
        ).hexdigest().encode() == stamp.group(1)
    # Before content stamps, the only varying line was the source-root literal.
    # Normalize that line and compare the entire known template. A header or
    # marker alone never authorizes replacement of a custom helper.
    try:
        text = payload.decode()
        candidate_lines = [
            line
            for line in text.splitlines(keepends=True)
            if line.startswith("    set -l candidates ")
        ]
        if len(candidate_lines) != 1:
            return False
        tokens = _fish_tokens(candidate_lines[0])
        if (
            len(tokens) != 5
            or tokens[:3] != ["set", "-l", "candidates"]
            or not Path(tokens[3]).is_absolute()
            or "$" in tokens[3]
            or tokens[4] != "$fallback"
        ):
            return False
    except (UnicodeError, ValueError):
        return False
    normalized = text.replace(
        candidate_lines[0], '    set -l candidates __managed_source__ "$fallback"\n'
    )
    original = (
        _native_alias_helper_body()
        .decode()
        .replace(
            '    set -l declared "$argv[1]"\n    set -l fallback "$declared"\n',
            '    set -l fallback "$argv[1]"\n',
        )
        .replace(
            '    set -l candidates "$declared" "$fallback"\n',
            '    set -l candidates __managed_source__ "$fallback"\n',
        )
    )
    return normalized == original


@dataclass(frozen=True)
class Change:
    path: Path
    before: bytes | None
    after: bytes


def change(path: Path, after: bytes) -> Change | None:
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    try:
        before = path.read_bytes()
    except FileNotFoundError:
        before = None
    if before == after:
        return None
    return Change(path, before, after)


def plan_repairs(
    vault: Path, source: Path, functions: Path, scheduler_plist: Path
) -> list[Change]:
    changes: list[Change] = []

    def add(path: Path, after: bytes) -> None:
        proposed = change(path, after)
        if proposed is not None:
            changes.append(proposed)

    for template in sorted((source / "tools" / "shell").glob("*.fish")):
        add(functions / template.name, template.read_bytes())

    # Keep already-installed public entrypoints usable when retiring their old
    # launcher. An unrelated custom definition is outside this migration.
    managed_alias = False
    for legacy in LEGACY_FUNCTIONS:
        path = functions / f"{legacy}.fish"
        if (source / "tools/shell" / path.name).is_file() or not path.exists():
            continue
        if path.is_symlink():
            raise ValueError(f"Existing launcher alias needs manual review: {path}")
        contents = path.read_text(encoding="utf-8")
        fallback = legacy_alias_fallback(legacy, contents)
        if fallback is not None:
            add(path, native_alias(legacy, fallback))
            managed_alias = True
    if managed_alias:
        helper = functions / "__vaultlens_launch.fish"
        payload = native_alias_helper(source)
        if helper.exists() and not managed_alias_helper(helper.read_bytes()):
            raise ValueError(
                f"Existing VaultLens root helper needs manual review: {helper}"
            )
        add(helper, payload)

    instruction_dirs = [vault, vault / "wiki", vault / "projects"]
    instruction_dirs.extend(
        path.parent
        for path in (vault / "projects").glob("*/project.md")
        if not path.parent.is_symlink()
    )
    for directory in instruction_dirs:
        if not (directory / "AGENTS.md").is_file():
            raise ValueError(
                f"Missing canonical instructions: {directory / 'AGENTS.md'}"
            )

    ignore = vault / ".gitignore"
    ignored = ignore.read_text(encoding="utf-8")
    for entry in (
        "tools/llm.local.json",
        "tools/access.local.json",
        "tools/runtime-state/",
        "tools/runtime-node/node_modules/",
        "tools/host-repair-backups/",
    ):
        if entry not in ignored.splitlines():
            ignored = ignored.rstrip() + f"\n{entry}\n"
    add(ignore, ignored.encode())

    if scheduler_plist.exists():
        # Host dotfile managers may symlink this plist. Preserve that link and
        # show the real file as the reviewable repair target.
        if scheduler_plist.is_symlink():
            scheduler_plist = scheduler_plist.resolve(strict=True)
        data = plistlib.loads(scheduler_plist.read_bytes())
        env = data.setdefault("EnvironmentVariables", {})
        removed = False
        for key in PROVIDER_KEYS:
            if key in env:
                del env[key]
                removed = True
        validate_plist(data, config_path=vault / "tools" / "llm.local.json")
        if removed:
            add(scheduler_plist, plistlib.dumps(data, sort_keys=False))
    return changes


def _atomic_write(path: Path, payload: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_repairs(changes: list[Change], backup_directory: Path) -> None:
    # Refuse stale plans before the first change or backup.
    for proposed in changes:
        current = proposed.path.read_bytes() if proposed.path.exists() else None
        if proposed.path.is_symlink() or current != proposed.before:
            raise ValueError(f"Target changed since preview: {proposed.path}")
    backup_directory.mkdir(parents=True, exist_ok=False)
    records: list[str] = []
    modes: list[int] = []
    for index, proposed in enumerate(changes):
        modes.append(
            proposed.path.stat().st_mode & 0o777
            if proposed.before is not None
            else 0o644
        )
        saved = backup_directory / str(index)
        if proposed.before is not None:
            saved.write_bytes(proposed.before)
        records.append(
            f"{index}\t{proposed.path}\t{'new' if proposed.before is None else 'saved'}"
        )
    (backup_directory / "manifest.txt").write_text("\n".join(records) + "\n")
    applied: list[tuple[Change, int]] = []
    try:
        for proposed, mode in zip(changes, modes):
            _atomic_write(proposed.path, proposed.after, mode)
            applied.append((proposed, mode))
    except BaseException:
        for proposed, mode in reversed(applied):
            if proposed.before is None:
                proposed.path.unlink(missing_ok=True)
            else:
                _atomic_write(proposed.path, proposed.before, mode)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=SOURCE_ROOT)
    parser.add_argument(
        "--functions", type=Path, default=Path.home() / ".config/fish/functions"
    )
    parser.add_argument(
        "--plist",
        type=Path,
        default=Path.home() / "Library/LaunchAgents/com.brain.schedule.plist",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--diff", action="store_true")
    args = parser.parse_args(argv)
    try:
        changes = plan_repairs(args.vault, args.source, args.functions, args.plist)
        for proposed in changes:
            print(
                f"{'Update' if proposed.before is not None else 'Create'}: {proposed.path}"
            )
            if args.diff:
                print(
                    "".join(
                        difflib.unified_diff(
                            (proposed.before or b"").decode().splitlines(keepends=True),
                            proposed.after.decode().splitlines(keepends=True),
                            fromfile=str(proposed.path),
                            tofile=str(proposed.path),
                        )
                    ),
                    end="",
                )
        if args.apply and changes:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backups = args.vault / "tools/host-repair-backups" / stamp
            apply_repairs(changes, backups)
            print(f"Applied {len(changes)} repairs; originals saved under {backups}")
        elif not args.apply:
            print(f"Preview only: {len(changes)} repairs; pass --apply to write them.")
        print("No native agent or launchd job was started, stopped, or enabled.")
    except (ValueError, OSError, plistlib.InvalidFileException) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
