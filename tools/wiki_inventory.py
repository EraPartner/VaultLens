#!/usr/bin/env python3
"""Inventory layer: structured tracking of things the wiki cares about.

Distinct from `raw/` (source text) and `wiki/` (synthesized knowledge), the
inventory tracks durable *intentions and watch-items*: sources you mean to
ingest, open research questions, tasks, things to monitor. These previously
lived as ad-hoc TODO.md lines; here each is a first-class record under
`wiki/inventory/<kind>/<slug>.md` with a status and priority, so they can be
listed, filtered, and linked from wiki pages.
"""

from __future__ import annotations

import datetime as dt
import json

from wiki import WIKI_DIR, load_page

INVENTORY_DIR = WIKI_DIR / "inventory"

# `entity` is intentionally omitted — Brain tracks entities as wiki/entities/ pages.
KINDS = {
    "item",
    "ingest-candidate",
    "question",
    "task",
    "watch",
    "corpus",
    "artifact",
}
STATUSES = {"proposed", "active", "blocked", "ingested", "superseded", "archived"}
PRIORITIES = {"p0", "p1", "p2", "p3", "p4"}

RECORD_TEMPLATE = """\
---
title: {title}
type: inventory
kind: {kind}
status: {status}
priority: {priority}
created: {today}
updated: {today}
summary: {summary}
tags: []
sources: []
---

# {title}

## Why this record exists

{summary}

## Next actions

-

## Notes

"""


def _records() -> list:
    if not INVENTORY_DIR.exists():
        return []
    return [load_page(p) for p in sorted(INVENTORY_DIR.rglob("*.md")) if p.name != "_index.md"]


def _kind_of(page) -> str:
    value = page.frontmatter.get("kind", "")
    return value if isinstance(value, str) else ""


def _priority_of(page) -> str:
    value = page.frontmatter.get("priority", "")
    return value if isinstance(value, str) else ""


def inventory_new(
    kind: str, slug: str, title: str, status: str, priority: str, summary: str
) -> int:
    if kind not in KINDS:
        print(f"Unknown kind {kind!r}. Choose from: {', '.join(sorted(KINDS))}")
        return 1
    if status not in STATUSES:
        print(f"Unknown status {status!r}. Choose from: {', '.join(sorted(STATUSES))}")
        return 1
    if priority not in PRIORITIES:
        print(f"Unknown priority {priority!r}. Choose from: {', '.join(sorted(PRIORITIES))}")
        return 1
    cleaned = slug.strip().strip("/")
    if not cleaned or "/" in cleaned or cleaned.startswith("."):
        print(f"Invalid slug: {slug!r}")
        return 1

    kind_dir = INVENTORY_DIR / kind
    kind_dir.mkdir(parents=True, exist_ok=True)
    record_path = kind_dir / f"{cleaned}.md"
    if record_path.exists():
        print(f"Record already exists: {record_path.relative_to(WIKI_DIR.parent)}")
        return 1

    from wiki import slug_to_title

    record_path.write_text(
        RECORD_TEMPLATE.format(
            title=title or slug_to_title(cleaned),
            kind=kind,
            status=status,
            priority=priority,
            today=dt.date.today().isoformat(),
            summary=summary or "(describe why this is tracked)",
        ),
        encoding="utf-8",
    )
    print(f"Created inventory/{kind}/{cleaned}.md ({status}, {priority}).")
    return 0


def inventory_list(kind: str, status: str, as_json: bool) -> int:
    records = _records()
    if kind:
        records = [r for r in records if _kind_of(r) == kind]
    if status:
        records = [r for r in records if r.status == status]

    records.sort(key=lambda r: (_priority_of(r), _kind_of(r), r.rel.as_posix()))

    if as_json:
        print(
            json.dumps(
                [
                    {
                        "path": r.rel.as_posix(),
                        "title": r.title,
                        "kind": _kind_of(r),
                        "status": r.status,
                        "priority": _priority_of(r),
                        "summary": r.summary,
                        "tags": r.tags,
                    }
                    for r in records
                ],
                indent=2,
            )
        )
        return 0

    if not records:
        print("No inventory records match.")
        print("Create one with: python3 tools/wiki.py inventory new <kind> <slug>")
        return 0
    print(f"Inventory records ({len(records)}):\n")
    for r in records:
        pr = _priority_of(r) or "--"
        print(f"  [{pr}] {_kind_of(r):<16} {r.status:<10} {r.rel.as_posix()}")
        if r.summary:
            print(f"        {r.summary[:96]}")
    return 0


def inventory_show(ref: str, as_json: bool) -> int:
    path = INVENTORY_DIR / (ref if ref.endswith(".md") else f"{ref}.md")
    if not path.exists():
        print(f"Inventory record not found: {ref}")
        return 1
    record = load_page(path)
    if as_json:
        print(
            json.dumps(
                {
                    "path": record.rel.as_posix(),
                    "title": record.title,
                    "kind": _kind_of(record),
                    "status": record.status,
                    "priority": _priority_of(record),
                    "summary": record.summary,
                    "tags": record.tags,
                },
                indent=2,
            )
        )
        return 0
    print(record.path.read_text(encoding="utf-8"))
    return 0


def cmd_inventory(
    action: str,
    kind: str | None,
    slug: str | None,
    title: str,
    status: str,
    priority: str,
    summary: str,
    as_json: bool,
) -> int:
    if action == "list":
        return inventory_list(kind or "", status, as_json)
    if action == "new":
        if not kind or not slug:
            print("Error: inventory new <kind> <slug> requires both")
            return 1
        return inventory_new(
            kind, slug, title, status or "proposed", priority or "p2", summary
        )
    if action == "show":
        if not kind:
            print("Error: inventory show <kind/slug> requires a reference")
            return 1
        return inventory_show(kind, as_json)
    print(f"Unknown inventory action: {action}")
    return 1
