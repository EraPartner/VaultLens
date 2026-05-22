#!/usr/bin/env python3
"""Utilities for maintaining a markdown-based LLM wiki."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WIKI_DIR = ROOT / "wiki"
PROJECTS_DIR = ROOT / "projects"
RAW_SOURCES_DIR = ROOT / "raw" / "sources"
RAW_SOURCES_TEXT_DIR = ROOT / "raw" / "sources-text"

IGNORE_DIRS = {"_templates", ".obsidian"}
IGNORE_FILES = {"index.md", "log.md"}
SPECIAL_LINK_TARGETS = {"index", "log", "home", "category", "page-name", "path", "to"}
PROJECT_REQUIRED_FIELDS = {"title", "type", "status", "created", "updated", "summary"}


class ExtractStatus(Enum):
    EXTRACTED = "extracted"
    SKIPPED = "skipped"
    DECRYPTED = "decrypted"


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


def extract_wikilinks(text: str) -> list[str]:
    result = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
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
            broken.append(f"{page.rel.as_posix()}: [[{raw_target}]]")

    return inbound, broken, ambiguous


STALENESS_DAYS = 180


def check_staleness(pages: list[Page]) -> list[str]:
    stale: list[str] = []
    today = dt.date.today()
    for page in pages:
        if page.category in {"system", "root"}:
            continue
        raw = page.updated.strip()
        if not raw:
            continue
        try:
            updated = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        age = (today - updated).days
        if age > STALENESS_DAYS:
            stale.append(f"{page.rel.as_posix()}: last updated {raw} ({age} days ago)")
    return stale


def validate_log() -> int:
    log_path = WIKI_DIR / "log.md"
    if not log_path.exists():
        print("No log.md found")
        return 1

    text = log_path.read_text(encoding="utf-8")
    entry_re = re.compile(r"^## \[\d{4}-\d{2}-\d{2}\] \w+ \| .+$")
    entries = 0
    malformed: list[str] = []

    for i, line in enumerate(text.splitlines(), 1):
        if line.startswith("## ["):
            entries += 1
            if not entry_re.match(line):
                malformed.append(f"line {i}: {line}")
        elif line.startswith("## ") and "[" not in line and i > 3:
            malformed.append(f"line {i}: heading missing date format: {line}")

    print(f"Log entries: {entries}")
    print(f"Malformed entries: {len(malformed)}")
    if malformed:
        print("\nMalformed:")
        for row in malformed:
            print(f"- {row}")
    return 1 if malformed else 0


REQUIRED_FRONTMATTER_BASE = {"title", "type", "status", "created", "updated", "summary"}
REQUIRED_FRONTMATTER_BY_CATEGORY = {
    "sources": {"source_id", "source_type", "origin", "ingested_on"},
}
LINK_VALIDATION_SKIP_CATEGORIES = {"system"}
ORPHAN_EXEMPT = {"home", "system/schema"}


def lint(strict: bool) -> int:
    pages = list_content_pages()
    canonical, basename_map = build_page_indexes(pages)

    missing_fields: list[str] = []
    for page in pages:
        if page.category in {"system", "root"}:
            continue
        need = set(REQUIRED_FRONTMATTER_BASE)
        need.update(REQUIRED_FRONTMATTER_BY_CATEGORY.get(page.category, set()))
        missing = sorted(key for key in need if key not in page.frontmatter)
        if missing:
            missing_fields.append(
                f"{page.rel.as_posix()}: missing {', '.join(missing)}"
            )

    inbound, broken_links, ambiguous_links = compute_inbound_links(
        pages,
        canonical,
        basename_map,
        skip_categories=LINK_VALIDATION_SKIP_CATEGORIES,
    )

    orphan_pages: list[str] = []
    for page in pages:
        key = page.rel.with_suffix("").as_posix()
        if key in ORPHAN_EXEMPT:
            continue
        if inbound.get(key, 0) == 0:
            orphan_pages.append(page.rel.as_posix())

    stale_pages = check_staleness(pages)

    projects = list_projects()
    project_missing, project_broken_refs = lint_projects(projects, canonical, basename_map)

    print(f"Pages checked: {len(pages)}")
    print(f"Missing frontmatter fields: {len(missing_fields)}")
    print(f"Broken links: {len(broken_links)}")
    print(f"Ambiguous links: {len(ambiguous_links)}")
    print(f"Orphans: {len(orphan_pages)}")
    print(f"Stale pages (>{STALENESS_DAYS} days): {len(stale_pages)}")
    print(f"Projects checked: {len(projects)}")
    print(f"Project missing fields: {len(project_missing)}")
    print(f"Project broken wiki_refs: {len(project_broken_refs)}")

    if missing_fields:
        print("\nMissing fields:")
        for row in missing_fields:
            print(f"- {row}")

    if broken_links:
        print("\nBroken links:")
        for row in broken_links:
            print(f"- {row}")

    if ambiguous_links:
        print("\nAmbiguous links:")
        for row in ambiguous_links:
            print(f"- {row}")

    if orphan_pages:
        print("\nOrphan pages:")
        for row in orphan_pages:
            print(f"- {row}")

    if stale_pages:
        print("\nStale pages:")
        for row in stale_pages:
            print(f"- {row}")

    if project_missing:
        print("\nProject missing fields:")
        for row in project_missing:
            print(f"- {row}")

    if project_broken_refs:
        print("\nProject broken wiki_refs:")
        for row in project_broken_refs:
            print(f"- {row}")

    print("\nNote: Contradiction and semantic quality checks require agent review.")
    print("Run: python3 tools/agents/wiki-agent.py contradict")

    has_errors = bool(
        missing_fields
        or broken_links
        or ambiguous_links
        or project_missing
        or project_broken_refs
    )
    if strict:
        has_errors = has_errors or bool(orphan_pages)

    return 1 if has_errors else 0


def _body_word_count(body: str) -> int:
    in_code = False
    words = 0
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        words += len(re.findall(r"[A-Za-z0-9_]+", line))
    return words


SHALLOW_WORD_THRESHOLD = 300
SPARSE_TOPIC_CONCEPT_THRESHOLD = 5


def coverage(as_json: bool, limit: int) -> int:
    """Report sparse-coverage targets for the enhancer agent.

    Ranks pages by weakness signals:
      - Body word count (shallow < 300 words)
      - Inbound wikilink count (underlinked < 2)
      - Outbound wikilink count (isolated < 2)
      - For topics: concept-page count mentioned in body
    """
    pages = list_content_pages()
    canonical, basename_map = build_page_indexes(pages)
    inbound, _broken, _ambiguous = compute_inbound_links(pages, canonical, basename_map)

    rows: list[dict] = []
    for page in pages:
        if page.category in {"system", "root", "entities"}:
            continue
        key = page.rel.with_suffix("").as_posix()
        word_count = _body_word_count(page.body)
        outbound = len(page.links)
        inbound_count = inbound.get(key, 0)

        shallow = word_count < SHALLOW_WORD_THRESHOLD
        underlinked = inbound_count < 2
        isolated = outbound < 2

        concept_count = 0
        if page.category == "topics":
            concept_count = sum(
                1
                for raw in page.links
                if normalize_link_target(raw).startswith("concepts/")
            )

        sparse_topic = (
            page.category == "topics" and concept_count < SPARSE_TOPIC_CONCEPT_THRESHOLD
        )

        score = 0
        if shallow:
            score += max(0, SHALLOW_WORD_THRESHOLD - word_count) // 30
        if underlinked:
            score += (2 - inbound_count) * 5
        if isolated:
            score += (2 - outbound) * 3
        if sparse_topic:
            score += (SPARSE_TOPIC_CONCEPT_THRESHOLD - concept_count) * 4

        if score == 0:
            continue

        source_origin = page._scalar("origin") if page.category == "sources" else ""

        rows.append(
            {
                "path": page.rel.as_posix(),
                "category": page.category,
                "title": page.title,
                "words": word_count,
                "inbound": inbound_count,
                "outbound": outbound,
                "concept_count": concept_count,
                "sparse_topic": sparse_topic,
                "shallow": shallow,
                "underlinked": underlinked,
                "isolated": isolated,
                "score": score,
                "origin": source_origin,
            }
        )

    rows.sort(key=lambda row: (-row["score"], row["path"]))
    top = rows[:limit] if limit > 0 else rows

    if as_json:
        print(json.dumps(top, indent=2))
        return 0

    print(f"Coverage candidates (top {len(top)} of {len(rows)} flagged):\n")
    print(
        f"{'score':>5}  {'words':>5}  {'in':>3}  {'out':>3}  flags                    path"
    )
    for row in top:
        flags = []
        if row["shallow"]:
            flags.append("shallow")
        if row["underlinked"]:
            flags.append("underlinked")
        if row["isolated"]:
            flags.append("isolated")
        if row["sparse_topic"]:
            flags.append(f"sparse-topic({row['concept_count']})")
        flag_str = ",".join(flags)
        print(
            f"{row['score']:>5}  {row['words']:>5}  {row['inbound']:>3}  "
            f"{row['outbound']:>3}  {flag_str:<24} {row['path']}"
        )
    return 0


def search(query: str, limit: int) -> int:
    terms = [token for token in re.findall(r"[A-Za-z0-9_\-]+", query.lower()) if token]
    if not terms:
        print("No query terms found")
        return 1

    pages = list_content_pages()
    scored: list[tuple[int, Page]] = []
    for page in pages:
        text = page.text.lower()
        title = page.title.lower()
        score = 0
        for term in terms:
            score += text.count(term)
            score += 5 * title.count(term)
        if score > 0:
            scored.append((score, page))

    scored.sort(key=lambda item: (-item[0], item[1].rel.as_posix()))
    if not scored:
        print("No results")
        return 0

    for score, page in scored[:limit]:
        print(f"{score:>3}  {page.rel.as_posix()}  {page.title}")
    return 0


def tags_command(
    queries: list[str], domain: str, as_json: bool, limit: int
) -> int:
    """List all tags or filter pages by tag.

    With no `queries`, prints `tag\tcount` for every tag in the wiki.
    With one or more `queries`, prints pages whose tags contain ALL queries
    (case-insensitive). `domain` further restricts results to pages whose
    `domain` frontmatter matches (case-insensitive). `--limit 0` disables truncation.
    """
    pages = list_content_pages()
    domain_norm = domain.strip().lower() if domain else ""

    if not queries:
        counts: dict[str, int] = defaultdict(int)
        for page in pages:
            if domain_norm and page.domain.lower() != domain_norm:
                continue
            for tag in page.tags:
                counts[tag] += 1

        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if limit > 0:
            rows = rows[:limit]

        if as_json:
            print(json.dumps([{"tag": tag, "count": n} for tag, n in rows], indent=2))
            return 0

        if not rows:
            print("No tags found.")
            return 0

        width = max(len(tag) for tag, _ in rows)
        for tag, count in rows:
            print(f"{tag:<{width}}  {count}")
        return 0

    wanted = {q.lower() for q in queries if q.strip()}
    if not wanted:
        print("No query tags supplied.")
        return 1

    matched: list[Page] = []
    for page in pages:
        if domain_norm and page.domain.lower() != domain_norm:
            continue
        page_tags = {t.lower() for t in page.tags}
        if wanted.issubset(page_tags):
            matched.append(page)

    matched.sort(key=lambda p: p.rel.as_posix())
    if limit > 0:
        matched = matched[:limit]

    if as_json:
        payload = [
            {
                "path": page.rel.as_posix(),
                "title": page.title,
                "tags": page.tags,
                "domain": page.domain,
            }
            for page in matched
        ]
        print(json.dumps(payload, indent=2))
        return 0

    if not matched:
        print(f"No pages match tags: {', '.join(sorted(wanted))}")
        return 0

    print(f"Pages matching tags [{', '.join(sorted(wanted))}]: {len(matched)}\n")
    for page in matched:
        tag_str = ", ".join(page.tags)
        print(f"{page.rel.as_posix()}  ({tag_str})")
    return 0


def append_log_entry(
    operation: str,
    title: str,
    summary: str,
    pages: list[str],
    sources: list[str],
    notes: str,
) -> int:
    date = dt.datetime.now().strftime("%Y-%m-%d")
    log_path = WIKI_DIR / "log.md"
    if not log_path.exists():
        log_path.write_text("# Log\n\n", encoding="utf-8")

    rows = [f"## [{date}] {operation} | {title}", ""]
    rows.append(f"- Summary: {summary}")

    if sources:
        rows.append("- Sources: " + ", ".join(f"`{source}`" for source in sources))
    if pages:
        rendered_pages = []
        for page in pages:
            cleaned = page.strip().replace(".md", "")
            rendered_pages.append(f"[[{cleaned}]]")
        rows.append("- Pages touched: " + ", ".join(rendered_pages))
    if notes:
        rows.append(f"- Notes: {notes}")

    rows.append("")

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n" + "\n".join(rows))

    print(f"Appended log entry to {log_path}")
    return 0


def text_path_for_pdf(pdf_path: Path) -> Path:
    """Return the markdown sibling path for a given PDF in raw/sources/."""
    return RAW_SOURCES_TEXT_DIR / f"{pdf_path.stem}.md"


def _pdf_needs_extract(pdf_path: Path, text_path: Path) -> bool:
    if not text_path.exists():
        return True
    return pdf_path.stat().st_mtime > text_path.stat().st_mtime


def extract_pdf_to_markdown(
    pdf_path: Path, force: bool = False
) -> tuple[Path, ExtractStatus]:
    """Extract a PDF into a markdown sibling using `pdftotext -layout`.

    Returns (markdown_path, status). Status is SKIPPED when the sibling
    exists and is newer than the PDF (and force=False), EXTRACTED on a
    plain run, or DECRYPTED when qpdf was needed first.
    Raises FileNotFoundError, RuntimeError on failure.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if shutil.which("pdftotext") is None:
        raise RuntimeError(
            "pdftotext is not installed. Install poppler (`brew install poppler`) "
            "or xpdf-tools and retry."
        )

    text_path = text_path_for_pdf(pdf_path)
    if not force and not _pdf_needs_extract(pdf_path, text_path):
        return text_path, ExtractStatus.SKIPPED

    text_path.parent.mkdir(parents=True, exist_ok=True)

    raw_txt = text_path.with_suffix(".raw.txt")
    decrypted_pdf: Path | None = None
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), str(raw_txt)],
            capture_output=True,
            text=True,
        )
        PERMISSION_ERROR = "Copying of text from this document is not allowed"
        if result.returncode != 0 and PERMISSION_ERROR in result.stderr:
            # PDF has copy-protection; strip restrictions with qpdf and retry.
            if shutil.which("qpdf") is None:
                raise RuntimeError(
                    f"Permission Error: {PERMISSION_ERROR}. "
                    "Install qpdf (`brew install qpdf`) to bypass restriction."
                )
            decrypted_pdf = text_path.with_suffix(".decrypted.pdf")
            qpdf_result = subprocess.run(
                ["qpdf", "--decrypt", str(pdf_path), str(decrypted_pdf)],
                capture_output=True,
                text=True,
            )
            if qpdf_result.returncode != 0:
                raise RuntimeError(
                    f"qpdf decrypt failed for {pdf_path.name}: {qpdf_result.stderr.strip()}"
                )
            result = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", str(decrypted_pdf), str(raw_txt)],
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"pdftotext failed for {pdf_path.name}: {result.stderr.strip()}"
            )

        body = raw_txt.read_text(encoding="utf-8", errors="replace")
    finally:
        raw_txt.unlink(missing_ok=True)
        if decrypted_pdf is not None:
            decrypted_pdf.unlink(missing_ok=True)

    today = dt.datetime.now().strftime("%Y-%m-%d")
    status = (
        ExtractStatus.DECRYPTED if decrypted_pdf is not None else ExtractStatus.EXTRACTED
    )
    extractor = (
        "qpdf --decrypt | pdftotext -layout"
        if status is ExtractStatus.DECRYPTED
        else "pdftotext -layout"
    )
    header = (
        "---\n"
        f'source_pdf: "raw/sources/{pdf_path.name}"\n'
        f"extracted: {today}\n"
        f"extractor: {extractor}\n"
        "---\n\n"
        f"# {pdf_path.stem}\n\n"
        "> Pre-extracted plain text from the source PDF. Treat as ground-truth\n"
        "> reference content. Layout artifacts (page numbers, headers, line\n"
        "> breaks inside paragraphs) may be present.\n\n"
    )

    text_path.write_text(header + body, encoding="utf-8")
    return text_path, status


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


