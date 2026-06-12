---
paths:
  - "projects/**"
---

# Working inside a project (projects/<slug>/)

Follow these on top of `## Rules` in the project's `project.md` — project rules win on conflict.

**Wiki search ladder** — try in order, stop when you have enough:

1. `qmd query "<question>" --json` — hybrid BM25 + vector + LLM rerank; best for conceptual questions.
2. `qmd search "<keywords>"` — BM25 only; fast, free, good for exact terms.
3. `python3 tools/wiki.py search "<query>"` — substring fallback when qmd is unavailable.
4. `python3 tools/wiki.py tags <tag> [<tag>...]` — AND-filter wiki pages by the project's `tags`.

If `mcp__qmd__*` tools are exposed in the session, prefer them over the CLI.

> **In the devcontainer (no GPU): invert the ladder — lead with `qmd search` (BM25, instant).**
> `qmd query`/`vsearch` and `mcp__qmd__query` run an LLM expand+embed+rerank pipeline that costs
> 30s+ on 4 CPU cores and currently stalls for minutes because the snapshot index's chunks are
> stamped with an uncached embed model (`embeddinggemma-300M`) that qmd then tries to fetch through
> the locked egress. Fix by re-embedding on the host (`qmd embed`, Metal) so chunks re-stamp to the
> cached Qwen3 model; it propagates to the container on next start.

**Citation discipline** — every load-bearing claim carries an inline wikilink
(`[[concepts/some-page]]`) to the wiki page that backs it. Mark anything not wiki-backed as
`[outside wiki — agent inference]`. Unmarked claims are treated as general knowledge.

**Saving durable Q&A** — when an answer captures a non-trivial decision/design/analysis the project
will reference later, save it to `projects/<slug>/queries/YYYY-MM-DD-<topic>.md` (unless `## Rules`
overrides) with frontmatter (`type: query`, inherit project `tags`, list cited `wiki_refs`) and body
`## Question` / `## Answer` (inline wikilinks) / `## Sources` / `## Follow-ups`. Skip the artifact
for trivial one-line Q&A.

**Write boundary** — a project session writes only inside `projects/<slug>/`; never modify `wiki/`
or `raw/` (recommend `wiki-enhancer` / `wiki-ingest` follow-ups instead).
