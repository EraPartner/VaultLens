---
title: Enhancement Strategies
type: page
status: active
created: 2026-06-29
updated: 2026-06-29
summary: Names the source-first vs topic-first axis that governs how the wiki grows, so coverage decisions are auditable rather than implicit.
tags: [system, enhancement, coverage]
---

# Enhancement Strategies

How the wiki grows is not arbitrary, but until now the governing principle was never written down.
This page names it. The `wiki-enhancer` agent (`.claude/agents/wiki-enhancer.md`) selects what to
work on with one of five strategies; underneath them sits a single organizing axis.

## The axis: source-first vs topic-first

- **Source-first (sweep).** Pick one ingested source, walk its structure, and create or deepen every
  page it should produce — MISSING, then BAD, then THIN — until that source is well represented. This
  is `wiki-enhancer` **Strategy D** (source-driven gap discovery). In practice it runs as a *sweep*: a
  textbook is selected and its pages are filled across several back-to-back sessions until it is
  "complete." This has been the de-facto dominant mode, which is why the wiki's shape tracks **which
  sources were swept** rather than which topics matter most to the operator.
- **Topic-first.** Start from a topic (a personal-priority area, a sparse cluster, a stub) and enhance
  it across whatever sources cover it. This is **Strategy C** (sparse coverage, by score) and
  **Strategy A** (shallowest stub). It makes the wiki track **what the operator wants to know**.

Both are legitimate. They produce different shapes: source-first gives broad, even coverage of whatever
has been ingested; topic-first gives deep coverage of chosen areas. A wiki built only source-first is a
catalog of its bookshelf; one built only topic-first leaves ingested sources half-mined.

## Selection strategies (the five the agent offers)

| Strategy | Mode | Picks by |
|---|---|---|
| A — shallowest stub | topic-first | fewest-line page (`wc -l`) |
| B — random page | either | uniform random page |
| C — sparse coverage | topic-first | lowest `wiki.py coverage` score |
| D — source-gap discovery | source-first | a source's structure vs current wiki |
| E — mixed / agent-chosen | either | agent judgement |

## Keeping coverage auditable

- **State the mode in the log.** Every enhance `wiki/log.md` entry should make the mode legible — for
  a sweep, name the source and that it is source-first; for topic-first, name the topic and why.
- **Balance the axis deliberately.** A run of pure source-first sweeps grows the bookshelf; interleave
  topic-first passes (Strategy C/A) on operator-priority areas so the wiki stays a knowledge map, not a
  textbook index.
- This complements the [Page Content Standard](schema.md) (one worked example per page): that governs
  *how dense* a page is; this governs *which pages exist at all*.

## Sources

- `.claude/agents/wiki-enhancer.md` — the strategy definitions (A–E) this page names.
- `../reports/scheduled-emerge-2026-06-29.md` — the emerge run that surfaced the unnamed source-sweep pattern.
