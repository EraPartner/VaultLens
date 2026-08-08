---
paths:
  - "wiki/**/*.md"
---

# Writing and editing wiki pages

## Required frontmatter

All content pages need at least: `title`, `type` (page/source/entity/concept/topic/synthesis/
comparison/query/project/inventory), `status` (active/superseded/archived/draft), `created`
(`YYYY-MM-DD`), `updated`, `summary` (one falsifiable sentence). Optional: `domain`
(personal/research/work/learning), `tags`, `confidence` (high/medium/low — evidential trust),
`volatility` (hot/warm/cold — refresh cadence; drives staleness thresholds 60/180/365 days).

- Analytical pages (`concepts/`, `topics/`, `syntheses/`, `comparisons/`) should set `confidence`
  and `volatility`; `lint` validates the values and flags low-confidence pages for follow-up.
- `wiki/sources/*` also need `source_id` (e.g. `src-2026-04-11-001`), `source_type`
  (article/paper/book/pdf/video/podcast/dataset/note/other), `origin`, `ingested_on`.

## Links and citations

Write Obsidian path-based wikilinks `[[path/to/page]]` — the canonical form. In a `## Sources`
section, concept/topic pages cite the source *page* (`[[sources/...]]`), while a **source page**
(`wiki/sources/src-*.md`) cites its immutable raw material: `- Source text:
[[raw/sources-text/<stem>]]` (always present, linked without the `.md`) and, when a PDF exists,
`- Source PDF: [[raw/sources/<stem>.pdf]]`.

`raw/` wikilinks to real files are validated by `lint`. Filenames containing `[`/`]` use an
angle-bracket markdown link `[Label](<../../raw/...>)`, since Obsidian wikilinks cannot contain `]`.

Keep external URLs on source pages and reference sources indirectly from concept/topic pages.

For portability outside Obsidian (GitHub, plain-markdown viewers, headless agents) wikilinks carry
a **dual-link** markdown mirror — `[[concepts/foo]] ([Foo Title](../concepts/foo.md))`. Do not
hand-write the `([Title](path.md))` mirror; relative paths are error-prone. Write the bare
`[[...]]` and run `python3 tools/wiki.py links --fix --write`, which adds mirrors deterministically
and idempotently. `python3 tools/wiki.py links` reports coverage without writing.

## Change quality

Preserve validated content unless superseded by stronger evidence; mark superseded claims
`status: superseded` rather than silently deleting history. Keep summaries concise and falsifiable.
Favor incremental edits across related pages over isolated notes. Bump `updated` when editing.

## Archiving

Retire pages with `archive page <ref> --reason "…"` — never delete. Archived pages keep wikilinks
resolving but drop out of staleness/orphan checks and `search`. Full semantics:
`.claude/skills/wiki-maintenance/SKILL.md`.

## Index and log

`wiki/index.md` (Dataview) updates itself inside Obsidian. The derived `_index.md` mirrors are
**generated — never hand-edit**; regenerate via the maintenance skill after adding or removing
pages. `wiki/log.md` is append-only; headings are `## [YYYY-MM-DD] operation | title`.

## Obsidian behavior

Templater auto-applies the matching `wiki/_templates/` template when a file is created in a `wiki/`
subfolder; Dataview tables update automatically from frontmatter (JS API enabled).
