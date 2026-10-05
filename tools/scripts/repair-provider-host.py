#!/usr/bin/env python3
"""Preview host repairs; --apply writes them with backups and leaves launchd alone."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import plistlib
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT / "tools" / "schedule"))
from render_plist import PROVIDER_KEYS, validate_plist  # noqa: E402


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