def _find_project(slug: str) -> Project | None:
    project_md = PROJECTS_DIR / slug / "project.md"
    if not project_md.exists():
        return None
    return _load_project(project_md)


def lint_projects(
    projects: list[Project],
    canonical: dict[str, Page],
    basename_map: dict[str, list[Page]],
) -> tuple[list[str], list[str]]:
    """Validate project metadata and wiki_refs. Returns (missing_fields, broken_refs)."""
    missing: list[str] = []
    broken: list[str] = []
    for project in projects:
        rel = project.path.relative_to(ROOT).as_posix()
        gaps = sorted(key for key in PROJECT_REQUIRED_FIELDS if key not in project.frontmatter)
        if gaps:
            missing.append(f"{rel}: missing {', '.join(gaps)}")
        for ref in project.wiki_refs:
            target = normalize_link_target(ref)
            if not target or target in canonical:
                continue
            if target in SPECIAL_LINK_TARGETS:
                continue
            if "/" not in target and len(basename_map.get(target, [])) == 1:
                continue
            broken.append(f"{rel}: wiki_refs [[{ref}]]")
    return missing, broken


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
    return f"---\n" + "\n".join(new_lines) + f"\n---\n{text[end + 5:]}"


PROJECT_TEMPLATE = """---
title: {title}
type: project
status: active
created: {today}
updated: {today}
summary: One-sentence description of this project.
domain: personal
tags: []
wiki_refs: []
---

# {title}

## Description

Describe what this project is, its goals, and why you're working on it.

## Layout

This project owns its own folder structure. AI tools working from this directory
read this section to understand where things live before answering questions.

- `queries/` — durable Q&A artifacts (default landing zone).
<!-- Add your own folders here, e.g.:
- `papers/` — relevant academic papers (PDFs + extracted notes)
- `meetings/` — dated meeting notes and annotations
- `repos/` — read-only references to external repos
- `drafts/` — writing in progress
-->

## Rules

Project-specific rules agents working in this directory MUST follow. These
override the defaults in the root `AGENTS.md` (`## Working inside a project`)
when they conflict. Be specific.

<!-- Examples:
- Never summarize meeting notes from `meetings/` without asking first.
- Cite the source PDF filename whenever referencing a paper from `papers/`.
- Save query artifacts under `meetings/qa/` instead of the default `queries/`.
- Treat `repos/` as read-only — never write inside it.
- When answering design questions, prefer concepts in `wiki_refs` over general wiki search.
-->

## Key questions

Open questions you want to answer using the wiki KB.

## Context

Background, constraints, decisions to date.

## Linked wiki pages

Wikilinks to relevant concepts, sources, and topics. Add via:

```bash
python3 tools/wiki.py project link {slug} concepts/some-page
```
"""


