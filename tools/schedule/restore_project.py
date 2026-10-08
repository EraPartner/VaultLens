#!/usr/bin/env python3
"""Restore a complete project snapshot while preserving the current project."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
import tempfile
import uuid


def restore_project(snapshot: Path, project: Path) -> Path:
    """Stage first, retain current contents, then replace the whole project tree.

    Renames stay on the project's filesystem. Copy failures leave the project
    untouched, and installation failures put its retained tree back in place.
    Symlinks inside the snapshot are copied as links, never traversed.
    """
    if snapshot.is_symlink() or project.is_symlink():
        raise ValueError("Snapshot and project must be directories, not symlinks")
    snapshot = snapshot.resolve(strict=True)
    project = project.resolve(strict=True)
    if not snapshot.is_dir() or not project.is_dir():
        raise ValueError("Snapshot and project must both be existing directories")
    if (
        snapshot == project
        or snapshot in project.parents
        or project in snapshot.parents
    ):
        raise ValueError(
            "Snapshot and project must be separate, non-nested directories"
        )

    backups = project.parent / ".restore-backups"
    if backups.is_symlink():
        raise ValueError("Restore backup directory must not be a symlink")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backups / f"{project.name}-{stamp}-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(
        dir=project.parent, prefix=".restore-"
    ) as temporary:
        staged = Path(temporary) / "project"
        shutil.copytree(snapshot, staged, symlinks=True)
        backups.mkdir(exist_ok=True)
        project.rename(backup)
        try:
            staged.rename(project)
        except BaseException as exc:
            try:
                backup.rename(project)
            except OSError as rollback_error:
                raise OSError(
                    f"Restore installation failed and rollback failed: {rollback_error}. "
                    f"The previous project is preserved at {backup}"
                ) from exc
            raise
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        backup = restore_project(args.snapshot, args.project)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Restored {args.project}; previous contents preserved at {backup}")
    _pause_runner(Path(args.project).name)
    return 0


def _pause_runner(slug: str) -> None:
    """Keep the nightly runner from redoing the work this restore rejected."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        import agenda
    except ImportError:
        print(
            f"Warning: could not pause the nightly runner for {slug}; it may repeat "
            "the restored tasks tonight.",
            file=sys.stderr,
        )
        return
    agenda.pause_for_review(slug)
    print(
        f"Paused the nightly runner for {slug}. Resume it with: "
        f"python3 tools/wiki.py project agenda ack {slug}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
