#!/usr/bin/env python3
"""Self-contained tests for the wiki tooling.

Builds a golden wiki and one-defect-per-rule fixtures in a temp directory at
runtime (so no dummy pages pollute the real vault), points the tooling at them,
and asserts the lint/links/index behaviour. Run with:

    python3 tools/tests/test_wiki.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wiki  # noqa: E402
import wiki_index  # noqa: E402
import wiki_lint  # noqa: E402
import wiki_links  # noqa: E402
import wiki_log  # noqa: E402
import wiki_projects  # noqa: E402
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
        check(
            "golden has no orphans (strict)",
            not rep["errors"].get("orphans"),
            str(rep["errors"].get("orphans")),
        )


def test_reports_excluded() -> None:
    print("reports excluded from lint:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        make_clean_wiki(root)
        # A generated report: no inbound links (would be an orphan), stale dates,
        # and a broken wikilink in its body. None of these should be flagged.
        write_page(
            root,
            "reports/scheduled-cos-brief-2020-01-01.md",
            "Brief referencing [[concepts/gone]].",
            **base_fields(
                title="Brief", type="report", created="2020-01-01", updated="2020-01-01"
            ),
        )
        rep = report_for(root, strict=True)
        orphans = rep["errors"].get("orphans", [])
        check(
            "report not flagged orphan",
            not any("reports/" in o for o in orphans),
            str(orphans),
        )
        check(
            "report broken link not flagged",
            not any("reports/" in b for b in rep["errors"]["broken_links"]),
            str(rep["errors"]["broken_links"]),
        )
        check(
            "report not flagged stale",
            not any("reports/" in s for s in rep["warnings"]["stale_pages"]),
            str(rep["warnings"]["stale_pages"]),
        )
        check(
            "clean content pages still pass",
            rep["error_count"] == 0,
            str(rep["errors"]),
        )


def test_agent_instructions_excluded() -> None:
    print("agent instructions excluded from content:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        make_clean_wiki(root)
        (root / "AGENTS.md").write_text(
            "# Instructions\n\nExample [[raw/sources-text/<stem>]].\n",
            encoding="utf-8",
        )
        rep = report_for(root, strict=True)
        content_paths = {page.rel.as_posix() for page in wiki.list_content_pages()}
        check("AGENTS.md not treated as a page", "AGENTS.md" not in content_paths)
        check("instruction examples are not linted", rep["error_count"] == 0, str(rep))


def test_runtime_log_excluded() -> None:
    print("runtime log excluded from content:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        make_clean_wiki(root)
        log_dir = root / "log"
        log_dir.mkdir()
        (log_dir / "2026-08-25-enhance.md").write_text(
            "# Runtime entry\n\nNo page metadata; broken [[concepts/gone]].\n",
            encoding="utf-8",
        )
        concept = root / "concepts" / "a.md"
        concept.write_text(
            concept.read_text(encoding="utf-8")
            + "\nLinked runtime record [[log/2026-08-25-enhance]].\n",
            encoding="utf-8",
        )
        rep = report_for(root, strict=True)
        content_paths = {page.rel.as_posix() for page in wiki.list_content_pages()}
        check(
            "runtime log note is not a content page",
            "log/2026-08-25-enhance.md" not in content_paths,
        )
        check("runtime log note is not linted", rep["error_count"] == 0, str(rep))


def test_defects() -> None:
    print("defects (each fixture trips exactly its rule):")
    cases = [
        (
            "missing_fields",
            lambda r: write_page(
                r,
                "concepts/c.md",
                "[[concepts/a]]",
                title="C",
                type="concept",
                status="active",
                created="2026-01-01",
                updated="2026-01-02",
            ),
        ),  # no summary
        (
            "broken_links",
            lambda r: write_page(
                r,
                "concepts/c.md",
                "[[concepts/nope]] [[concepts/a]]",
                **base_fields(title="C"),
            ),
        ),
        (
            "invalid_status",
            lambda r: write_page(
                r,
                "concepts/c.md",
                "[[concepts/a]]",
                **base_fields(title="C", status="bogus"),
            ),
        ),
        (
            "invalid_enums",
            lambda r: write_page(
                r,
                "concepts/c.md",
                "[[concepts/a]]",
                **base_fields(title="C", confidence="wrong"),
            ),
        ),
        (
            "malformed_dates",
            lambda r: write_page(
                r,
                "concepts/c.md",
                "[[concepts/a]]",
                **base_fields(title="C", created="nope"),
            ),
        ),
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
        write_page(
            root,
            "concepts/c.md",
            "[[concepts/a]]",
            **base_fields(
                title="C", summary="", updated="2025-01-01", created="2026-01-01"
            ),
        )
        rep = report_for(root, strict=False)
        check("empty_required warns", len(rep["warnings"]["empty_required"]) >= 1)
        check(
            "updated_before_created warns",
            len(rep["warnings"]["updated_before_created"]) >= 1,
        )


def test_fix() -> None:
    print("auto-fix:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        make_clean_wiki(root)
        write_page(
            root,
            "concepts/c.md",
            "[[concepts/a]]",
            **base_fields(title="C", status="Active", confidence="HIGH"),
        )
        use_wiki(root)
        fixes = wiki_lint.apply_fixes(wiki.list_content_pages())
        check("two fixes applied", len(fixes) == 2, str(fixes))
        rep = report_for(root, strict=False)
        check(
            "no invalid status/enums after fix",
            not rep["errors"]["invalid_status"] and not rep["errors"]["invalid_enums"],
        )


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
            check(
                "source-text target resolves (no .md)",
                wiki.is_raw_file_target("raw/sources-text/Foo Bar"),
            )
            check(
                "pdf target resolves",
                wiki.is_raw_file_target("raw/sources/Foo Bar.pdf"),
            )
            check(
                "missing raw target rejected",
                not wiki.is_raw_file_target("raw/sources/Nope.pdf"),
            )
            check(
                "non-raw target rejected",
                not wiki.is_raw_file_target("concepts/whatever"),
            )
            check(
                "path traversal rejected",
                not wiki.is_raw_file_target("raw/../../etc/passwd"),
            )

            root = repo / "wiki"
            write_page(
                root, "concepts/a.md", "Links [[concepts/b]].", **base_fields(title="A")
            )
            write_page(
                root,
                "sources/src-x.md",
                "## Sources\n\n- Source text: [[raw/sources-text/Foo Bar]]\n"
                "- Source PDF: [[raw/sources/Foo Bar.pdf]]\n[[concepts/a]]",
                **base_fields(title="X"),
            )
            write_page(
                root,
                "concepts/b.md",
                "Links [[concepts/a]] [[sources/src-x]].",
                **base_fields(title="B"),
            )
            rep = report_for(root, strict=False)
            check(
                "real raw wikilinks not flagged broken",
                not rep["errors"]["broken_links"],
                str(rep["errors"]["broken_links"]),
            )
        finally:
            wiki.ROOT = saved_root


def test_project_provider_scaffold() -> None:
    print("project-provider-scaffold:")
    saved_root = wiki_projects.ROOT
    saved_projects = wiki_projects.PROJECTS_DIR
    saved_rebuild = wiki_projects._rebuild_projects_todo
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        projects = root / "projects"
        projects.mkdir()
        wiki_projects.ROOT = root
        wiki_projects.PROJECTS_DIR = projects
        wiki_projects._rebuild_projects_todo = lambda: None
        try:
            rc = wiki_projects._project_new("model-agnostic")
            project = projects / "model-agnostic"
            agents_text = (project / "AGENTS.md").read_text(encoding="utf-8")
            claude_text = (project / "CLAUDE.md").read_text(encoding="utf-8")
            check("project scaffold succeeds", rc == 0)
            check("project gets neutral AGENTS.md", "read\n`project.md`" in agents_text)
            check(
                "Claude shim imports neutral context",
                claude_text.startswith("@AGENTS.md\n@project.md\n"),
                claude_text,
            )
            check(
                "Claude shim does not duplicate project rules",
                "Project context wins ties" not in claude_text,
            )
        finally:
            wiki_projects.ROOT = saved_root
            wiki_projects.PROJECTS_DIR = saved_projects
            wiki_projects._rebuild_projects_todo = saved_rebuild


def test_project_freeze() -> None:
    print("project-freeze:")
    saved_root = wiki_projects.ROOT
    saved_projects = wiki_projects.PROJECTS_DIR
    saved_wiki_projects = wiki.PROJECTS_DIR
    saved_rebuild = wiki_projects._rebuild_projects_todo
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        projects = root / "projects"
        project = projects / "cryptopals"
        project.mkdir(parents=True)
        (project / "project.md").write_text(
            "---\n"
            "title: Cryptopals\n"
            "type: project\n"
            "status: active\n"
            "created: 2026-01-01\n"
            "updated: 2026-01-01\n"
            "summary: Exercises.\n"
            "domain: learning\n"
            "tags: []\n"
            "wiki_refs: []\n"
            "---\n",
            encoding="utf-8",
        )
        (project / "TODO.md").write_text("- [ ] finish set 1\n", encoding="utf-8")
        deadlines = projects / "deadlines.md"
        (projects / "deadlines.template.md").write_text(
            "```tasks\nnot done\nfolder includes projects\nhas due date\n```\n",
            encoding="utf-8",
        )
        wiki_projects.ROOT = root
        wiki_projects.PROJECTS_DIR = projects
        wiki.PROJECTS_DIR = projects
        wiki_projects._rebuild_projects_todo = lambda: None
        try:
            check(
                "freeze command succeeds",
                wiki_projects._project_freeze("cryptopals", True) == 0,
            )
            check(
                "project status becomes frozen",
                wiki_projects._find_project("cryptopals").status == "frozen",
            )
            visible = io.StringIO()
            with contextlib.redirect_stdout(visible):
                wiki_projects._project_list(False, slugs_only=True)
            check("default project list excludes frozen", visible.getvalue() == "")
            all_projects = io.StringIO()
            with contextlib.redirect_stdout(all_projects):
                wiki_projects._project_list(False, include_frozen=True, slugs_only=True)
            check(
                "administrative list can include frozen",
                all_projects.getvalue().strip() == "cryptopals",
            )
            wiki_projects._rebuild_deadlines()
            check(
                "deadline query is created from tracked template",
                deadlines.is_file(),
            )
            check(
                "deadline query excludes frozen TODO",
                "path does not include projects/cryptopals/TODO.md"
                in deadlines.read_text(encoding="utf-8"),
            )
            check(
                "unfreeze command succeeds",
                wiki_projects._project_freeze("cryptopals", False) == 0,
            )
            wiki_projects._rebuild_deadlines()
            check(
                "unfreeze removes deadline exclusion",
                "path does not include projects/cryptopals/TODO.md"
                not in deadlines.read_text(encoding="utf-8"),
            )
        finally:
            wiki_projects.ROOT = saved_root
            wiki_projects.PROJECTS_DIR = saved_projects
            wiki.PROJECTS_DIR = saved_wiki_projects
            wiki_projects._rebuild_projects_todo = saved_rebuild


def test_frontmatter_list_parsing() -> None:
    # Normal comma-separated arrays parse to N items (unchanged behavior).
    check(
        "comma list parses to N items",
        wiki._parse_frontmatter_value("[a, b, c]") == ["a", "b", "c"]
        and wiki._coerce_str_list("[a, b, c]") == ["a", "b", "c"],
    )
    # Recovery: an array reflowed to whitespace-separated with no commas — as the
    # host Obsidian app can do to frontmatter a CLI just wrote — still parses to N
    # items instead of one joined blob, so the next write self-heals it.
    check(
        "whitespace-reflowed list recovers to N items",
        wiki._parse_frontmatter_value("[a b c]") == ["a", "b", "c"]
        and wiki._coerce_str_list("[a b c]") == ["a", "b", "c"],
    )
    # Guard: quoted multi-word items (aliases/requires) are never split mid-value.
    check(
        "quoted multi-word items are not split",
        wiki._parse_frontmatter_value('["machine learning", "deep learning"]')
        == ["machine learning", "deep learning"],
    )
    check("single-item list", wiki._parse_frontmatter_value("[a]") == ["a"])
    check("empty list", wiki._parse_frontmatter_value("[]") == [])


def test_frontmatter_eof_and_round_trip() -> None:
    text = "---\ntitle: Original\nstatus: active\n---"
    fm, body = wiki.parse_frontmatter(text)
    check("frontmatter closing delimiter at EOF parses", fm["title"] == "Original")
    check("frontmatter at EOF has an empty body", body == "")

    changed = wiki._set_frontmatter_field(text, "title", "Changed")
    changed = wiki._set_frontmatter_field(changed, "tags", ["one", "two"])
    round_trip, round_body = wiki.parse_frontmatter(changed)
    check(
        "frontmatter setter round-trips updated and appended fields",
        round_trip["title"] == "Changed"
        and round_trip["status"] == "active"
        and round_trip["tags"] == ["one", "two"],
        str(round_trip),
    )
    check("frontmatter setter preserves the empty body", round_body == "")


def test_search_uses_body_words() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        write_page(
            root,
            "concepts/category-only.md",
            "A category is not the requested word.",
            **base_fields(title="Category Only", tags="[cat]"),
        )
        write_page(
            root,
            "concepts/exact.md",
            "The cat appears once.",
            **base_fields(title="Exact"),
        )
        use_wiki(root)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = wiki_query.search("cat", 10)
        rendered = output.getvalue()
        check("exact body search succeeds", rc == 0 and "concepts/exact.md" in rendered)
        check(
            "search ignores frontmatter and substring matches",
            "concepts/category-only.md" not in rendered,
            rendered,
        )


def test_source_id_stats_and_sampling() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        sources = root / "sources"
        sources.mkdir(parents=True)
        for name in (
            "src-2026-09-05-001.md",
            "src-2026-09-05-003.md",
            "src-2026-09-05-x.md",
        ):
            (sources / name).write_text("", encoding="utf-8")
        write_page(
            root,
            "concepts/a.md",
            "one two three",
            **base_fields(title="A"),
        )
        use_wiki(root)
        check(
            "source ID advances beyond the maximum suffix",
            wiki.generate_source_id(dt.date(2026, 9, 5)) == "src-2026-09-05-004",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            words, pages = wiki.wiki_stats()
        check(
            "stats use content page bodies",
            words == 3 and pages == 4,
            output.getvalue(),
        )
        with contextlib.redirect_stdout(output):
            sampled = wiki.sample_page("concept")
        check("safe concept sampler finds a concept", sampled == 0)


def test_bounded_cli_output_defaults() -> None:
    parsed_lint = wiki.build_parser().parse_args(["lint"])
    parsed_tags = wiki.build_parser().parse_args(["tags"])
    check("lint uses a bounded human-output default", parsed_lint.limit is None)
    check("tags selects its output-aware default at runtime", parsed_tags.limit is None)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        for index in range(4):
            write_page(
                root,
                f"concepts/{index}.md",
                "Body.",
                title=f"Page {index}",
            )
        use_wiki(root)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = wiki_lint.run_lint(False, False, False, limit=2)
        rendered = output.getvalue()
        check("lint retains failure status when output is capped", rc == 1)
        check(
            "lint reports omitted findings and the uncapped command",
            "Findings omitted:" in rendered and "--limit 0" in rendered,
            rendered,
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        for index in range(55):
            write_page(
                root,
                f"concepts/{index}.md",
                "Body.",
                **base_fields(title=f"Page {index}", tags=f"[tag-{index}]"),
            )
        use_wiki(root)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = wiki_query.tags_command([], "", True, None)
        check(
            "tags JSON remains complete by default",
            rc == 0 and len(__import__("json").loads(output.getvalue())) == 55,
        )


def test_log_frontmatter_is_stdlib_and_yaml_safe() -> None:
    rendered = wiki_log._render_frontmatter(
        {
            "date": "2026-08-17",
            "title": "Codex: migration",
            "pages": ["concepts/a", 'topics/quoted "item"'],
        }
    )
    check("log frontmatter quotes punctuation", 'title: "Codex: migration"' in rendered)
    check(
        "log frontmatter renders arrays",
        'pages: ["concepts/a", "topics/quoted \\"item\\""]' in rendered,
        rendered,
    )


def main() -> int:
    test_golden()
    test_reports_excluded()
    test_agent_instructions_excluded()
    test_runtime_log_excluded()
    test_defects()
    test_warnings()
    test_fix()
    test_links()
    test_index()
    test_raw_source_links()
    test_project_provider_scaffold()
    test_project_freeze()
    test_frontmatter_list_parsing()
    test_frontmatter_eof_and_round_trip()
    test_search_uses_body_words()
    test_source_id_stats_and_sampling()
    test_bounded_cli_output_defaults()
    test_log_frontmatter_is_stdlib_and_yaml_safe()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