# Claude Code shim: import the project AGENTS.md (instructions + handoff conventions)
# and project.md (deterministically — it's the per-project source of truth, so don't
# leave loading it to the agent's discretion). The root ../../AGENTS.md schema stays a
# lazy "read if needed" instruction inside AGENTS.md to avoid loading it every session.
CLAUDE_MD_TEMPLATE = "@AGENTS.md\n@project.md\n"

# Project-level AGENTS.md shim: points tools that look for AGENTS.md in the
# working directory to the root schema rather than duplicating it.
AGENTS_MD_TEMPLATE = """\
# Project Agent Context

This is a project workspace inside the Brain wiki.

Before answering questions or editing files:
1. Read `project.md` (this directory) — project description, layout, rules, current status.
2. If not already loaded: read `../../AGENTS.md` — the full wiki operating schema
   (directory contract, agents, search tools, write boundaries, project workflow conventions).

Write only inside this project directory. Never modify `wiki/` or `raw/`.

## Operating principles

- **Project context wins ties.** If a wiki page recommends approach X but `project.md` (Rules, Context, prior decisions) explicitly rules it out, propose the project-compatible alternative instead.
- **Don't fabricate around gaps.** If the wiki doesn't cover something the question requires, say so. Recommend running `wiki-ingest` (for a missing source) or `wiki-enhancer` (for shallow coverage) rather than inventing the answer.
- **Hand off to specialists when warranted.** End answers with a follow-up note when:
  - a wiki claim looks wrong vs. its source → recommend `wiki-source-verifier`
  - two wiki pages appear to disagree → recommend `wiki-contradiction-detector`
  - a referenced concept page is shallow → recommend `wiki-enhancer`
  - a needed source isn't in the wiki yet → recommend `wiki-ingest` with the candidate path
  - the question turns out to need no project context → suggest `wiki-search` instead
"""


