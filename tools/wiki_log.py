#!/usr/bin/env python3
"""Operations log: append to and validate `wiki/log.md`.

`wiki/log.md` is the running ledger of ingest/query/maintenance operations.
Entries follow `## [YYYY-MM-DD] <operation> | <title>` so they stay greppable.
This module owns appending new entries (`append-log`) and validating the format
of existing ones (`validate-log`).
"""

from __future__ import annotations

import datetime as dt
import re

from wiki import WIKI_DIR


def append_log_entry(
    operation: str,
    title: str,
    summary: str,
    pages: list[str],
    sources: list[str],
    notes: str,
) -> int:
    date = dt.datetime.now().strftime("%Y-%m-%d")
    log_path = WIKI_DIR / "log.md"
    if not log_path.exists():
        log_path.write_text("# Log\n\n", encoding="utf-8")

    rows = [f"## [{date}] {operation} | {title}", ""]
    rows.append(f"- Summary: {summary}")

    if sources:
        rows.append("- Sources: " + ", ".join(f"`{source}`" for source in sources))
    if pages:
        rendered_pages = []
        for page in pages:
            cleaned = page.strip().replace(".md", "")
            rendered_pages.append(f"[[{cleaned}]]")
        rows.append("- Pages touched: " + ", ".join(rendered_pages))
    if notes:
        rows.append(f"- Notes: {notes}")

    rows.append("")

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n" + "\n".join(rows))

    print(f"Appended log entry to {log_path}")
    return 0


def validate_log() -> int:
    log_path = WIKI_DIR / "log.md"
    if not log_path.exists():
        print("No log.md found")
        return 1

    text = log_path.read_text(encoding="utf-8")
    entry_re = re.compile(r"^## \[\d{4}-\d{2}-\d{2}\] \w+ \| .+$")
    entries = 0
    malformed: list[str] = []

    for i, line in enumerate(text.splitlines(), 1):
        if line.startswith("## ["):
            entries += 1
            if not entry_re.match(line):
                malformed.append(f"line {i}: {line}")
        elif line.startswith("## ") and "[" not in line and i > 3:
            malformed.append(f"line {i}: heading missing date format: {line}")

    print(f"Log entries: {entries}")
    print(f"Malformed entries: {len(malformed)}")
    if malformed:
        print("\nMalformed:")
        for row in malformed:
            print(f"- {row}")
    return 1 if malformed else 0
