#!/usr/bin/env python3
"""Read-only query surfaces over the wiki: search, coverage, tags.

These commands never mutate the wiki; they rank and report. `search` is a
term-frequency keyword search, `coverage` ranks sparse/underlinked pages for the
enhancer agent, and `tags` lists or filters by frontmatter tags. All three build
on the shared page model and link indexes in `wiki.py`.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from wiki import (
    Page,
    build_page_indexes,
    compute_inbound_links,
    list_content_pages,
    normalize_link_target,
)


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

# Categories that never carry analytical coverage weight (skipped on both paths).
# `reports/` holds scheduled-agent + lint/audit outputs — dated, unlinked, and
# off-limits to the enhancer — so they must not surface as coverage targets even
# though they trip every floor (short, 0 inbound, 0 outbound).
COVERAGE_SKIP_CATEGORIES = {"system", "root", "entities", "inventory", "reports"}
# The relative fallback only ranks the page kinds the enhancer is meant to deepen.
COVERAGE_RELATIVE_CATEGORIES = {"concepts", "topics"}


def _page_metrics(page, inbound: dict[str, int]) -> dict:
    """Per-page weakness metrics shared by both ranking paths."""
    key = page.rel.with_suffix("").as_posix()
    word_count = _body_word_count(page.body)
    outbound = len(page.links)
    inbound_count = inbound.get(key, 0)
    concept_count = 0
    if page.category == "topics":
        concept_count = sum(
            1
            for raw in page.links
            if normalize_link_target(raw).startswith("concepts/")
        )
    return {
        "path": page.rel.as_posix(),
        "category": page.category,
        "title": page.title,
        "words": word_count,
        "inbound": inbound_count,
        "outbound": outbound,
        "concept_count": concept_count,
    }


def rank_coverage(pages, inbound: dict[str, int], limit: int) -> tuple[list[dict], str]:
    """Rank coverage targets, returning ``(rows, mode)``.

    ``mode`` is ``"absolute"`` when at least one page trips an absolute floor
    (those pages — and only those — are returned, ranked by descending score),
    or ``"relative"`` when no page trips a floor and we fall back to a relative
    bottom-N weakness ranking over concepts/topics (excluding archived pages),
    sorted ascending by word count, then inbound, then outbound links. The
    relative path guarantees a non-empty result on any non-trivial corpus.

    Pure: no I/O, deterministic ordering. The CLI printer and the test suite
    both call this so they rank identically.
    """
    rows: list[dict] = []
    for page in pages:
        if page.category in COVERAGE_SKIP_CATEGORIES:
            continue
        metrics = _page_metrics(page, inbound)
        word_count = metrics["words"]
        outbound = metrics["outbound"]
        inbound_count = metrics["inbound"]
        concept_count = metrics["concept_count"]

        shallow = word_count < SHALLOW_WORD_THRESHOLD
        underlinked = inbound_count < 2
        isolated = outbound < 2
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

        rows.append(
            {
                **metrics,
                "sparse_topic": sparse_topic,
                "shallow": shallow,
                "underlinked": underlinked,
                "isolated": isolated,
                "score": score,
                "origin": page._scalar("origin") if page.category == "sources" else "",
            }
        )

    if rows:
        rows.sort(key=lambda row: (-row["score"], row["path"]))
        return (rows[:limit] if limit > 0 else rows), "absolute"

    # Relative fallback: no page is starved by the absolute floors, but the
    # enhancer still needs targets. Rank the weakest concepts/topics so the
    # loop never silently degrades to a semantically blind wc -l ranking.
    weak: list[dict] = []
    for page in pages:
        if page.category not in COVERAGE_RELATIVE_CATEGORIES:
            continue
        if page.is_archived:
            continue
        weak.append(_page_metrics(page, inbound))

    weak.sort(
        key=lambda row: (row["words"], row["inbound"], row["outbound"], row["path"])
    )
    return (weak[:limit] if limit > 0 else weak), "relative"


def coverage(as_json: bool, limit: int) -> int:
    """Report sparse-coverage targets for the enhancer agent.

    Two ranking paths (see ``rank_coverage``), selected automatically:

    1. ABSOLUTE (``mode == "absolute"``) — pages tripping a hard floor:
       - Body word count (shallow < 300 words)
       - Inbound wikilink count (underlinked < 2)
       - Outbound wikilink count (isolated < 2)
       - For topics: concept-page count mentioned in body (< 5)
       Only flagged pages are returned, ranked by descending weakness score.

    2. RELATIVE (``mode == "relative"``) — fallback when no page trips a floor.
       A mature, densely linked corpus clears every absolute floor, which used
       to make this command return nothing; the enhance loop then silently fell
       back to a semantically blind ``wc -l`` ranking. Instead we now rank the
       weakest concepts/topics (skipping archived pages) ascending by word
       count, then inbound, then outbound links, so the enhancer always gets a
       meaningfully-ordered, non-empty target list.

    The chosen path is reported via the ``mode`` field in JSON output and a
    one-line note in the human-readable output. When falling back, the note
    reads: "All pages clear the absolute floors; showing the relative
    bottom-N weakest concepts/topics."
    """
    pages = list_content_pages()
    canonical, basename_map = build_page_indexes(pages)
    inbound, _broken, _ambiguous = compute_inbound_links(pages, canonical, basename_map)

    top, mode = rank_coverage(pages, inbound, limit)

    if as_json:
        print(json.dumps([{**row, "mode": mode} for row in top], indent=2))
        return 0

    if mode == "relative":
        print(f"Coverage candidates (relative fallback, top {len(top)} weakest):\n")
        print(
            "All pages clear the absolute floors; showing the relative "
            "bottom-N weakest concepts/topics."
        )
        print(f"{'words':>5}  {'in':>3}  {'out':>3}  {'concepts':>8}  category   path")
        for row in top:
            print(
                f"{row['words']:>5}  {row['inbound']:>3}  {row['outbound']:>3}  "
                f"{row['concept_count']:>8}  {row['category']:<9}  {row['path']}"
            )
        return 0

    print(f"Coverage candidates (absolute floors, top {len(top)} flagged):\n")
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


def search(query: str, limit: int, include_archived: bool = False) -> int:
    terms = [token for token in re.findall(r"[A-Za-z0-9_\-]+", query.lower()) if token]
    if not terms:
        print("No query terms found")
        return 1

    pages = list_content_pages()
    if not include_archived:
        pages = [page for page in pages if not page.is_archived]
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


def tags_command(queries: list[str], domain: str, as_json: bool, limit: int) -> int:
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
