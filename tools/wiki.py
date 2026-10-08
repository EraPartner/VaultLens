#!/usr/bin/env python3
"""Utilities for maintaining a markdown-based LLM wiki."""

from __future__ import annotations

import datetime as dt
import random
import re
import runpy
import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

if sys.version_info < (3, 11):
    # Unreachable under the 3.11 typing target; the guard exists for older hosts.
    sys.stderr.write(  # pyright: ignore[reportUnreachable]
        "VaultLens requires Python 3.11 or newer. Use Homebrew Python or set BRAIN_PYTHON for host wrappers.\n"
    )
    raise SystemExit(2)

from project_state import FROZEN_STATUS  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
WIKI_DIR = ROOT / "wiki"
PROJECTS_DIR = ROOT / "projects"

IGNORE_DIRS = {"_templates", ".obsidian", "log"}
# Navigation/runtime files and nested agent instructions are repository
# infrastructure, not knowledge pages. Keep them out of lint, indexes, search,
# coverage, and stats even when they live under wiki/ for scoped discovery.
IGNORE_FILES = {
    "index.md",
    "_index.md",
    "log.md",
    "AGENTS.md",
    "CLAUDE.md",
    "AGENTS.override.md",
}
SPECIAL_LINK_TARGETS = {
    "index",
    "log",
    "home",
    "category",
    "page-name",
    "path",
    "to",
    # This operator-specific page is intentionally absent from the public
    # skeleton. When a local Brain supplies it, normal resolution above counts
    # the link and prevents the private page from becoming an orphan.
    "entities/user-background",
}


@dataclass
class Page:
    path: Path
    rel: Path
    frontmatter: dict[str, str | list[str]]
    body: str
    text: str

    @cached_property
    def links(self) -> list[str]:
        # Lazy: search, stats and tags never need the link scan.
        return extract_wikilinks(self.text)

    def scalar(self, key: str) -> str:
        value = self.frontmatter.get(key, "")
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        title = self.scalar("title").strip()
        if title:
            return title
        return slug_to_title(self.rel.stem)

    @property
    def summary(self) -> str:
        summary = self.scalar("summary").strip()
        if summary:
            return summary
        return first_paragraph(self.body)

    @property
    def updated(self) -> str:
        return self.scalar("updated")

    @property
    def tags(self) -> list[str]:
        return _coerce_str_list(self.frontmatter.get("tags"))

    @property
    def domain(self) -> str:
        return self.scalar("domain").strip()

    @property
    def category(self) -> str:
        if len(self.rel.parts) == 1:
            return "root"
        return self.rel.parts[0]

    @property
    def status(self) -> str:
        return self.scalar("status").strip().lower()

    @property
    def is_archived(self) -> bool:
        return self.status == "archived"

    @property
    def confidence(self) -> str:
        """Trust signal: high|medium|low (empty when unset). Lowercased."""
        return self.scalar("confidence").strip().lower()

    @property
    def volatility(self) -> str:
        """Refresh cadence: hot|warm|cold (empty when unset). Lowercased."""
        return self.scalar("volatility").strip().lower()


@dataclass
class Project:
    slug: str
    path: Path
    root: Path
    frontmatter: dict[str, str | list[str]]
    body: str

    def scalar(self, key: str) -> str:
        value = self.frontmatter.get(key, "")
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        title = self.scalar("title").strip()
        return title or slug_to_title(self.slug)

    @property
    def summary(self) -> str:
        summary = self.scalar("summary").strip()
        return summary or first_paragraph(self.body)

    @property
    def status(self) -> str:
        return self.scalar("status").strip().lower()

    @property
    def is_frozen(self) -> bool:
        return self.status == FROZEN_STATUS

    @property
    def domain(self) -> str:
        return self.scalar("domain").strip()

    @property
    def tags(self) -> list[str]:
        return _coerce_str_list(self.frontmatter.get("tags"))

    @property
    def wiki_refs(self) -> list[str]:
        return _coerce_str_list(self.frontmatter.get("wiki_refs"))


def _split_inline_list(inner: str) -> list[str]:
    """Split the body of an inline frontmatter list (`[...]` already stripped) into items.

    Normally comma-separated (`a, b, c`). Recovery path: if an external editor
    reflows the array to whitespace-separated with no commas (`a b c` — observed
    when the host Obsidian app re-serialises frontmatter that a CLI wrote), split
    on whitespace instead — but ONLY when there are no quotes, so quoted
    multi-word items (e.g. `aliases`, `requires`) are never split mid-value.
    """
    if "," not in inner and '"' not in inner and "'" not in inner:
        parts = inner.split()
    else:
        parts = inner.split(",")
    return [p.strip().strip('"').strip("'") for p in parts if p.strip()]


