"""Relative coverage ranking (temporary) — reuses wiki_query scoring without the
absolute zero-cutoff so the weakest concept/topic pages resurface."""
import json
from wiki import build_page_indexes, compute_inbound_links, list_content_pages, normalize_link_target
from wiki_query import _body_word_count

pages = list_content_pages()
canonical, basename_map = build_page_indexes(pages)
inbound, _b, _a = compute_inbound_links(pages, canonical, basename_map)

rows = []
for page in pages:
    if page.category not in {"concepts", "topics"}:
        continue
    if getattr(page, "is_archived", False):
        continue
    key = page.rel.with_suffix("").as_posix()
    wc = _body_word_count(page.body)
    outb = len(page.links)
    inb = inbound.get(key, 0)
    concept_count = 0
    if page.category == "topics":
        concept_count = sum(1 for raw in page.links if normalize_link_target(raw).startswith("concepts/"))
    rows.append({
        "path": page.rel.as_posix(),
        "category": page.category,
        "title": page.title,
        "words": wc,
        "inbound": inb,
        "outbound": outb,
        "concept_count": concept_count,
    })

rows.sort(key=lambda r: (r["words"], r["inbound"], r["outbound"]))
print(json.dumps(rows[:40], indent=2))
print("=== TOTAL concept+topic pages:", len(rows))
