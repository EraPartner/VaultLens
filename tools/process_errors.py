"""Error type shared by process cleanup and the audit guardian.

A leaf module with no VaultLens imports, so the isolated guardian (`macos_processes.py`)
can import it without pulling in `process_control.py`, which lazily imports the guardian.
"""

from __future__ import annotations


class ProcessCleanupError(RuntimeError):
    """The caller must retain its cancellation gate until cleanup is confirmed."""

    def __init__(self, message: str, *, group_id: int | None = None) -> None:
        super().__init__(message)
        self.group_id: int | None = group_id
