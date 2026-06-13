#!/usr/bin/env python3
"""Archive lifecycle for wiki pages.

Brain uses path-based wikilinks, so physically moving a page to an `.archive/`
directory would break every link to it. Instead, archival is driven by the
existing `status: archived` frontmatter field plus a registry that records when
and why each page was archived. Archived pages stay on disk (links keep
resolving) but are excluded from staleness, orphan, and search results by
default — `lint`/`search` keep resolving links to them.
"""

from __future__ import annotations

import datetime as dt
import json

from wiki import (
    WIKI_DIR,
    _set_frontmatter_field,
    build_page_indexes,
    list_content_pages,
    normalize_link_target,
)

REGISTRY_PATH = WIKI_DIR / "system" / "archive-registry.json"


def _load_registry() -> dict[str, dict[str, str]]:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    archived = data.get("archived", {})
    return archived if isinstance(archived, dict) else {}


def _save_registry(archived: dict[str, dict[str, str]]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"archived": dict(sorted(archived.items()))}
    REGISTRY_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _resolve_key(ref: str):
    """Resolve a page reference (e.g. concepts/foo) to its Page, or None."""
    target = normalize_link_target(ref)
    pages = list_content_pages()
    canonical, basename_map = build_page_indexes(pages)
    if target in canonical:
        return canonical[target]
    if "/" not in target and len(basename_map.get(target, [])) == 1:
        return basename_map[target][0]
    return None


def _set_status(page, status: str) -> None:
    today = dt.date.today().isoformat()
    text = page.path.read_text(encoding="utf-8")
    text = _set_frontmatter_field(text, "status", status)
    text = _set_frontmatter_field(text, "updated", today)
    page.path.write_text(text, encoding="utf-8")


def archive_page(ref: str, reason: str) -> int:
    page = _resolve_key(ref)
    if page is None:
        print(f"Page not found: {ref!r}")
        return 1
    key = page.rel.with_suffix("").as_posix()
    if page.is_archived:
        print(f"Already archived: {key}")
        return 0
    _set_status(page, "archived")
    registry = _load_registry()
    registry[key] = {"archived_on": dt.date.today().isoformat(), "reason": reason or ""}
    _save_registry(registry)
    print(f"Archived {key} (status: archived, registry updated).")
    print("Run `python3 tools/wiki.py index --rebuild` to refresh indexes.")
    return 0


def restore_page(ref: str) -> int:
    page = _resolve_key(ref)
    if page is None:
        print(f"Page not found: {ref!r}")
        return 1
    key = page.rel.with_suffix("").as_posix()
    _set_status(page, "active")
    registry = _load_registry()
    registry.pop(key, None)
    _save_registry(registry)
    print(f"Restored {key} (status: active, removed from registry).")
    print("Run `python3 tools/wiki.py index --rebuild` to refresh indexes.")
    return 0


def list_archived(as_json: bool) -> int:
    registry = _load_registry()
    # Reconcile with on-disk status so the registry can't silently drift.
    on_disk = {
        page.rel.with_suffix("").as_posix()
        for page in list_content_pages()
        if page.is_archived
    }
    rows = []
    for key in sorted(set(registry) | on_disk):
        entry = registry.get(key, {})
        rows.append(
            {
                "page": key,
                "archived_on": entry.get("archived_on", ""),
                "reason": entry.get("reason", ""),
                "in_registry": key in registry,
                "status_archived": key in on_disk,
            }
        )

    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No archived pages.")
        return 0
    print(f"Archived pages ({len(rows)}):\n")
    for row in rows:
        drift = "" if row["in_registry"] and row["status_archived"] else "  [DRIFT]"
        reason = f" — {row['reason']}" if row["reason"] else ""
        print(f"  {row['page']:<50} {row['archived_on']}{reason}{drift}")
    return 0


def cmd_archive(action: str, ref: str | None, reason: str, as_json: bool) -> int:
    if action == "list":
        return list_archived(as_json)
    if action == "page":
        if not ref:
            print("Error: archive page <ref> requires a page reference")
            return 1
        return archive_page(ref, reason)
    if action == "restore":
        if not ref:
            print("Error: archive restore <ref> requires a page reference")
            return 1
        return restore_page(ref)
    print(f"Unknown archive action: {action}")
    return 1
