#!/usr/bin/env python3
"""Create the fixed VaultLens scaffold without overwriting local data."""

from __future__ import annotations

import datetime as dt
from pathlib import Path


REQUIRED_DIRECTORIES = (
    "raw/sources",
    "raw/sources-text",
    "raw/assets",
    "raw/inbox",
    "raw/review-inbox",
    "wiki/log",
)


def _log_text(today: str) -> str:
    return f"""---
title: Log
type: page
status: active
created: {today}
updated: {today}
summary: Chronological record of wiki operations.
---

# Log

Append-only chronological record of wiki operations.

Format: `## [YYYY-MM-DD] operation | title`
"""


def _index_text() -> str:
    return """# Index

Navigate the wiki from [[home|Home]] or browse the live catalogs below.

## Sources

```dataview
TABLE source_type AS "Type", origin AS "Origin", ingested_on AS "Ingested"
FROM "wiki/sources"
SORT ingested_on DESC
```

## Concepts

```dataview
TABLE summary AS "Summary", updated AS "Updated"
FROM "wiki/concepts"
SORT file.name ASC
```

## Topics and Syntheses

```dataview
TABLE type AS "Type", summary AS "Summary", updated AS "Updated"
FROM "wiki/topics" OR "wiki/syntheses" OR "wiki/comparisons"
SORT file.name ASC
```

## Entities

```dataview
TABLE summary AS "Summary", updated AS "Updated"
FROM "wiki/entities"
SORT file.name ASC
```
"""


def initialize_vault(root: Path, *, today: dt.date | None = None) -> list[Path]:
    """Create missing scaffold paths under *root* and return what was created."""
    created: list[Path] = []
    for relative in REQUIRED_DIRECTORIES:
        path = root / relative
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)

    stamp = (today or dt.date.today()).isoformat()
    files = {
        root / "wiki/log.md": _log_text(stamp),
        root / "wiki/index.md": _index_text(),
    }
    for path, content in files.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)

    return created


def run_init(root: Path) -> int:
    created = initialize_vault(root)
    if created:
        print("Created VaultLens scaffold paths:")
        for path in created:
            print(f"  {path.relative_to(root).as_posix()}")
    else:
        print("VaultLens scaffold is already initialized.")
    return 0
