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
        if page.category in {"system", "root", "entities", "inventory"}:
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
