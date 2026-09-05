#!/usr/bin/env python3
"""Shared project lifecycle checks for active-work consumers.

This module stays stdlib-only so the CLI, scheduled dispatcher, agent launcher,
and AGENDA helpers can all use the same frozen-project rule.
"""

from __future__ import annotations

import re
from pathlib import Path


FROZEN_STATUS = "frozen"
PROJECT_STATUSES = ("active", "paused", "frozen", "archived")

_STATUS_LINE = re.compile(r"^status\s*:\s*(.*?)\s*$", re.IGNORECASE)


def project_status(project_dir: str | Path) -> str:
    """Return normalized `project.md` status, or an empty string if unreadable.

    Only the leading frontmatter block is inspected. This intentionally avoids a
    YAML dependency because scheduler code imports this helper before any LLM run.
    """
    project_md = Path(project_dir) / "project.md"
    try:
        text = project_md.read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError):
        return ""
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    if end == -1:
        return ""
    for line in text[4:end].splitlines():
        match = _STATUS_LINE.match(line)
        if match:
            return match.group(1).strip().strip("'\"").lower()
    return ""


def is_frozen_project(project_dir: str | Path) -> bool:
    """Whether a project is excluded from active-work surfaces."""
    return project_status(project_dir) == FROZEN_STATUS
