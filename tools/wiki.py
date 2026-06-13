#!/usr/bin/env python3
"""Utilities for maintaining a markdown-based LLM wiki."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WIKI_DIR = ROOT / "wiki"
PROJECTS_DIR = ROOT / "projects"

IGNORE_DIRS = {"_templates", ".obsidian"}
IGNORE_FILES = {"index.md", "_index.md", "log.md"}
SPECIAL_LINK_TARGETS = {"index", "log", "home", "category", "page-name", "path", "to"}


@dataclass
class Page:
    path: Path
    rel: Path
    frontmatter: dict[str, str | list[str]]
    body: str
    text: str
    links: list[str]

    def _scalar(self, key: str) -> str:
        value = self.frontmatter.get(key, "")
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        title = self._scalar("title").strip()
        if title:
            return title
        return slug_to_title(self.rel.stem)

    @property
    def summary(self) -> str:
        summary = self._scalar("summary").strip()
        if summary:
            return summary
        return first_paragraph(self.body)

    @property
    def updated(self) -> str:
        return self._scalar("updated")

    @property
    def tags(self) -> list[str]:
        return _coerce_str_list(self.frontmatter.get("tags"))

    @property
    def domain(self) -> str:
        return self._scalar("domain").strip()

    @property
    def category(self) -> str:
        if len(self.rel.parts) == 1:
            return "root"
        return self.rel.parts[0]

    @property
    def status(self) -> str:
        return self._scalar("status").strip().lower()

    @property
    def is_archived(self) -> bool:
        return self.status == "archived"

    @property
    def confidence(self) -> str:
        """Trust signal: high|medium|low (empty when unset). Lowercased."""
        return self._scalar("confidence").strip().lower()

    @property
    def volatility(self) -> str:
        """Refresh cadence: hot|warm|cold (empty when unset). Lowercased."""
        return self._scalar("volatility").strip().lower()


@dataclass
class Project:
    slug: str
    path: Path
    root: Path
    frontmatter: dict[str, str | list[str]]
    body: str

    def _scalar(self, key: str) -> str:
        value = self.frontmatter.get(key, "")
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        title = self._scalar("title").strip()
        return title or slug_to_title(self.slug)

    @property
    def summary(self) -> str:
        summary = self._scalar("summary").strip()
        return summary or first_paragraph(self.body)

    @property
    def status(self) -> str:
        return self._scalar("status").strip()

    @property
    def domain(self) -> str:
        return self._scalar("domain").strip()

    @property
    def tags(self) -> list[str]:
        return _coerce_str_list(self.frontmatter.get("tags"))

    @property
    def wiki_refs(self) -> list[str]:
        return _coerce_str_list(self.frontmatter.get("wiki_refs"))


def _coerce_str_list(value: str | list[str] | None) -> list[str]:
    """Frontmatter list coercion shared by Page.tags and Project.{tags,wiki_refs}."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    text = str(value).strip().strip("[]")
    return [item.strip().strip('"').strip("'") for item in text.split(",") if item.strip()]