def _coerce_str_list(value: str | list[str] | None) -> list[str]:
    """Frontmatter list coercion shared by Page.tags and Project.{tags,wiki_refs}."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    inner = str(value).strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    inner = inner.strip()
    if not inner:
        return []
    return _split_inline_list(inner)


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
        return _split_inline_list(inner)
    return value


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split a leading YAML block, accepting its closing delimiter at EOF."""
    if not text.startswith("---\n"):
        return None
    closing = re.search(r"^---(?:\n|$)", text[4:], flags=re.MULTILINE)
    if closing is None:
        return None
    end = 4 + closing.start()
    body_start = 4 + closing.end()
    return text[4:end], text[body_start:]


_BLOCK_ITEM_RE = re.compile(r"^\s*-\s+(.*\S)\s*$")


def _is_block_continuation(line: str) -> bool:
    """A line that belongs to the previous key's value (block list or nested map)."""
    return bool(line) and (line[:1].isspace() or line.startswith("- "))


def parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    normalized = text.replace("\r\n", "\n")
    split = _split_frontmatter(normalized)
    if split is None:
        return {}, text
    block, body = split
    result: dict[str, str | list[str]] = {}
    list_key: str | None = None
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # YAML block list (`tags:` then `  - a`), as Obsidian's Properties UI writes.
        item = _BLOCK_ITEM_RE.match(line)
        if item and list_key is not None:
            items = result[list_key]
            if not isinstance(items, list):
                items = result[list_key] = []
            items.append(item.group(1).strip().strip('"').strip("'"))
            continue
        list_key = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        result[key] = _parse_frontmatter_value(value)
        if result[key] == "" and not line[:1].isspace():
            list_key = key
    return result, body


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
INLINE_CODE_RE = re.compile(r"`[^`]*`")


def extract_wikilinks(text: str) -> list[str]:
    result: list[str] = []
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
    return Page(
        path=path,
        rel=path.relative_to(WIKI_DIR),
        frontmatter=fm,
        body=body,
        text=text,
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


def is_runtime_log_target(target: str) -> bool:
    """True when a link points at an existing ignored `wiki/log/` note."""
    if not target.startswith("log/") or ".." in target:
        return False
    return (WIKI_DIR / f"{target}.md").is_file()


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
            if is_runtime_log_target(target):
                continue
            broken.append(f"{page.rel.as_posix()}: [[{raw_target}]]")

    return inbound, broken, ambiguous


def load_project(project_md: Path) -> Project:
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
        projects.append(load_project(project_md))
    return projects


def _render_list_item(item: str) -> str:
    # Quote items with whitespace so `_split_inline_list` never splits them.
    return f'"{item}"' if re.search(r"\s", item) else item


def _render_frontmatter_value(value: str | list[str]) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_render_list_item(item) for item in value) + "]"
    return str(value)


def set_frontmatter_field(text: str, key: str, value: str | list[str]) -> str:
    """Update or append `key: value` inside the frontmatter block of `text`."""
    split = _split_frontmatter(text)
    if split is None:
        return text
    block, body = split
    rendered = _render_frontmatter_value(value)
    pattern = re.compile(rf"^{re.escape(key)}\s*:")
    new_lines: list[str] = []
    found = False
    replacing = False
    for line in block.splitlines():
        if pattern.match(line):
            new_lines.append(f"{key}: {rendered}")
            found = True
            replacing = True
        elif replacing and _is_block_continuation(line):
            continue  # drop the old block value; the new inline value replaces it
        else:
            replacing = False
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}: {rendered}")
    return "---\n" + "\n".join(new_lines) + f"\n---\n{body}"


def generate_source_id(today: dt.date | None = None) -> str:
    """Return the next unused source ID for a date without reusing gaps."""
    day = (today or dt.date.today()).isoformat()
    pattern = re.compile(rf"^src-{re.escape(day)}-(\d+)\.md$")
    suffixes = [
        int(match.group(1))
        for path in (WIKI_DIR / "sources").glob(f"src-{day}-*.md")
        if (match := pattern.match(path.name))
    ]
    return f"src-{day}-{max(suffixes, default=0) + 1:03d}"


def wiki_stats() -> tuple[int, int]:
    """Print and return totals for knowledge pages and their body words."""
    pages = list_content_pages()
    words = sum(len(page.body.split()) for page in pages)
    print(f"Total words in wiki: {words:,}")
    print(f"Total pages: {len(pages)}")
    return words, len(pages)


def sample_page(kind: str) -> int:
    """Print a random concept or source page from a fixed safe directory."""
    directory = WIKI_DIR / ("concepts" if kind == "concept" else "sources")
    pattern = "*.md" if kind == "concept" else "src-*.md"
    pages = sorted(directory.glob(pattern))
    if not pages:
        print(f"No {kind} pages found.")
        return 1
    print(random.choice(pages).relative_to(WIKI_DIR.parent).as_posix())
    return 0


if __name__ == "__main__":
    # Launched by module name, not imported, so wiki.py does not import the modules that import it.
    runpy.run_module("wiki_cli", run_name="__main__")