# Per-project TODO seed. Plain checkboxes in the Obsidian Tasks plugin emoji
# format: add `⏫`/`🔺` priority, `📅 YYYY-MM-DD` due dates, etc. via the
# editor autosuggest (`obsidian-tasks-plugin` is configured for this vault).
# Mirrors `wiki/_templates/project-todo.md` (Templater), which auto-applies when
# a TODO.md is created interactively in Obsidian; this constant is used when
# the project is scaffolded via `wiki.py project new`.
TODO_TEMPLATE = """\
# {slug} TODO

- [ ]
"""


def _project_list(as_json: bool) -> int:
    projects = list_projects()
    if as_json:
        rows = [
            {
                "slug": p.slug,
                "title": p.title,
                "status": p.status,
                "summary": p.summary,
                "domain": p.domain,
                "tags": p.tags,
                "wiki_refs": p.wiki_refs,
            }
            for p in projects
        ]
        print(json.dumps(rows, indent=2))
        return 0
    if not projects:
        print(
            "No projects yet. Create one with:\n"
            "  python3 tools/wiki.py project new <slug>"
        )
        return 0
    print(f"Projects ({len(projects)}):\n")
    for project in projects:
        status = project.status or "active"
        print(f"  {project.slug:<28} [{status}]  {project.title}")
        if project.summary:
            print(f"    {project.summary[:100]}")
    return 0


