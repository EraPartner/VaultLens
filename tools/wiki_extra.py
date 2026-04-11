#!/usr/bin/env python3
"""Additional wiki maintenance utilities."""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
WIKI_DIR = ROOT / "wiki"


def find_qmd() -> Optional[Path]:
    """Locate qmd executable."""
    candidates = [
        Path("/usr/local/bin/qmd"),
        Path.home() / ".npm-global/bin/qmd",
    ]
    for path in candidates:
        if path.exists():
            return path
    result = subprocess.run(["which", "qmd"], capture_output=True)
    if result.returncode == 0:
        return Path(result.stdout.decode().strip())
    return None


def cmd_qmd(args: list[str]) -> str:
    """Run qmd command and return output."""
    qmd_path = find_qmd()
    if not qmd_path:
        return ""
    result = subprocess.run(
        [str(qmd_path)] + args, capture_output=True, text=True, cwd=str(WIKI_DIR)
    )
    return result.stdout if result.returncode == 0 else ""


def generate_source_id() -> str:
    """Generate next source ID."""
    existing = list((WIKI_DIR / "sources").glob("src-*.md"))
    today = datetime.now().strftime("%Y-%m-%d")
    count = 1
    for f in existing:
        if today in f.stem:
            count += 1
    return f"src-{today.replace('-', '')}-{count:03d}"


def qmd_search(query: str, limit: int = 10) -> int:
    """Search wiki using qmd."""
    output = cmd_qmd(["query", query, "-n", str(limit), "--json"])
    if not output:
        print(f"QMD not available or no results for: {query}")
        return 1
    try:
        results = json.loads(output)
        for r in results.get("results", [])[:limit]:
            print(
                f"- {r.get('path', 'unknown')}: {r.get('title', 'untitled')} ({r.get('score', 0):.0%})"
            )
    except json.JSONDecodeError:
        print(output)
    return 0


def suggest_links(page_path: str) -> int:
    """Suggest relevant links for a wiki page."""
    page = WIKI_DIR / page_path
    if not page.exists():
        print(f"Page not found: {page_path}")
        return 1

    content = page.read_text()

    # Extract potential entity/concept mentions
    print(f"Analyzing {page.name} for link suggestions...")

    # Get all entities and concepts
    entities = list((WIKI_DIR / "entities").glob("*.md"))
    concepts = list((WIKI_DIR / "concepts").glob("*.md"))
    topics = list((WIKI_DIR / "topics").glob("*.md"))

    suggestions = []
    content_lower = content.lower()

    for f in entities + concepts + topics:
        if f.name == page.name:
            continue
        name = f.stem.replace("-", " ").lower()
        if name in content_lower:
            suggestions.append(f"  - {f.relative_to(WIKI_DIR).with_suffix('')}")

    if suggestions:
        print("Suggested links:")
        print("\n".join(suggestions[:10]))
    else:
        print("No obvious link suggestions found.")

    return 0


def find_orphans() -> int:
    """Find orphan pages with no inbound links."""
    pages = list(WIKI_DIR.rglob("*.md"))
    pages = [
        p
        for p in pages
        if p.name not in ("index.md", "log.md") and "_templates" not in str(p)
    ]

    inbound = {}
    for page in pages:
        rel = page.relative_to(WIKI_DIR).with_suffix("")
        content = page.read_text()

        for other in pages:
            other_rel = other.relative_to(WIKI_DIR).with_suffix("")
            if (
                f"[[{other_rel}]]" in content
                or f"[[{other_rel.as_posix()}]]" in content
            ):
                inbound.setdefault(other_rel, []).append(page)

    orphans = [
        p for p in pages if p.relative_to(WIKI_DIR).with_suffix("") not in inbound
    ]

    print(f"Found {len(orphans)} orphan pages:")
    for o in orphans[:20]:
        print(f"  - {o.relative_to(WIKI_DIR)}")
    if len(orphans) > 20:
        print(f"  ... and {len(orphans) - 20} more")

    return 0


def count_words() -> int:
    """Count total words in wiki."""
    total = 0
    pages = list(WIKI_DIR.rglob("*.md"))
    for page in pages:
        if "_templates" in str(page) or page.name in ("index.md", "log.md"):
            continue
        content = page.read_text()
        # Strip frontmatter
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                content = content[end + 3 :]
        words = len(content.split())
        total += words
    print(f"Total words in wiki: {total:,}")
    print(
        f"Total pages: {len([p for p in pages if '_templates' not in str(p) and p.name not in ('index.md', 'log.md')])}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Additional wiki utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("next-id", help="Generate next source ID")

    search_parser = sub.add_parser("qmd-search", help="Search with qmd")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("-n", "--limit", type=int, default=10)

    link_parser = sub.add_parser("suggest-links", help="Suggest links for a page")
    link_parser.add_argument("page", help="Page path relative to wiki/")

    sub.add_parser("orphans", help="Find orphan pages")
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
    if args.command == "suggest-links":
        return suggest_links(args.page)
    if args.command == "orphans":
        return find_orphans()
    if args.command == "stats":
        return count_words()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
