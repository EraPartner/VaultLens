---
title: Example Project
type: project
status: active
created: 2026-05-04
updated: 2026-05-04
summary: Starter project showing the projects/ layer in practice. Rename or delete once you're ready to make your own.
domain: personal
tags: [example, starter]
wiki_refs: []
---

# Example Project

## Description

This is a placeholder project showing how `projects/<slug>/` works.
It demonstrates the bespoke folder structure (`papers/`, `meetings/`, `repos/`,
`drafts/`, `queries/`) and how the `## Layout` and `## Rules` sections steer the
`project-assistant` agent. Copy or rename this folder to start a real project,
or delete it with `rm -rf projects/example` when you don't need it anymore.

## Layout

- `papers/` — relevant academic papers (PDFs + a sibling `<paper-slug>.md` with notes)
- `meetings/` — dated meeting notes (e.g. `meetings/2026-04-12-supervisor.md`)
- `repos/` — read-only references to external code (forks, submodules, surveyed projects)
- `drafts/` — writing in progress (chapter drafts, blog posts, talks)
- `queries/` — durable Q&A artifacts saved by `project-assistant` (default)

## Rules

The `project-assistant` agent MUST follow these rules. Project rules override
the agent's defaults when they conflict.

- Treat everything under `repos/` as read-only — never write inside it.
- When citing a paper from `papers/`, include the source PDF filename.
- For meeting questions, quote the relevant meeting note line + date; do not paraphrase silently.
- Prefer concepts in `wiki_refs` over general wiki search when answering design questions.
- Save durable Q&A under `queries/YYYY-MM-DD-<topic>.md` (default).

## Key questions

- What's the cleanest way to use the `projects/` layer in this vault?
- How do `## Layout` and `## Rules` shape the agent's behaviour?

## Context

This project was scaffolded as a starter. It's not real work — feel free to
delete it once you've confirmed the flow works.

## Linked wiki pages

Add via:

```bash
python3 tools/wiki.py project link example concepts/some-page
```
