"""Command-line entry for the wiki tooling (`python3 tools/wiki.py <command>`).

Lives apart from `wiki.py` so the helpers every `wiki_*` module imports do not import
those modules back; `wiki.py` only launches this module when run as a script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import wiki


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Markdown wiki maintenance tools")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "init",
        help="Create missing fixed directories and local navigation files",
    )

    lint_parser = sub.add_parser("lint", help="Validate links and metadata")
    lint_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat orphan pages as failures",
    )
    lint_parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable JSON report"
    )
    lint_parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply unambiguous repairs (case-normalise confidence/volatility/status)",
    )
    lint_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum text findings to print (default: 50; 0 = all)",
    )

    search_parser = sub.add_parser("search", help="Search wiki content")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=10, help="Max results")
    search_parser.add_argument(
        "--include-archived",
        dest="include_archived",
        action="store_true",
        help="Include pages with status: archived (excluded by default)",
    )

    coverage_parser = sub.add_parser(
        "coverage",
        help="Rank sparse / underlinked pages for the enhancer agent",
    )
    coverage_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    coverage_parser.add_argument(
        "--limit", type=int, default=25, help="Max rows (0 = all)"
    )

    tags_parser = sub.add_parser(
        "tags",
        help="List tags with counts, or filter pages by tag (AND across multiple)",
    )
    tags_parser.add_argument(
        "tag",
        nargs="*",
        help="Tag(s) to filter by. Omit to list all tags with counts.",
    )
    tags_parser.add_argument(
        "--domain", default="", help="Restrict to pages with this `domain` frontmatter"
    )
    tags_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    tags_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max rows (human default: 50; JSON default: all; 0 = all).",
    )

    sub.add_parser("next-id", help="Print the next unused source ID for today")
    sub.add_parser("stats", help="Print wiki page and body-word totals")
    sample_parser = sub.add_parser(
        "sample", help="Print one random page from a fixed wiki category"
    )
    sample_parser.add_argument("kind", choices=["concept", "source"])

    sub.add_parser("validate-log", help="Check log.md entry format")

    log_parser = sub.add_parser("append-log", help="Append entry to wiki/log.md")
    log_parser.add_argument(
        "--operation", help="ingest|query|lint|other (omit when using --from-json)"
    )
    log_parser.add_argument("--title", help="Entry title")
    log_parser.add_argument("--summary", help="One-line summary")
    log_parser.add_argument("--page", action="append", default=[], help="Page path")
    log_parser.add_argument(
        "--source", action="append", default=[], help="Raw source path"
    )
    log_parser.add_argument("--notes", default="", help="Optional notes")
    log_parser.add_argument(
        "--from-json",
        dest="from_json",
        help=(
            "Read fields from a JSON file with keys: operation, title, summary, "
            "pages (list), sources (list), notes. Avoids shell-escaping issues "
            "when titles/summaries contain `&`, `;`, `(...)`, etc."
        ),
    )

    preprocess_parser = sub.add_parser(
        "preprocess",
        help="Pre-extract raw/sources/*.pdf into raw/sources-text/*.md so agents can read them",
    )
    preprocess_parser.add_argument(
        "--pdf",
        help="Process a single PDF (path relative to repo root or absolute). Defaults to all PDFs in raw/sources/.",
    )
    preprocess_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even if the markdown sibling is newer than the PDF",
    )

    project_parser = sub.add_parser(
        "project",
        help="Manage application projects that consume the wiki KB",
    )
    project_parser.add_argument(
        "action",
        choices=["list", "new", "show", "link", "freeze", "unfreeze", "agenda"],
        help="Project subaction",
    )
    project_parser.add_argument(
        "slug",
        nargs="?",
        help="Project slug (required for new/show/link); for agenda, the agenda subcommand",
    )
    project_parser.add_argument(
        "ref",
        nargs="?",
        help="Wiki ref for link (e.g. concepts/some-page); for agenda, the target project slug",
    )
    project_parser.add_argument(
        "extra",
        nargs="?",
        help="For agenda complete/resolve: the task id (e.g. T1)",
    )
    project_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON for list/show/agenda due/clarifications",
    )
    project_parser.add_argument(
        "--include-frozen",
        action="store_true",
        help="Include frozen projects in `project list` (excluded by default)",
    )
    project_parser.add_argument(
        "--slugs",
        action="store_true",
        help="Emit only project slugs for `project list`",
    )

    index_parser = sub.add_parser(
        "index",
        help="Generate/check plain-markdown _index.md files (headless-readable mirror of Dataview)",
    )
    index_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Regenerate all _index.md files (default: check for staleness only)",
    )

    archive_parser = sub.add_parser(
        "archive",
        help="Archive lifecycle (list/page/restore) via status: archived + registry",
    )
    archive_parser.add_argument(
        "action", choices=["list", "page", "restore"], help="Archive subaction"
    )
    archive_parser.add_argument(
        "ref", nargs="?", help="Page reference (e.g. concepts/foo) for page/restore"
    )
    archive_parser.add_argument(
        "--reason",
        default="",
        help="Why the page is being archived (recorded in registry)",
    )
    archive_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON for list"
    )

    inventory_parser = sub.add_parser(
        "inventory",
        help="Track ingest-candidates / questions / tasks / watch items (list/new/show)",
    )
    inventory_parser.add_argument(
        "action", choices=["list", "new", "show"], help="Inventory subaction"
    )
    inventory_parser.add_argument(
        "kind",
        nargs="?",
        help="Kind for new (item/ingest-candidate/question/task/watch/corpus/artifact), "
        "kind filter for list, or kind/slug for show",
    )
    inventory_parser.add_argument("slug", nargs="?", help="Slug (required for new)")
    inventory_parser.add_argument("--title", default="", help="Record title")
    inventory_parser.add_argument(
        "--status",
        default="",
        help="Status (filter for list; default proposed for new)",
    )
    inventory_parser.add_argument(
        "--priority", default="", help="Priority p0-p4 (default p2 for new)"
    )
    inventory_parser.add_argument(
        "--summary", default="", help="One-line summary for new"
    )
    inventory_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON for list/show"
    )

    links_parser = sub.add_parser(
        "links",
        help="Report wikilink dual-link coverage; --fix adds portable markdown mirrors",
    )
    links_parser.add_argument(
        "--fix",
        action="store_true",
        help="Add a markdown mirror after each resolvable bare wikilink (dry-run unless --write)",
    )
    links_parser.add_argument(
        "--write",
        action="store_true",
        help="With --fix, persist changes to disk (otherwise preview only)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        from wiki_init import run_init

        return run_init(wiki.ROOT)
    if args.command == "lint":
        from wiki_lint import run_lint

        return run_lint(
            strict=args.strict,
            as_json=args.json,
            fix=args.fix,
            limit=args.limit,
        )
    if args.command == "search":
        from wiki_query import search

        return search(args.query, args.limit, include_archived=args.include_archived)
    if args.command == "coverage":
        from wiki_query import coverage

        return coverage(as_json=args.json, limit=args.limit)
    if args.command == "tags":
        from wiki_query import tags_command

        return tags_command(
            queries=args.tag,
            domain=args.domain,
            as_json=args.json,
            limit=args.limit,
        )
    if args.command == "next-id":
        print(wiki.generate_source_id())
        return 0
    if args.command == "stats":
        wiki.wiki_stats()
        return 0
    if args.command == "sample":
        return wiki.sample_page(args.kind)
    if args.command == "validate-log":
        from wiki_log import validate_log

        return validate_log()
    if args.command == "append-log":
        from wiki_log import append_log_entry

        if args.from_json:
            json_path = Path(args.from_json)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            return append_log_entry(
                operation=payload["operation"],
                title=payload["title"],
                summary=payload["summary"],
                pages=payload.get("pages", []),
                sources=payload.get("sources", []),
                notes=payload.get("notes", ""),
            )
        missing = [
            name
            for name in ("operation", "title", "summary")
            if not getattr(args, name)
        ]
        if missing:
            parser.error(
                f"append-log requires --{', --'.join(missing)} "
                f"(or pass --from-json with these fields)"
            )
        return append_log_entry(
            operation=args.operation,
            title=args.title,
            summary=args.summary,
            pages=args.page,
            sources=args.source,
            notes=args.notes,
        )
    if args.command == "preprocess":
        from wiki_ingest import preprocess_pdfs

        return preprocess_pdfs(pdf=args.pdf, force=args.force)
    if args.command == "project":
        from wiki_projects import cmd_project

        return cmd_project(
            action=args.action,
            slug=args.slug,
            ref=args.ref,
            as_json=args.json,
            extra=args.extra,
            include_frozen=args.include_frozen,
            slugs_only=args.slugs,
        )
    if args.command == "index":
        from wiki_index import cmd_index

        return cmd_index(rebuild=args.rebuild)
    if args.command == "links":
        from wiki_links import cmd_links

        return cmd_links(fix=args.fix, write=args.write)
    if args.command == "archive":
        from wiki_archive import cmd_archive

        return cmd_archive(
            action=args.action, ref=args.ref, reason=args.reason, as_json=args.json
        )
    if args.command == "inventory":
        from wiki_inventory import cmd_inventory

        return cmd_inventory(
            action=args.action,
            kind=args.kind,
            slug=args.slug,
            title=args.title,
            status=args.status,
            priority=args.priority,
            summary=args.summary,
            as_json=args.json,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
