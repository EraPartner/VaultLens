"""Coordinate host maintenance with every scoped runtime invocation."""

import contextlib
import fcntl
import functools
import os
import stat


@contextlib.contextmanager
def runtime_lock(root, *, exclusive=False):
    state = root / "tools/runtime-state"
    for parent in (state, *state.parents):
        if parent.is_symlink():
            raise ValueError("Runtime maintenance state cannot follow symbolic links")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("Runtime maintenance state must be private")
        os.fchmod(directory, 0o700)
        descriptor = os.open(
            "maintenance.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Runtime maintenance lock must be a private regular file")
        try:
            fcntl.flock(
                descriptor,
                (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise ValueError(
                "An agent or sandbox maintenance is active; retry when it finishes"
            ) from exc
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def shared_runtime(function):
    @functools.wraps(function)
    def guarded(scope, *args, **kwargs):
        with runtime_lock(scope.root):
            yield from function(scope, *args, **kwargs)

    return guarded
