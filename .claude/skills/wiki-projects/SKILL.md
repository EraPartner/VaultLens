---
name: wiki-projects
description: Create and manage projects/ workspaces — scaffold a new project, link wiki pages to it, inspect project metadata, keep project.md current, manage per-project TODO.md. Use when the user wants a new project, asks to link/show project info, finishes a session that changed project state, or edits project TODO items.
---

# Projects layer — scaffolding & lifecycle

`projects/` consumes the wiki as a knowledge base. Each subfolder is one project workspace that
owns its structure; the user adds whatever else the project needs (`papers/`, `meetings/`,
`repos/`, `drafts/`, `data/`, …).

## Commands

```bash
python3 tools/wiki.py project list               # enumerate projects
python3 tools/wiki.py project new <slug>         # scaffold project.md + shims + queries/
python3 tools/wiki.py project show <slug>        # details (--json for machine output)
python3 tools/wiki.py project link <slug> concepts/some-page   # append wiki_ref + bump updated
```

Never hand-edit `wiki_refs` frontmatter — `project link` preserves YAML and bumps `updated`.

## Scaffolded structure

```
projects/<slug>/
  project.md      ← metadata + Description + Layout + Rules + Key questions + Context + linked wiki pages
  CLAUDE.md       ← AI entrypoint: @project.md + operating principles (root schema auto-loads)
  TODO.md         ← per-project todo, embedded into projects/TODO.md
  AGENDA.md       ← dormant autonomous-runner agenda (opt-in via enabled flag)
  queries/        ← default Q&A landing zone (overridable in ## Rules)
```

## Project page schema

```yaml
---
title: <Human Title>
type: project
status: active            # active | paused | archived
created: YYYY-MM-DD
updated: YYYY-MM-DD
summary: <one-sentence description>
domain: personal          # or research / work / learning
tags: [<scope tags>]
wiki_refs: [<concepts/foo, topics/bar, sources/src-...>]
---

# <Title>
## Description
## Layout                 ← what each subfolder contains; the agent reads this first
## Rules                  ← project-specific rules the agent MUST follow
## Key questions
## Context
## Linked wiki pages
```

`wiki_refs` and `tags` are load-bearing: agents use them to scope which wiki pages to pull into
context. `## Rules` overrides the root schema's "Working inside a project" defaults when they
conflict. If `## Layout` is missing, fall back to `ls`/`find`.

## Keeping project.md current

`project.md` is the model-agnostic source of truth. After any session that establishes new
information (meeting outcome, decision, direction change, completed deliverable, new deadline),
update the relevant sections before ending: Current status/deliverables, Key questions, Context,
Description/research question, Planning. Edit only sections that changed; small targeted updates;
bump `updated`.

## TODO files

Per-project `TODO.md` uses the Obsidian Tasks plugin emoji format (priority 🔺/⏫/🔼/🔽/⏬, dates
📅/🛫/⏳). Three aggregators surface them — read but never hand-edit: `projects/TODO.md` (desktop
embeds; gitignored/generated; the scaffold appends an embed line per `project new`),
`projects/TODO-widget.md` (flattened copy for the iOS widget; gitignored/generated),
`projects/deadlines.md` (live Tasks-plugin query of upcoming dated items; desktop only; tracked).

## AGENDA files (autonomous nightly runner)

Every project carries an `AGENDA.md` (scaffolded by `project new`; backfill existing projects with
`project agenda scaffold-all`). It is **dormant by default** — `enabled: false` in its frontmatter.
Flip to `enabled: true` to opt the project into the nightly `project-runner` agent (a writer in the
scheduled batch; see `tools/schedule/SPEC.md` and `.claude/agents/wiki-project-runner.md`).

Structure (single file, distinct from `TODO.md` so it never pollutes the Obsidian Tasks widgets):

- **frontmatter** — `enabled` (opt-in flag), `runner_scope` (`edits`/`artifacts`/`research` the
  runner may do), `max_tasks_per_run` (per-night cap).
- **`## Inbox`** — dump tasks here loosely, any format; the runner grooms them out each night.
- **`## Tasks`** — one `### [id] <title>` block per task with `key:: value` lines (Dataview inline
  fields): `status::` (`clear`/`needs-clarification`/`blocked`/`done`/`paused`), `schedule::`
  (`once`/`nightly`/`weekly:Mon`/`every:3d`/`weekdays:Mon,Wed,Fri`), `last_run::`, `next_due::`,
  `acceptance::`, `output::`, plus `questions::`/`blocked_reason::` when relevant.
- **`## Clarifications`** — open questions surfaced for `/project-clarify`.
- **`## Run log`** — append-only audit trail.

The runner edits the working tree but **never commits**; the dispatcher snapshots
`projects/<slug>/` to `~/.brain/project-snapshots/<date>/` before each run, and the morning roll-up
(`wiki/reports/scheduled-project-runner-<date>.md`) carries the restore command (since `projects/`
is gitignored, the snapshot — not git — is the undo).

`project agenda <sub>` subcommands (CLI is `tools/agenda.py`; pure-python, host-runnable):

```bash
python3 tools/wiki.py project agenda scaffold-all        # backfill dormant AGENDA.md everywhere
python3 tools/wiki.py project agenda enable <slug>       # opt in (set enabled: true) / disable to opt out
python3 tools/wiki.py project agenda status [<slug>]     # enabled flag + task counts + due count
python3 tools/wiki.py project agenda due [--json]        # clear+due tasks across enabled projects
python3 tools/wiki.py project agenda clarifications [--json]   # open questions across all projects
python3 tools/wiki.py project agenda lint [<slug>]       # validate task fields + schedule grammar
python3 tools/wiki.py project agenda ack <slug>          # "I reviewed the edits" — resume after the stacking pause
```

State transitions are mechanical — `complete <slug> <id>` (advance after execution),
`resolve <slug> <id>` (clarification → clear), `new-id <slug>` — and are called by the runner /
the `/project-clarify` skill, not hand-edited. After ≥2 nights of unreviewed edits a project is
paused until `ack`.
