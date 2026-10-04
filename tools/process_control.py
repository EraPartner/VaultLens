"""Bounded cleanup for processes and their local tools."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class ProcessCleanupError(RuntimeError):
    """The caller must retain its cancellation gate until cleanup is confirmed."""

    def __init__(self, message: str, *, group_id: int | None = None):
        super().__init__(message)
        self.group_id = group_id


def launch_supervised(command, *, run: Path, interactive=False, **options):
    """Choose a kernel-backed lifetime owner without changing provider argv."""
    if sys.platform == "darwin":
        from macos_processes import AuditSessionProcess

        return AuditSessionProcess(command, run=run, interactive=interactive, **options)
    return subprocess.Popen(
        command,
        start_new_session=not interactive,
        **({"process_group": 0} if interactive else {}),
        **options,
    )


def check_process_records(root: Path) -> None:
    """A lost supervisor must be resolved before another vault run starts."""
    directory = root / "tools/runtime-state/processes"
    if not directory.exists() and not directory.is_symlink():
        return
    if sys.platform != "darwin":
        raise ValueError("Unresolved process ownership records require verification")
    from macos_processes import verify_process_records

    verify_process_records(directory)


def signal_group(process: subprocess.Popen, signum: int) -> bool:
    try:
        os.killpg(process.pid, signum)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        try:
            groups = subprocess.run(
                ["ps", "-eo", "pgid=,stat="],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            for line in groups.stdout.splitlines():
                pgid, state = line.split()
                if int(pgid) == process.pid and not state.startswith("Z"):
                    raise ProcessCleanupError(
                        f"signal {signum} denied for live process group {process.pid}",
                        group_id=process.pid,
                    ) from exc
        except ProcessCleanupError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as verification:
            raise ProcessCleanupError(
                f"signal {signum} denied; cannot verify process group {process.pid}: {verification}",
                group_id=process.pid,
            ) from verification
        return False


def terminate_group(process: subprocess.Popen, grace: float = 2.0) -> None:
    if getattr(process, "audit_session_owner", False) is True:
        process.terminate_owned(grace=grace)
        return
    if not signal_group(process, signal.SIGTERM):
        process.wait()
        return
    time.sleep(grace)
    signal_group(process, signal.SIGKILL)
    process.wait()
