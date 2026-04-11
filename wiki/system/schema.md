---
title: Schema
type: page
status: active
created: 2026-04-11
updated: 2026-04-11
summary: Operating conventions and schema for this wiki.
---

# Operating Schema

The authoritative schema is at vault root: `../AGENTS.md`

## Quick Reference

### Required Frontmatter

All content pages need:

```
---
title: ...
type: page
status: active|superseded|archived|draft
created: YYYY-MM-DD
updated: YYYY-MM-DD
summary: One sentence.
---
```

### Source Pages

Additional required fields:

```
source_id: src-YYYY-MM-NNN
source_type: article|paper|book|video|podcast|dataset|note|other
origin: URL or publication
ingested_on: YYYY-MM-DD
```

### Categories

| Directory | Purpose | Example |
|-----------|---------|---------|
| `sources/` | Per-source summaries | `src-2026-04-11-001.md` |
| `entities/` | People, orgs, tools, places | `entities/openai.md` |
| `concepts/` | Methods, theories, ideas | `concepts/attention.md` |
| `topics/` | Thematic syntheses | `topics/llm-architecture.md` |
| `syntheses/` | Cross-topic analyses | `syntheses/ai-safety-frameworks.md` |
| `comparisons/` | Side-by-side analyses | `comparisons/gpt-vs-claude.md` |
| `queries/` | Preserved Q&A | `queries/transformer-training.md` |
| `reports/` | Lint outputs, audits | `reports/lint-2026-04-11.md` |

### Link Conventions

- Use wikilinks: `[[sources/my-source]]` or `[[entities/openai]]`
- Prefer explicit path-based links when ambiguous
- Source citations in `## Sources` section
- Template examples in `wiki/_templates/` are placeholders - copy and customize

### Additional Types

#### Topics
Synthesize multiple concepts and sources into thematic pages.

```
---
title: ...
type: topic
status: active|draft
created: YYYY-MM-DD
updated: YYYY-MM-DD
summary: One sentence synthesis.
---
```

#### Syntheses
Cross-topic analyses with thesis and evidence structure.

```
---
title: ...
type: synthesis
status: draft
created: YYYY-MM-DD
updated: YYYY-MM-DD
summary: One sentence thesis.
---
```

#### Comparisons
Side-by-side analyses with dimension tables.

```
---
title: ...
type: comparison
status: draft
created: YYYY-MM-DD
updated: YYYY-MM-DD
summary: One sentence comparison.
comparisons:
  - item_a
  - item_b
---
```

#### Reports
Lint outputs and audit results.

```
---
title: Report Name
type: report
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
summary: One sentence description.
report_type: lint|audit|other
---
```