def _rebuild_projects_todo() -> None:
    """Regenerate the two project-TODO aggregators.

    Writes both `projects/TODO.md` (live, embed-based for desktop Obsidian)
    and `projects/TODO-widget.md` (P1-only inlined for the iOS widget).
    Delegates to the shell script so the rebuild logic stays single-sourced.
    """
    import subprocess

    script = ROOT / "tools" / "scripts" / "rebuild-projects-todo.sh"
    subprocess.run([str(script)], check=True)


def _project_new(slug: str) -> int:
    cleaned = slug.strip().strip("/")
    if not cleaned or "/" in cleaned or cleaned.startswith("."):
        print(f"Invalid project slug: {slug!r}")
        return 1
    project_dir = PROJECTS_DIR / cleaned
    if project_dir.exists():
        print(f"Project '{cleaned}' already exists at {project_dir.relative_to(ROOT)}")
        return 1
    project_dir.mkdir(parents=True)
    (project_dir / "queries").mkdir()
    today = dt.datetime.now().strftime("%Y-%m-%d")
    title = slug_to_title(cleaned)
    (project_dir / "project.md").write_text(
        PROJECT_TEMPLATE.format(title=title, slug=cleaned, today=today),
        encoding="utf-8",
    )
    (project_dir / "CLAUDE.md").write_text(CLAUDE_MD_TEMPLATE, encoding="utf-8")
    (project_dir / "AGENTS.md").write_text(AGENTS_MD_TEMPLATE, encoding="utf-8")
    (project_dir / "opencode.json").write_text('{\n  "instructions": ["AGENTS.md"]\n}\n', encoding="utf-8")
    (project_dir / "TODO.md").write_text(
        TODO_TEMPLATE.format(slug=cleaned), encoding="utf-8"
    )
    _rebuild_projects_todo()
    print(f"Created project '{cleaned}' at {project_dir.relative_to(ROOT)}")
    print("  - project.md")
    print("  - AGENTS.md      (AI entrypoint → read project.md + ../../AGENTS.md)")
    print("  - CLAUDE.md      (Claude Code shim → @AGENTS.md + @project.md)")
    print("  - opencode.json  (opencode shim → instructions: [AGENTS.md])")
    print("  - TODO.md        (per-project todo; embedded into projects/TODO.md, P1 items surface in projects/TODO-widget.md)")
    print("  - queries/       (default Q&A artifact dir; redefine in ## Rules if you want)")
    print(
        f"\nNext steps:\n"
        f"  1. Edit projects/{cleaned}/project.md — fill in Description, Layout, and Rules.\n"
        f"  2. Create whatever subfolders this project needs (papers/, meetings/, repos/, ...).\n"
        f"  3. Link relevant wiki pages: python3 tools/wiki.py project link {cleaned} <wiki-ref>"
    )
    return 0