def slug_to_title(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def first_paragraph(text: str) -> str:
    lines = text.splitlines()
    in_code = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-", "*", "+")):
            line = line[1:].strip()
            if not line:
                continue
        return line[:220]
    return "No summary available."


def _parse_frontmatter_value(raw: str) -> str | list[str]:
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        items = [item.strip().strip('"').strip("'") for item in inner.split(",")]
        return [item for item in items if item]
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, text
    end = normalized.find("\n---\n", 4)
    if end == -1:
        return {}, text
    block = normalized[4:end]
    body = normalized[end + 5 :]
    result: dict[str, str | list[str]] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = _parse_frontmatter_value(value)
    return result, body


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
INLINE_CODE_RE = re.compile(r"`[^`]*`")


def extract_wikilinks(text: str) -> list[str]:
    result = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        # Inline code spans (e.g. `lst[[1]]`) are not wikilinks; strip them so
        # R/Python double-bracket indexing is not misread as a [[link]].
        line = INLINE_CODE_RE.sub("", line)
        for match in WIKILINK_RE.finditer(line):
            result.append(match.group(1).strip())
    return result


def wiki_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(WIKI_DIR.rglob("*.md")):
        rel = path.relative_to(WIKI_DIR)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        files.append(path)
    return files


def load_page(path: Path) -> Page:
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    links = extract_wikilinks(text)
    return Page(
        path=path,
        rel=path.relative_to(WIKI_DIR),
        frontmatter=fm,
        body=body,
        text=text,
        links=links,
    )


def list_content_pages() -> list[Page]:
    pages: list[Page] = []
    for path in wiki_files():
        if path.name in IGNORE_FILES:
            continue
        pages.append(load_page(path))
    return pages


def normalize_link_target(target: str) -> str:
    value = target.strip().lstrip("/")
    if value.endswith(".md"):
        value = value[:-3]
    return value


def is_raw_file_target(target: str) -> bool:
    """True if a (normalized) wikilink target points at a real file/dir in raw/.

    Source pages cite their immutable material with path-based wikilinks into
    `raw/` (e.g. `[[raw/sources/Foo.pdf]]`, `[[raw/sources-text/Foo]]`). Those
    are not wiki pages, so they never appear in the page index, but they are
    valid links — resolve them against the filesystem rather than flagging them
    broken. `normalize_link_target` strips a trailing `.md`, so also probe the
    `.md` sibling for source-text targets.
    """
    if not target.startswith("raw/") or ".." in target:
        return False
    return (ROOT / target).exists() or (ROOT / f"{target}.md").exists()


def build_page_indexes(
    pages: list[Page],
) -> tuple[dict[str, Page], dict[str, list[Page]]]:
    """Return (canonical-key → Page, basename → [Page]) lookup tables."""
    canonical = {page.rel.with_suffix("").as_posix(): page for page in pages}
    basename_map: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        basename_map[page.rel.stem].append(page)
    return canonical, basename_map


def compute_inbound_links(
    pages: list[Page],
    canonical: dict[str, Page],
    basename_map: dict[str, list[Page]],
    *,
    skip_categories: set[str] | None = None,
) -> tuple[dict[str, int], list[str], list[str]]:
    """Walk every page's links and tally inbound counts.

    Returns (inbound_counts, broken_links, ambiguous_links). Pages whose
    category is in `skip_categories` contribute nothing on either side.
    """
    skip = skip_categories or set()
    inbound: dict[str, int] = defaultdict(int)
    broken: list[str] = []
    ambiguous: list[str] = []

    for page in pages:
        if page.category in skip:
            continue
        for raw_target in page.links:
            target = normalize_link_target(raw_target)
            if not target:
                continue
            if target in canonical:
                inbound[target] += 1
                continue
            if target in SPECIAL_LINK_TARGETS:
                continue
            if "/" not in target and target in basename_map:
                candidates = basename_map[target]
                if len(candidates) == 1:
                    inbound[candidates[0].rel.with_suffix("").as_posix()] += 1
                else:
                    ambiguous.append(
                        f"{page.rel.as_posix()}: [[{raw_target}]] matches {len(candidates)} pages"
                    )
                continue
            if is_raw_file_target(target):
                continue
            broken.append(f"{page.rel.as_posix()}: [[{raw_target}]]")

    return inbound, broken, ambiguous


def _load_project(project_md: Path) -> Project:
    text = project_md.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    return Project(
        slug=project_md.parent.name,
        path=project_md,
        root=project_md.parent,
        frontmatter=fm,
        body=body,
    )


def list_projects() -> list[Project]:
    if not PROJECTS_DIR.exists():
        return []
    projects: list[Project] = []
    for project_md in sorted(PROJECTS_DIR.glob("*/project.md")):
        projects.append(_load_project(project_md))
    return projects


def _render_frontmatter_value(value: str | list[str]) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(value) + "]"
    return str(value)


def _set_frontmatter_field(text: str, key: str, value: str | list[str]) -> str:
    """Update or append `key: value` inside the frontmatter block of `text`."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    block = text[4:end]
    rendered = _render_frontmatter_value(value)
    pattern = re.compile(rf"^{re.escape(key)}\s*:")
    new_lines: list[str] = []
    found = False
    for line in block.splitlines():
        if pattern.match(line):
            new_lines.append(f"{key}: {rendered}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}: {rendered}")
    return "---\n" + "\n".join(new_lines) + f"\n---\n{text[end + 5:]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Markdown wiki maintenance tools")
    sub = parser.add_subparsers(dest="command", required=True)

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
        default=0,
        help="Max rows (0 = all). Default 0 for filter, 0 for tally.",
    )

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
        help="Manage application projects that consume the wiki KB (list/new/show/link)",
    )
    project_parser.add_argument(
        "action",
        choices=["list", "new", "show", "link"],
        help="Project subaction",
    )
    project_parser.add_argument(
        "slug",
        nargs="?",
        help="Project slug (required for new/show/link)",
    )
    project_parser.add_argument(
        "ref",
        nargs="?",
        help="Wiki page reference (required for link, e.g. concepts/some-page)",
    )
    project_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON for list/show",
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
        "--reason", default="", help="Why the page is being archived (recorded in registry)"
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
        "--status", default="", help="Status (filter for list; default proposed for new)"
    )
    inventory_parser.add_argument(
        "--priority", default="", help="Priority p0-p4 (default p2 for new)"
    )
    inventory_parser.add_argument("--summary", default="", help="One-line summary for new")
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

    if args.command == "lint":
        from wiki_lint import run_lint

        return run_lint(strict=args.strict, as_json=args.json, fix=args.fix)
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
