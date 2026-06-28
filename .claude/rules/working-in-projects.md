---
paths:
  - "projects/**"
---

# Working inside a project (projects/<slug>/)

Follow these on top of `## Rules` in the project's `project.md` — project rules win on conflict.

**Wiki search ladder** — try in order, stop when you have enough:

1. `qmd query "<question>" --format json` — hybrid BM25 + vector + LLM rerank; best for conceptual questions.
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

**AGENDA.md** is the autonomous-runner agenda — agent-managed task state in `key:: value` form.
Its mechanical transitions (`last_run`/`next_due`/`status: done`, resolving clarifications) go
through `python3 tools/wiki.py project agenda …`, not hand edits. Do not put runner tasks in
`TODO.md` (that is the operator's Obsidian Tasks list, a separate system).

**Resolving runner clarifications (do this from a plain request — no command name needed).** When the
operator asks to "answer / sort out / clear the runner's questions" for a project (or just asks about
what the runner flagged), this is the flow — you do not need them to invoke any skill:
1. `python3 tools/wiki.py project agenda clarifications [--json]` — list the `needs-clarification`
   tasks and their open questions (scope to the named project).
2. Ask the operator the open questions (one task at a time; batch a task's questions together).
3. Edit the task's `### [id]` block in `AGENDA.md` so it is now executable: rewrite `acceptance::`
   into a single objective line, fix `schedule::`/`output::`/`notes::` as answered. Do **not**
   hand-edit `status`/`next_due`/the `questions` block or the `## Clarifications` entry.
4. `python3 tools/wiki.py project agenda resolve <slug> <id>` — flips it to `clear`, sets `next_due`,
   removes the questions + clarification entry, logs it. The next nightly run then executes it.
The `/project-clarify` skill is just a shortcut for this same flow; the operator never has to name it.
