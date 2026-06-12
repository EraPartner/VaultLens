---
name: wiki-ingest
description: Add source material to the wiki — ingest a PDF, article, paper, video transcript, or any file from raw/inbox/ into wiki/ source+concept pages. Use when the user says "ingest this", "add this to the wiki/brain", drops a file or URL to capture, or asks to process the inbox.
---

# Ingest flow (raw/ → wiki/)

`raw/` is immutable source-of-truth; ingest never modifies it in place.

1. **Place the material.** New files land in `raw/inbox/` (or directly `raw/sources/` when final).
   Items needing human review first go to `raw/review-inbox/`. URLs: extract clean markdown with
   the `obsidian:defuddle` skill before placing it in `raw/inbox/`.
2. **PDFs:** first-class sources — the model reads them directly. For large/complex PDFs,
   pre-extract text: `python3 tools/wiki.py preprocess` (writes `raw/sources/*.pdf` →
   `raw/sources-text/*.md`).
3. **Run the ingest agent** (preferred): `python3 tools/agents/wiki-agent.py ingest --source raw/sources/x.pdf`
   — on the host invoke via `brain-wiki ingest …` (wiki-agent.py refuses to run on the host directly).
   It does extraction, source-page creation, concept/topic updates, lint, and the log entry.
4. **Manual flow** (when doing it inline): create `wiki/sources/src-YYYY-MM-DD-NNN.md`
   (`python3 tools/wiki_extra.py next-id` for the ID) with required frontmatter — the base set
   (`title`, `type: source`, `status`, `created`, `updated`, `summary`) **plus** `source_id`,
   `source_type` (article/paper/book/pdf/video/podcast/dataset/note/other), `origin`, `ingested_on`.
   Cite the raw material from the source page: `- Source text: [[raw/sources-text/<stem>]]` (always)
   and `- Source PDF: [[raw/sources/<stem>.pdf]]` when a PDF exists. Update/create the related
   `wiki/concepts/` and `wiki/topics/` pages (set `confidence` + `volatility`); concept/topic pages
   cite the source *page* (`[[sources/...]]`), never raw/ directly.
5. **Finish:** write bare `[[...]]` wikilinks then run `python3 tools/wiki.py links --fix --write`
   (adds the portable markdown mirrors deterministically — never hand-write them); append a
   `wiki/log.md` entry (`python3 tools/wiki.py append-log …`, heading format
   `## [YYYY-MM-DD] operation | title`); run `python3 tools/wiki.py lint`; re-index search with
   `qmd update`.

Track not-yet-ingested intentions in the inventory instead of leaving loose notes:
`python3 tools/wiki.py inventory new ingest-candidate <slug> --priority p2 --summary "…"`.

Related: `.claude/skills/wiki-agents/SKILL.md` (choosing agents) ·
`.claude/skills/wiki-maintenance/SKILL.md` (lint/index/links commands).
