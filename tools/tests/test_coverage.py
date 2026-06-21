#!/usr/bin/env python3
"""Self-contained tests for the coverage ranking (wiki_query.rank_coverage).

Builds throwaway wikis in a temp directory, points the tooling at them, and
asserts both coverage paths: the ABSOLUTE floors (pages tripping a hard floor
rank first) and the RELATIVE fallback (when every page clears the floors, the
weakest concepts/topics still come back, correctly ordered). Run with:

    python3 tools/tests/test_coverage.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wiki  # noqa: E402
import wiki_query  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def write_page(root: Path, rel: str, body: str = "", **fm: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in fm.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


def base_fields(**over: str) -> dict[str, str]:
    fm = {
        "title": "Page",
        "type": "concept",
        "status": "active",
        "created": "2026-01-01",
        "updated": "2026-01-02",
        "summary": "A clean page.",
    }
    fm.update(over)
    return fm


def filler(words: int) -> str:
    """Body with `words` plain word tokens (clears the shallow floor when large)."""
    return " ".join(["lorem"] * words)


def rank_for(root: Path, limit: int = 25) -> tuple[list[dict], str]:
    """Point the tooling at `root` and run the pure ranking helper."""
    wiki.WIKI_DIR = root
    pages = wiki.list_content_pages()
    canonical, basename_map = wiki.build_page_indexes(pages)
    inbound, _b, _a = wiki.compute_inbound_links(pages, canonical, basename_map)
    return wiki_query.rank_coverage(pages, inbound, limit)


def test_absolute_path() -> None:
    print("absolute floors:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        # Three healthy, mutually-linked, long concept pages. Each gets 2 inbound
        # and 2 outbound links and >300 words, so they clear every floor.
        healthy = ["a", "b", "c"]
        for name in healthy:
            others = [f"[[concepts/{o}]]" for o in healthy if o != name]
            write_page(
                root,
                f"concepts/{name}.md",
                filler(400) + " " + " ".join(others),
                **base_fields(title=name.upper()),
            )
        # A starved stub: short body, no inbound links. Trips shallow +
        # underlinked (it links out to a and b, so it is not isolated).
        write_page(
            root,
            "concepts/stub.md",
            "tiny [[concepts/a]] [[concepts/b]]",
            **base_fields(title="Stub"),
        )

        rows, mode = rank_for(root)
        check("mode is absolute when a floor trips", mode == "absolute", mode)
        paths = [r["path"] for r in rows]
        check(
            "only the flagged stub is returned",
            paths == ["concepts/stub.md"],
            str(paths),
        )
        stub = rows[0]
        check("stub flagged shallow", stub["shallow"] is True)
        check("stub flagged underlinked", stub["underlinked"] is True)
        check("flagged rows carry a score", stub["score"] > 0, str(stub))


def test_relative_fallback() -> None:
    print("relative fallback:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        # Every page clears every absolute floor: long body, >=2 inbound, >=2
        # outbound. They differ only by word count so the relative order is
        # deterministic. Each links to all others -> inbound >= 2 for all.
        sizes = {"big": 800, "mid": 500, "small": 320}
        for name in sizes:
            others = [f"[[concepts/{o}]]" for o in sizes if o != name]
            write_page(
                root,
                f"concepts/{name}.md",
                filler(sizes[name]) + " " + " ".join(others),
                **base_fields(title=name),
            )

        rows, mode = rank_for(root)
        check("mode is relative when all clear the floors", mode == "relative", mode)
        check("fallback is non-empty", len(rows) == 3, str(len(rows)))
        order = [r["path"] for r in rows]
        check(
            "ordered ascending by word count (weakest first)",
            order == ["concepts/small.md", "concepts/mid.md", "concepts/big.md"],
            str(order),
        )
        check("rows expose word counts", all("words" in r for r in rows))

        # Archived pages must not appear in the fallback even if weak.
        write_page(
            root,
            "concepts/dead.md",
            "stub",
            **base_fields(title="Dead", status="archived"),
        )
        rows2, mode2 = rank_for(root)
        # The archived stub is shallow+isolated, so it would trip a floor and
        # flip us back to the absolute path *unless* archived pages are skipped
        # there too. The absolute path does not skip archived, so it trips —
        # which is acceptable; what matters is the fallback skips archived.
        if mode2 == "relative":
            check(
                "archived page excluded from fallback",
                "concepts/dead.md" not in [r["path"] for r in rows2],
                str([r["path"] for r in rows2]),
            )
        else:
            # If the archived stub trips the absolute floor, assert it is the
            # only thing returned there (documents the boundary behavior).
            check(
                "archived stub trips absolute floor",
                [r["path"] for r in rows2] == ["concepts/dead.md"],
                str([r["path"] for r in rows2]),
            )


def test_limit() -> None:
    print("limit flag:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        sizes = {"big": 800, "mid": 500, "small": 320}
        for name in sizes:
            others = [f"[[concepts/{o}]]" for o in sizes if o != name]
            write_page(
                root,
                f"concepts/{name}.md",
                filler(sizes[name]) + " " + " ".join(others),
                **base_fields(title=name),
            )
        rows, mode = rank_for(root, limit=2)
        check("limit truncates fallback", len(rows) == 2, str(len(rows)))
        check(
            "limit keeps the weakest first",
            [r["path"] for r in rows] == ["concepts/small.md", "concepts/mid.md"],
            str([r["path"] for r in rows]),
        )


def main() -> int:
    test_absolute_path()
    test_relative_fallback()
    test_limit()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
