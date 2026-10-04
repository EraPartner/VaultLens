#!/usr/bin/env python3
"""Public synthetic per-run OS confinement check; never calls a model."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_runtime import verify_active_boundary  # noqa: E402


def main():
    try:
        verify_active_boundary()
        scratch = Path(os.environ["TMPDIR"]) / "preflight.txt"
        scratch.write_text("PUBLIC_SYNTHETIC_ALLOWED")
        if scratch.read_text() != "PUBLIC_SYNTHETIC_ALLOWED":
            raise ValueError("Scoped scratch is unavailable")
        scratch.unlink()
        print("Whole-process read/write preflight passed")
        return 0
    except (ValueError, OSError) as exc:
        print(f"Confinement failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