def _project_subfolders(project: Project) -> list[dict]:
    """Enumerate every direct subfolder of the project root, with file counts."""
    rows: list[dict] = []
    for child in sorted(project.root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        md_files = [p for p in child.rglob("*.md") if p.is_file()]
        all_files = [p for p in child.rglob("*") if p.is_file()]
        rows.append(
            {
                "name": child.name,
                "rel": str(child.relative_to(project.root)),
                "md_count": len(md_files),
                "file_count": len(all_files),
            }
        )
    return rows


def _project_show(slug: str, as_json: bool) -> int:
    project = _find_project(slug)
    if project is None:
        print(f"Project '{slug}' not found in {PROJECTS_DIR.relative_to(ROOT)}/")
        return 1
    subfolders = _project_subfolders(project)

    if as_json:
        print(
            json.dumps(
                {
                    "slug": project.slug,
                    "title": project.title,
                    "status": project.status,
                    "domain": project.domain,
                    "summary": project.summary,
                    "tags": project.tags,
                    "wiki_refs": project.wiki_refs,
                    "path": str(project.path.relative_to(ROOT)),
                    "subfolders": subfolders,
                },
                indent=2,
            )
        )
        return 0

    print(f"# {project.title}")
    print(f"Slug:    {project.slug}")
    print(f"Status:  {project.status or '(unset)'}")
    if project.domain:
        print(f"Domain:  {project.domain}")
    if project.tags:
        print(f"Tags:    {', '.join(project.tags)}")
    print(f"Summary: {project.summary}")
    print(f"Path:    {project.path.relative_to(ROOT)}")
    if project.wiki_refs:
        print(f"\nLinked wiki pages ({len(project.wiki_refs)}):")
        for ref in project.wiki_refs:
            print(f"  - [[{ref}]]")
    if subfolders:
        print(f"\nSubfolders ({len(subfolders)}):")
        for entry in subfolders:
            print(
                f"  - {entry['name']:<20} "
                f"({entry['md_count']} md / {entry['file_count']} files)"
            )
    else:
        print("\nNo subfolders yet — create them as needed for this project.")
    return 0


def _project_link(slug: str, ref: str) -> int:
    project = _find_project(slug)
    if project is None:
        print(f"Project '{slug}' not found")
        return 1
    cleaned = normalize_link_target(ref)
    if not cleaned:
        print(f"Empty wiki reference: {ref!r}")
        return 1
    existing = project.wiki_refs
    if cleaned in existing:
        print(f"'{cleaned}' already linked in project '{slug}'")
        return 0
    new_refs = existing + [cleaned]
    today = dt.datetime.now().strftime("%Y-%m-%d")
    text = project.path.read_text(encoding="utf-8")
    text = _set_frontmatter_field(text, "wiki_refs", new_refs)
    text = _set_frontmatter_field(text, "updated", today)
    project.path.write_text(text, encoding="utf-8")
    print(f"Linked '{cleaned}' to project '{slug}' ({len(new_refs)} total wiki_refs)")
    return 0


def cmd_project(
    action: str,
    slug: str | None,
    ref: str | None,
    as_json: bool,
) -> int:
    if action == "list":
        return _project_list(as_json)
    if action == "new":
        if not slug:
            print("Error: project new <slug> requires a slug")
            return 1
        return _project_new(slug)
    if action == "show":
        if not slug:
            print("Error: project show <slug> requires a slug")
            return 1
        return _project_show(slug, as_json)
    if action == "link":
        if not slug or not ref:
            print("Error: project link <slug> <wiki-ref> requires both arguments")
            return 1
        return _project_link(slug, ref)
    print(f"Unknown project action: {action}")
    return 1


def preprocess_pdfs(pdf: str | None, force: bool) -> int:
    """CLI entry: preprocess one PDF or all PDFs under raw/sources/."""
    if pdf:
        pdf_path = Path(pdf)
        if not pdf_path.is_absolute():
            pdf_path = (ROOT / pdf_path).resolve()
        targets = [pdf_path]
    else:
        if not RAW_SOURCES_DIR.exists():
            print(f"No raw/sources directory at {RAW_SOURCES_DIR}")
            return 1
        targets = sorted(RAW_SOURCES_DIR.glob("*.pdf"))

    if not targets:
        print("No PDFs found to preprocess.")
        return 0

    counts = {status: 0 for status in ExtractStatus}
    failed = 0
    for target in targets:
        try:
            text_path, status = extract_pdf_to_markdown(target, force=force)
            counts[status] += 1
            print(f"  {status.value:<9} {target.name} -> {text_path.relative_to(ROOT)}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL      {target.name}: {exc}")

    print(
        f"\nPreprocess complete. extracted={counts[ExtractStatus.EXTRACTED]} "
        f"decrypted={counts[ExtractStatus.DECRYPTED]} "
        f"skipped={counts[ExtractStatus.SKIPPED]} failed={failed}"
    )
    return 0 if failed == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Markdown wiki maintenance tools")
    sub = parser.add_subparsers(dest="command", required=True)

    lint_parser = sub.add_parser("lint", help="Validate links and metadata")
    lint_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat orphan pages as failures",
    )

    search_parser = sub.add_parser("search", help="Search wiki content")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=10, help="Max results")

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "lint":
        return lint(strict=args.strict)
    if args.command == "search":
        return search(args.query, args.limit)
    if args.command == "coverage":
        return coverage(as_json=args.json, limit=args.limit)
    if args.command == "tags":
        return tags_command(
            queries=args.tag,
            domain=args.domain,
            as_json=args.json,
            limit=args.limit,
        )
    if args.command == "validate-log":
        return validate_log()
    if args.command == "append-log":
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
        return preprocess_pdfs(pdf=args.pdf, force=args.force)
    if args.command == "project":
        return cmd_project(
            action=args.action,
            slug=args.slug,
            ref=args.ref,
            as_json=args.json,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
