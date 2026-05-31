#!/usr/bin/env python3
"""Self-contained tests for the wiki tooling.

Builds a golden wiki and one-defect-per-rule fixtures in a temp directory at
runtime (so no dummy pages pollute the real vault), points the tooling at them,
and asserts the lint/links/index behaviour. Run with:

    python3 tools/tests/test_wiki.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wiki  # noqa: E402
import wiki_index  # noqa: E402
import wiki_lint  # noqa: E402
import wiki_links  # noqa: E402

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


def make_clean_wiki(root: Path) -> None:
    """Two mutually-linked clean concept pages (no orphans, no errors)."""
    write_page(root, "concepts/a.md", "Links [[concepts/b]].", **base_fields(title="A"))
    write_page(root, "concepts/b.md", "Links [[concepts/a]].", **base_fields(title="B"))


def use_wiki(root: Path) -> None:
    """Point all tooling that captured WIKI_DIR at `root`."""
    wiki.WIKI_DIR = root
    wiki.PROJECTS_DIR = root.parent / "projects"
    wiki.PROJECTS_DIR.mkdir(exist_ok=True)
    wiki_index.WIKI_DIR = root


def report_for(root: Path, strict: bool = True) -> dict:
    use_wiki(root)
    return wiki_lint.build_report(wiki.list_content_pages(), strict=strict)


def test_golden() -> None:
    print("golden:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        make_clean_wiki(root)
        rep = report_for(root, strict=True)
        check("golden has no errors", rep["error_count"] == 0, str(rep["errors"]))
        check("golden has no orphans (strict)", not rep["errors"].get("orphans"), str(rep["errors"].get("orphans")))


def test_defects() -> None:
    print("defects (each fixture trips exactly its rule):")
    cases = [
        ("missing_fields", lambda r: write_page(r, "concepts/c.md", "[[concepts/a]]",
            title="C", type="concept", status="active", created="2026-01-01", updated="2026-01-02")),  # no summary
        ("broken_links", lambda r: write_page(r, "concepts/c.md", "[[concepts/nope]] [[concepts/a]]", **base_fields(title="C"))),
        ("invalid_status", lambda r: write_page(r, "concepts/c.md", "[[concepts/a]]", **base_fields(title="C", status="bogus"))),
        ("invalid_enums", lambda r: write_page(r, "concepts/c.md", "[[concepts/a]]", **base_fields(title="C", confidence="wrong"))),
        ("malformed_dates", lambda r: write_page(r, "concepts/c.md", "[[concepts/a]]", **base_fields(title="C", created="nope"))),
    ]
    for rule, add_defect in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wiki"
            make_clean_wiki(root)
            add_defect(root)
            rep = report_for(root, strict=False)
            tripped = rep["errors"].get(rule, [])
            check(f"{rule} tripped", len(tripped) >= 1, str(rep["errors"]))


def test_warnings() -> None:
    print("warnings:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        make_clean_wiki(root)
        write_page(root, "concepts/c.md", "[[concepts/a]]",
                   **base_fields(title="C", summary="", updated="2025-01-01", created="2026-01-01"))
        rep = report_for(root, strict=False)
        check("empty_required warns", len(rep["warnings"]["empty_required"]) >= 1)
        check("updated_before_created warns", len(rep["warnings"]["updated_before_created"]) >= 1)


def test_fix() -> None:
    print("auto-fix:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        make_clean_wiki(root)
        write_page(root, "concepts/c.md", "[[concepts/a]]",
                   **base_fields(title="C", status="Active", confidence="HIGH"))
        use_wiki(root)
        fixes = wiki_lint.apply_fixes(wiki.list_content_pages())
        check("two fixes applied", len(fixes) == 2, str(fixes))
        rep = report_for(root, strict=False)
        check("no invalid status/enums after fix",
              not rep["errors"]["invalid_status"] and not rep["errors"]["invalid_enums"])


def test_links() -> None:
    print("dual-links:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        make_clean_wiki(root)
        use_wiki(root)
        rc = wiki_links.cmd_links(fix=True, write=True)
        check("links --fix --write returns 0", rc == 0)
        text = (root / "concepts/a.md").read_text()
        check("mirror added with relative path", "([B](b.md))" in text, text)
        # idempotency
        wiki_links.cmd_links(fix=True, write=True)
        text2 = (root / "concepts/a.md").read_text()
        check("links fix idempotent", text == text2)


def test_index() -> None:
    print("index:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        make_clean_wiki(root)
        use_wiki(root)
        check("stale before rebuild", wiki_index.check_indexes() == 1)
        wiki_index.rebuild_indexes()
        check("current after rebuild", wiki_index.check_indexes() == 0)
        check("category index written", (root / "concepts" / "_index.md").exists())


def test_raw_source_links() -> None:
    """Source pages cite raw/ material with path-based wikilinks; lint must treat
    `[[raw/...]]` targets that point at real files as valid, not broken."""
    print("raw-source-links:")
    saved_root = wiki.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "raw/sources-text").mkdir(parents=True)
        (repo / "raw/sources").mkdir(parents=True)
        (repo / "raw/sources-text/Foo Bar.md").write_text("text", encoding="utf-8")
        (repo / "raw/sources/Foo Bar.pdf").write_bytes(b"%PDF-")
        wiki.ROOT = repo
        try:
            check("source-text target resolves (no .md)",
                  wiki.is_raw_file_target("raw/sources-text/Foo Bar"))
            check("pdf target resolves", wiki.is_raw_file_target("raw/sources/Foo Bar.pdf"))
            check("missing raw target rejected",
                  not wiki.is_raw_file_target("raw/sources/Nope.pdf"))
            check("non-raw target rejected",
                  not wiki.is_raw_file_target("concepts/whatever"))
            check("path traversal rejected",
                  not wiki.is_raw_file_target("raw/../../etc/passwd"))

            root = repo / "wiki"
            write_page(root, "concepts/a.md", "Links [[concepts/b]].", **base_fields(title="A"))
            write_page(
                root, "sources/src-x.md",
                "## Sources\n\n- Source text: [[raw/sources-text/Foo Bar]]\n"
                "- Source PDF: [[raw/sources/Foo Bar.pdf]]\n[[concepts/a]]",
                **base_fields(title="X"),
            )
            write_page(root, "concepts/b.md", "Links [[concepts/a]] [[sources/src-x]].",
                       **base_fields(title="B"))
            rep = report_for(root, strict=False)
            check("real raw wikilinks not flagged broken",
                  not rep["errors"]["broken_links"], str(rep["errors"]["broken_links"]))
        finally:
            wiki.ROOT = saved_root


def main() -> int:
    test_golden()
    test_defects()
    test_warnings()
    test_fix()
    test_links()
    test_index()
    test_raw_source_links()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
