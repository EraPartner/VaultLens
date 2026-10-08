---
paths:
  - "projects/**"
---

# Working inside a project (projects/<slug>/)

Follow these on top of `## Rules` in the project's `project.md` — project rules win on conflict.

**Wiki search ladder** — try in order, stop when you have enough:

1. The scoped `mcp__qmd__*` tools when available.
2. `qmd search "<keywords>"` over the run's approved note selection.
3. `qmd query "<question>" --format json`. It ranks with the operator's qmd index when the
   launcher enables it and returns only approved notes; otherwise it is lexical, and its
   `fallback` field says why.

The local runtime searches only approved notes. The sandbox never opens a shared full-vault
index or downloads embedding models; qmd runs on the host and returns approved paths only. Excluded pages are unknown; request a
separate, reviewed access profile when they are needed. Hybrid full-vault qmd search is
an explicit operator workflow outside this scoped agent run.

**Citation discipline** — every load-bearing claim carries an inline wikilink
(`[[concepts/some-page]]`) to the wiki page that backs it. Mark anything not wiki-backed as
`[outside wiki — agent inference]`. Unmarked claims are treated as general knowledge.

**Saving durable Q&A** — when an answer captures a non-trivial decision/design/analysis the project
will reference later, save it to `projects/<slug>/queries/YYYY-MM-DD-<topic>.md` (unless `## Rules`
overrides) with frontmatter (`type: query`, inherit project `tags`, list cited `wiki_refs`) and body
`## Question` / `## Answer` (inline wikilinks) / `## Sources` / `## Follow-ups`. Skip the artifact
for trivial one-line Q&A.

**Write boundary** — a project session writes only inside `projects/<slug>/`; never modify `wiki/`
or `raw/` (recommend a `wiki-enhancer` / `wiki-ingest` follow-up instead when wiki coverage is
lacking). Projects may *reference* any wiki page via wikilinks. `lint` validates `wiki_refs`
against the wiki page set and checks projects for required frontmatter and broken refs; body
content (Layout, Rules) is free-form.

**AGENDA.md** is the autonomous-runner agenda — agent-managed task state in `key:: value` form.
Its mechanical transitions (`last_run`/`next_due`/`status: done`, resolving clarifications) go
through `python3 tools/wiki.py project agenda …`, not hand edits. Do not put runner tasks in
`TODO.md` (that is the operator's Obsidian Tasks list, a separate system).

**Frozen projects** — `status: frozen` in `project.md` excludes the project from all current-work
surfaces and scheduled routing. Use `python3 tools/wiki.py project freeze <slug>` or `unfreeze
<slug>` from the vault root; do not hand-edit the status because the commands also refresh TODO and
deadline views.

**Resolving runner clarifications.** A plain request to answer or clear the runner's questions
triggers `.agents/skills/wiki-project-clarify/SKILL.md`. That skill is the canonical interview and
transition procedure; the operator does not need to name it.
