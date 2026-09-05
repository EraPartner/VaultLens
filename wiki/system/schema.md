---
title: Schema
type: page
status: active
created: 2026-04-11
updated: 2026-09-05
summary: Reader-facing content standards with pointers to the canonical wiki schema.
---

# Wiki Content Standard

This page is a reader-facing guide to content quality. It does not duplicate the
machine-checked metadata schema.

- Required frontmatter, source fields, page types, links, citations, archiving,
  and index rules are canonical in `wiki/AGENTS.md`.
- The vault architecture and agent operating rules are canonical in `AGENTS.md`
  at the repository root.
- Ready-to-copy page shapes live in `wiki/_templates/`.

Keeping those contracts in one place prevents identifier and enum drift. In
particular, source IDs use `src-YYYY-MM-DD-NNN`, and `pdf` is a valid
`source_type`, as defined in `wiki/AGENTS.md`.

## Page Content Standard

Wiki pages are a **knowledge map and dense reference**, not a study guide:

- Use at most one worked example per page. One concrete example may illustrate
  the mechanism. Cover further variation in prose under `How It Works` or
  `Variants`.
- Favour encyclopedic density: definitions, properties, mechanisms, nuances,
  citations, and cross-links.
- Preserve validated content. Mark superseded claims instead of silently
  deleting their history.

The `wiki-enhancer` role applies this standard to pages it creates or rewrites.
Older pages with surplus examples remain a deliberate manual cleanup task; they
are not bulk-rewritten automatically.

See [[system/enhancement-strategies]] ([Enhancement Strategies](enhancement-strategies.md))
for the companion standard that governs which pages should be created or
expanded.
