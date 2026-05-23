#!/usr/bin/env python3
"""Dual-link support: portable markdown mirrors for Obsidian wikilinks.

Brain uses path-based Obsidian wikilinks (`[[concepts/foo]]`). Those resolve in
Obsidian and are followable by agents, but render as dead text in GitHub and
other plain-markdown viewers. A *dual-link* keeps the wikilink and appends a
portable markdown mirror:

    [[concepts/foo]] ([Foo Title](../concepts/foo.md))

The mirror is purely additive — the original `[[...]]` is preserved byte-for-byte,
so Obsidian behaviour, wikilink extraction, and `lint` are unchanged. `links`
reports coverage; `links --fix` previews the rewrite (dry-run); `--fix --write`
persists it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from wiki import (
    Page,
    SPECIAL_LINK_TARGETS,
    build_page_indexes,
    list_content_pages,
    normalize_link_target,
)

# A wikilink with optional #heading and |alias: groups = (target, heading, alias).
import re

LINK_RE = re.compile(r"\[\[([^\]|#]+)(#[^\]|]+)?(\|[^\]]+)?\]\]")
# A markdown mirror immediately following `]]` (with optional leading space):
#   ([Name](path))  — used to detect links that are already dual-linked.
MIRROR_RE = re.compile(r"^\s*\(\[[^\]]*\]\([^)]*\)\)")


@dataclass
class LinkStats:
    total: int = 0
    mirrored: int = 0
    fixable: int = 0
    unresolved: int = 0


def _resolve(
    raw_target: str,
    canonical: dict[str, Page],
    basename_map: dict[str, list[Page]],
) -> Page | None:
    """Resolve a wikilink target to a Page, mirroring lint's resolution rules."""
    target = normalize_link_target(raw_target)
    if not target or target in SPECIAL_LINK_TARGETS:
        return None
    if target in canonical:
        return canonical[target]
    if "/" not in target and len(basename_map.get(target, [])) == 1:
        return basename_map[target][0]
    return None


def _relative_link(source: Page, target: Page) -> str:
    """POSIX relative path from `source`'s directory to `target`'s file."""
    rel = os.path.relpath(target.path, start=source.path.parent)
    return Path(rel).as_posix()


def _process_line(
    line: str,
    source: Page,
    canonical: dict[str, Page],
    basename_map: dict[str, list[Page]],
    stats: LinkStats,
    rewrite: bool,
) -> str:
    """Tally (and optionally rewrite) wikilinks on a single non-code line."""
    matches = list(LINK_RE.finditer(line))
    if not matches:
        return line
    for match in reversed(matches):
        stats.total += 1
        tail = line[match.end():]
        if MIRROR_RE.match(tail):
            stats.mirrored += 1
            continue
        target_page = _resolve(match.group(1), canonical, basename_map)
        if target_page is None:
            stats.unresolved += 1
            continue
        stats.fixable += 1
        if not rewrite:
            continue
        display = match.group(3)[1:] if match.group(3) else target_page.title
        mirror = f" ([{display}]({_relative_link(source, target_page)}))"
        line = line[: match.end()] + mirror + line[match.end():]
    return line


def _process_page(
    page: Page,
    canonical: dict[str, Page],
    basename_map: dict[str, list[Page]],
    rewrite: bool,
) -> tuple[str, LinkStats]:
    stats = LinkStats()
    out: list[str] = []
    in_code = False
    for line in page.text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        out.append(_process_line(line, page, canonical, basename_map, stats, rewrite))
    trailing = "\n" if page.text.endswith("\n") else ""
    return "\n".join(out) + trailing, stats


def cmd_links(fix: bool, write: bool) -> int:
    pages = list_content_pages()
    canonical, basename_map = build_page_indexes(pages)

    totals = LinkStats()
    changed_files = 0
    for page in pages:
        new_text, stats = _process_page(page, canonical, basename_map, rewrite=fix)
        totals.total += stats.total
        totals.mirrored += stats.mirrored
        totals.fixable += stats.fixable
        totals.unresolved += stats.unresolved
        if fix and write and new_text != page.text:
            page.path.write_text(new_text, encoding="utf-8")
            changed_files += 1

    print(f"Wikilinks total:        {totals.total}")
    print(f"  already dual-linked:  {totals.mirrored}")
    print(f"  resolvable (fixable): {totals.fixable}")
    print(f"  unresolved (skipped): {totals.unresolved}")

    if not fix:
        if totals.fixable:
            print("\nRun `python3 tools/wiki.py links --fix` to preview adding markdown mirrors.")
        return 0
    if not write:
        print(f"\nDry run — {totals.fixable} links across the vault would gain a markdown mirror.")
        print("Re-run with `--fix --write` to apply (review the diff before committing).")
        return 0
    print(f"\nWrote markdown mirrors into {changed_files} files ({totals.fixable} links).")
    return 0
