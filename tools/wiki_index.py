#!/usr/bin/env python3
"""CLI-renderable derived indexes for the wiki.

Obsidian's Dataview renders `wiki/index.md` only inside the app. Agents running
headless in the devcontainer can't see those tables. This module generates plain
markdown `_index.md` files (one per category + a root summary) that are readable
anywhere — terminal, GitHub, any markdown viewer — and rebuilt on demand.

The indexes are *derived*: they are regenerated from page frontmatter, never
hand-edited. `index --check` (default) reports when an index is stale (its row
count no longer matches the files on disk); `index --rebuild` regenerates them.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from pathlib import Path

from wiki import (
    Page,
    WIKI_DIR,
    list_content_pages,
    parse_frontmatter,
)

INDEX_NAME = "_index.md"
SUMMARY_MAX = 120
# Categories that hold dated artifacts rather than durable pages; still indexed,
# but ordered newest-first instead of alphabetically.
DATED_CATEGORIES = {"queries", "reports", "log"}


def _cell(value: str) -> str:
    """Make a string safe for a markdown table cell (no pipes/newlines)."""
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _truncate(text: str, limit: int = SUMMARY_MAX) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _pages_by_category(pages: list[Page]) -> dict[str, list[Page]]:
    grouped: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        if page.category == "root":
            continue
        grouped[page.category].append(page)
    return grouped


def _sort_pages(category: str, pages: list[Page]) -> list[Page]:
    if category in DATED_CATEGORIES:
        return sorted(pages, key=lambda p: (p.updated, p.rel.as_posix()), reverse=True)
    return sorted(pages, key=lambda p: p.title.lower())


def build_category_index(category: str, pages: list[Page], today: str) -> str:
    """Render a category's `_index.md` body as plain markdown."""
    ordered = _sort_pages(category, pages)
    lines = [
        "---",
        f"title: {category.title()} Index",
        "type: index",
        f"updated: {today}",
        f"page_count: {len(ordered)}",
        "---",
        "",
        f"# {category.title()} Index",
        "",
        f"> Derived index of `wiki/{category}/` — regenerate with "
        "`python3 tools/wiki.py index --rebuild`. Do not hand-edit.",
        "",
        f"{len(ordered)} pages.",
        "",
        "| Page | Summary | Tags | Conf | Updated |",
        "| --- | --- | --- | --- | --- |",
    ]
    for page in ordered:
        link = f"[{_cell(page.title)}]({page.rel.name})"
        summary = _cell(_truncate(page.summary))
        tags = _cell(", ".join(page.tags))
        conf = _cell(page.confidence)
        updated = _cell(page.updated)
        lines.append(f"| {link} | {summary} | {tags} | {conf} | {updated} |")
    lines.append("")
    return "\n".join(lines)


def build_root_index(grouped: dict[str, list[Page]], today: str) -> str:
    """Render the root `wiki/_index.md` summary with per-category counts."""
    total = sum(len(v) for v in grouped.values())
    confidences: dict[str, int] = defaultdict(int)
    for pages in grouped.values():
        for page in pages:
            if page.confidence:
                confidences[page.confidence] += 1

    lines = [
        "---",
        "title: Wiki Index",
        "type: index",
        f"updated: {today}",
        f"page_count: {total}",
        "---",
        "",
        "# Wiki Index",
        "",
        "> Derived headless-readable index — regenerate with "
        "`python3 tools/wiki.py index --rebuild`. Do not hand-edit. "
        "(`index.md` is the Obsidian/Dataview view; this is its CLI mirror.)",
        "",
        f"**{total}** content pages across **{len(grouped)}** categories.",
        "",
        "## Categories",
        "",
        "| Category | Pages | Index |",
        "| --- | --- | --- |",
    ]
    for category in sorted(grouped):
        count = len(grouped[category])
        lines.append(f"| {category} | {count} | [{category}/{INDEX_NAME}]({category}/{INDEX_NAME}) |")
    lines.append("")
    if confidences:
        dist = ", ".join(f"{k}: {confidences[k]}" for k in ("high", "medium", "low") if confidences.get(k))
        lines += ["## Confidence distribution", "", dist, ""]
    return "\n".join(lines)


def _existing_page_count(index_path: Path) -> int | None:
    """Return the `page_count` recorded in an existing index, or None."""
    if not index_path.exists():
        return None
    fm, _ = parse_frontmatter(index_path.read_text(encoding="utf-8"))
    raw = fm.get("page_count")
    if not isinstance(raw, str):
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _stale_categories(grouped: dict[str, list[Page]]) -> list[str]:
    """Categories whose on-disk file count differs from the index's record."""
    stale: list[str] = []
    for category, pages in grouped.items():
        index_path = WIKI_DIR / category / INDEX_NAME
        recorded = _existing_page_count(index_path)
        if recorded is None or recorded != len(pages):
            stale.append(category)
    root_recorded = _existing_page_count(WIKI_DIR / INDEX_NAME)
    if root_recorded is None or root_recorded != sum(len(v) for v in grouped.values()):
        stale.append("(root)")
    return sorted(stale)


def rebuild_indexes() -> int:
    """Regenerate every category `_index.md` plus the root index."""
    pages = list_content_pages()
    grouped = _pages_by_category(pages)
    today = dt.date.today().isoformat()

    written = 0
    for category, cat_pages in grouped.items():
        index_path = WIKI_DIR / category / INDEX_NAME
        index_path.write_text(build_category_index(category, cat_pages, today), encoding="utf-8")
        written += 1

    (WIKI_DIR / INDEX_NAME).write_text(build_root_index(grouped, today), encoding="utf-8")
    written += 1

    print(f"Rebuilt {written} index files ({sum(len(v) for v in grouped.values())} pages).")
    return 0


def check_indexes() -> int:
    """Report stale indexes without writing. Nonzero exit when any are stale."""
    pages = list_content_pages()
    grouped = _pages_by_category(pages)
    stale = _stale_categories(grouped)
    if not stale:
        print(f"All indexes current ({len(grouped)} categories).")
        return 0
    print(f"Stale indexes ({len(stale)}): {', '.join(stale)}")
    print("Run: python3 tools/wiki.py index --rebuild")
    return 1


def cmd_index(rebuild: bool) -> int:
    return rebuild_indexes() if rebuild else check_indexes()
