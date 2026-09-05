"""Read approved inbox previews without following links into consent queues."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from pathlib import Path


def read_inbox_preview(root: Path, path: Path) -> str | None:
    """Return approved text, or None for unreadable/nonregular/linked entries.

    Resolve each directory using no-follow file descriptors. Checking only the
    leaf link would miss a linked inbox directory; checking before ordinary open
    would also leave a link-swap race. The configured root is the trust anchor.
    """
    if path.parent != root / "raw/inbox" or path.name in (".", ".."):
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY
    try:
        with ExitStack() as stack:
            root_fd = os.open(root, flags)
            stack.callback(os.close, root_fd)
            raw_fd = os.open("raw", flags | os.O_NOFOLLOW, dir_fd=root_fd)
            stack.callback(os.close, raw_fd)
            inbox_fd = os.open("inbox", flags | os.O_NOFOLLOW, dir_fd=raw_fd)
            stack.callback(os.close, inbox_fd)
            leaf_fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=inbox_fd
            )
            if not stat.S_ISREG(os.fstat(leaf_fd).st_mode):
                os.close(leaf_fd)
                return None
            with os.fdopen(leaf_fd, encoding="utf-8") as source:
                return source.read()
    except (OSError, UnicodeError):
        return None
