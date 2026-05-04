#!/usr/bin/env python3
"""Additional wiki maintenance utilities."""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from wiki import IGNORE_DIRS, IGNORE_FILES, parse_frontmatter

ROOT = Path(__file__).resolve().parents[1]
WIKI_DIR = ROOT / "wiki"


def cmd_qmd(args: list[str]) -> str:
    """Run qmd command and return stdout (empty string when missing or failed)."""
    qmd_path = shutil.which("qmd")
    if not qmd_path:
        return ""
    result = subprocess.run(
        [qmd_path] + args, capture_output=True, text=True, cwd=str(WIKI_DIR)
    )
    return result.stdout if result.returncode == 0 else ""


def generate_source_id() -> str:
    """Generate next source ID."""
    today = datetime.now().strftime("%Y-%m-%d")
    existing = list((WIKI_DIR / "sources").glob(f"src-{today}-*.md"))
    return f"src-{today}-{len(existing) + 1:03d}"


def qmd_search(query: str, limit: int = 10) -> int:
    """Search wiki using qmd."""
    output = cmd_qmd(["query", query, "-n", str(limit), "--json"])
    if not output:
        print(f"QMD not available or no results for: {query}")
        print("Hint: Run ./tools/scripts/setup-qmd.sh to initialize the search index.")
        return 1
    try:
        results = json.loads(output)
        items = results if isinstance(results, list) else results.get("results", [])
        for r in items[:limit]:
            print(
                f"- {r.get('file', 'unknown')}: {r.get('title', 'untitled')} ({r.get('score', 0):.0%})"
            )
    except json.JSONDecodeError:
        print(output)
    return 0


def count_words() -> int:
    """Count total words in wiki body content (frontmatter excluded)."""
    total = 0
    pages = []
    for path in WIKI_DIR.rglob("*.md"):
        rel = path.relative_to(WIKI_DIR)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        if path.name in IGNORE_FILES:
            continue
        pages.append(path)

    for page in pages:
        text = page.read_text(encoding="utf-8")
        _fm, body = parse_frontmatter(text)
        total += len(body.split())

    print(f"Total words in wiki: {total:,}")
    print(f"Total pages: {len(pages)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Additional wiki utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("next-id", help="Generate next source ID")

    search_parser = sub.add_parser("qmd-search", help="Search with qmd")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("-n", "--limit", type=int, default=10)

    sub.add_parser("stats", help="Wiki statistics")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "next-id":
        print(generate_source_id())
        return 0
    if args.command == "qmd-search":
        return qmd_search(args.query, args.limit)
    if args.command == "stats":
        return count_words()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
