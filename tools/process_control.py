"""Bounded cleanup for processes and their local tools."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any, Protocol, TypeGuard


class ProcessCleanupError(RuntimeError):
    """The caller must retain its cancellation gate until cleanup is confirmed."""

    def __init__(self, message: str, *, group_id: int | None = None) -> None:
        super().__init__(message)
        self.group_id = group_id


class SupervisedProcess(Protocol):
    """The Popen surface shared by subprocess.Popen and AuditSessionProcess."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdout(self) -> IO[Any] | None: ...

    @property
    def stderr(self) -> IO[Any] | None: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    # Any mirrors typeshed's Popen.communicate, which is tuple[Any, Any] for str and bytes pipes.
    def communicate(
        self, input: None = None, timeout: float | None = None
    ) -> tuple[Any, Any]: ...


class OwnedProcess(SupervisedProcess, Protocol):
    """A process whose whole lifetime is owned by a macOS audit session."""

    def terminate_owned(self, grace: float = 2.0) -> None: ...


def launch_supervised(
    command: list[str],
    *,
    run: Path,
    interactive: bool = False,
    # Any: options (cwd, env, stdout, stderr, text) are forwarded verbatim and each
    # backend (Popen, AuditSessionProcess) validates them in its own typed signature.
    **options: Any,  # noqa: ANN401
) -> SupervisedProcess:
    """Choose a kernel-backed lifetime owner without changing provider argv."""
    if sys.platform == "darwin":
        from macos_processes import AuditSessionProcess

        return AuditSessionProcess(command, run=run, interactive=interactive, **options)
    if interactive:
        return subprocess.Popen(
            command, start_new_session=False, process_group=0, **options
        )
    return subprocess.Popen(command, start_new_session=True, **options)


def check_process_records(root: Path) -> None:
    """A lost supervisor must be resolved before another vault run starts."""
    directory = root / "tools/runtime-state/processes"
    if not directory.exists() and not directory.is_symlink():
        return
    if sys.platform != "darwin":
        raise ValueError("Unresolved process ownership records require verification")
    from macos_processes import verify_process_records

    verify_process_records(directory)


def signal_group(process: SupervisedProcess, signum: int) -> bool:
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


def _is_audit_owner(process: SupervisedProcess) -> TypeGuard[OwnedProcess]:
    # Duck-typed marker (not isinstance) so test doubles and the lazy import both work.
    return getattr(process, "audit_session_owner", False) is True


def terminate_group(process: SupervisedProcess, grace: float = 2.0) -> None:
    if _is_audit_owner(process):
        process.terminate_owned(grace=grace)
        return
    if not signal_group(process, signal.SIGTERM):
        process.wait()
        return
    time.sleep(grace)
    signal_group(process, signal.SIGKILL)
    process.wait()
