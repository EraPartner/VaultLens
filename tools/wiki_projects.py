#!/usr/bin/env python3
"""Project workspaces that consume the wiki KB.

A project lives under `projects/<slug>/` with its own `project.md` (the source of
truth), provider-neutral AGENTS.md instructions, a CLAUDE.md compatibility shim,
and a TODO.md that feeds
the aggregated `projects/TODO.md`. This module scaffolds new projects from
templates and manages their `wiki_refs`. The `Project` dataclass and
`list_projects` loader live in `wiki.py` (shared with the linter).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess

import agenda
from project_state import is_frozen_project

from wiki import (
    PROJECTS_DIR,
    ROOT,
    Project,
    _load_project,
    _set_frontmatter_field,
    list_projects,
    normalize_link_target,
    slug_to_title,
)


def _find_project(slug: str) -> Project | None:
    project_md = PROJECTS_DIR / slug / "project.md"
    if not project_md.exists():
        return None
    return _load_project(project_md)


PROJECT_TEMPLATE = """---
title: {title}
type: project
status: active
created: {today}
updated: {today}
summary: One-sentence description of this project.
domain: personal
tags: []
wiki_refs: []
---

# {title}

## Description

Describe what this project is, its goals, and why you're working on it.

## Layout

This project owns its own folder structure. AI tools working from this directory
read this section to understand where things live before answering questions.

- `queries/` — durable Q&A artifacts (default landing zone).
<!-- Add your own folders here, e.g.:
- `papers/` — relevant academic papers (PDFs + extracted notes)
- `meetings/` — dated meeting notes and annotations
- `repos/` — read-only references to external repos
- `drafts/` — writing in progress
-->

## Rules

Project-specific rules agents working in this directory MUST follow. These
override the defaults in the root `AGENTS.md` (`## Working inside a project`)
when they conflict. Be specific.

<!-- Examples:
- Never summarize meeting notes from `meetings/` without asking first.
- Cite the source PDF filename whenever referencing a paper from `papers/`.
- Save query artifacts under `meetings/qa/` instead of the default `queries/`.
- Treat `repos/` as read-only — never write inside it.
- When answering design questions, prefer concepts in `wiki_refs` over general wiki search.
-->

## Current status

Where the project stands right now: current phase, recent outcomes, what's in
progress. Update this section (and bump `updated`) at the end of any session that
changes project state; granular tasks live in `TODO.md`.

## Key questions

Open questions you want to answer using the wiki KB.

## Context

Background, constraints, decisions to date.

## Linked wiki pages

Wikilinks to relevant concepts, sources, and topics. Add via:

```bash
python3 tools/wiki.py project link {slug} concepts/some-page
```
"""


# Project AGENTS.md: Codex discovers this file from the directory hierarchy.
# It explicitly requires reading project.md because AGENTS.md has no import syntax.
AGENTS_MD_TEMPLATE = """\
# Project Agent Context

This is a project workspace inside the VaultLens wiki. Before doing any work, read
`project.md` in full; it is the project source of truth. The root vault schema
(`../../AGENTS.md`) and shared project rules (`../AGENTS.md`) also apply.
`## Rules` in `project.md` overrides them where they conflict.

Write only inside this project directory. Never modify `wiki/` or `raw/`.

## Operating principles

- **Project context wins ties.** If a wiki page recommends approach X but `project.md` (Rules, Context, prior decisions) explicitly rules it out, propose the project-compatible alternative instead.
- **Don't fabricate around gaps.** If the wiki doesn't cover something the question requires, say so. Recommend running `wiki-ingest` (for a missing source) or `wiki-enhancer` (for shallow coverage) rather than inventing the answer.
- **Hand off to specialists when warranted.** End answers with a follow-up note when:
  - a wiki claim looks wrong vs. its source → recommend `wiki-source-verifier`
  - two wiki pages appear to disagree → recommend `wiki-contradiction-detector`
  - a referenced concept page is shallow → recommend `wiki-enhancer`
  - a needed source isn't in the wiki yet → recommend `wiki-ingest` with the candidate path
  - the question turns out to need no project context → suggest `wiki-search` instead
"""


# Claude Code imports the neutral project instructions and project.md. The root
# CLAUDE.md imports the provider-neutral root AGENTS.md separately.
CLAUDE_MD_TEMPLATE = """\
@AGENTS.md
@project.md

# Claude Code compatibility

`AGENTS.md` is the provider-neutral project instruction source.
"""


# Per-project TODO seed. Plain checkboxes in the Obsidian Tasks plugin emoji
# format: add `⏫`/`🔺` priority, `📅 YYYY-MM-DD` due dates, etc. via the
# editor autosuggest (`obsidian-tasks-plugin` is configured for this vault).
# Mirrors `wiki/_templates/project-todo.md` (Templater), which auto-applies when
# a TODO.md is created interactively in Obsidian; this constant is used when
# the project is scaffolded via `wiki.py project new`.
TODO_TEMPLATE = """\
# {slug} TODO

Rolling task list (embedded into projects/TODO.md; P1 items also feed
projects/TODO-widget.md). Organise into sections as the project grows.

- [ ]
"""


def _project_list(
    as_json: bool, include_frozen: bool = False, slugs_only: bool = False
) -> int:
    projects = [
        project
        for project in list_projects()
        if include_frozen or not project.is_frozen
    ]
    if slugs_only:
        for project in projects:
            print(project.slug)
        return 0
    if as_json:
        rows = [
            {
                "slug": p.slug,
                "title": p.title,
                "status": p.status,
                "summary": p.summary,
                "domain": p.domain,
                "tags": p.tags,
                "wiki_refs": p.wiki_refs,
            }
            for p in projects
        ]
        print(json.dumps(rows, indent=2))
        return 0
    if not projects:
        print(
            "No projects yet. Create one with:\n"
            "  python3 tools/wiki.py project new <slug>"
        )
        return 0
    print(f"Projects ({len(projects)}):\n")
    for project in projects:
        status = project.status or "active"
        print(f"  {project.slug:<28} [{status}]  {project.title}")
        if project.summary:
            print(f"    {project.summary[:100]}")
    return 0


def _rebuild_projects_todo() -> None:
    """Regenerate active-work views after a project lifecycle change.

    Writes both `projects/TODO.md` (live, embed-based for desktop Obsidian)
    and `projects/TODO-widget.md` (P1-only inlined for the iOS widget), then
    refreshes the frozen-project exclusions in `projects/deadlines.md`.
    """
    script = ROOT / "tools" / "scripts" / "rebuild-projects-todo.sh"
    subprocess.run([str(script)], check=True)
    _rebuild_deadlines()


_DEADLINE_EXCLUSION = re.compile(
    r"^path does not include projects/([^/]+)/TODO\.md\s*$"
)


def _rebuild_deadlines() -> None:
    """Keep Tasks queries from surfacing work from frozen projects.

    The Obsidian Tasks query language cannot read sibling `project.md`
    frontmatter. We therefore materialize one path exclusion per frozen project
    immediately after each `folder includes projects` line. Lifecycle commands
    call this function, so the query remains live for task edits while project
    visibility changes stay explicit and reviewable.
    """
    path = PROJECTS_DIR / "deadlines.md"
    template = PROJECTS_DIR / "deadlines.template.md"
    if not path.exists():
        if not template.exists():
            return
        path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    projects = list_projects()
    project_slugs = {project.slug for project in projects}
    exclusions = [
        f"path does not include projects/{project.slug}/TODO.md"
        for project in projects
        if project.is_frozen
    ]
    original = path.read_text(encoding="utf-8")
    out: list[str] = []
    for line in original.splitlines():
        match = _DEADLINE_EXCLUSION.match(line)
        if match and match.group(1) in project_slugs:
            continue
        out.append(line)
        if line.strip() == "folder includes projects":
            out.extend(exclusions)
    rendered = "\n".join(out) + ("\n" if original.endswith("\n") else "")
    if rendered != original:
        path.write_text(rendered, encoding="utf-8")


def _project_new(slug: str) -> int:
    cleaned = slug.strip().strip("/")
    if not cleaned or "/" in cleaned or cleaned.startswith("."):
        print(f"Invalid project slug: {slug!r}")
        return 1
    project_dir = PROJECTS_DIR / cleaned
    if project_dir.exists():
        print(f"Project '{cleaned}' already exists at {project_dir.relative_to(ROOT)}")
        return 1
    project_dir.mkdir(parents=True)
    (project_dir / "queries").mkdir()
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(
        '{\n  "enabledMcpjsonServers": [\n    "qmd"\n  ]\n}\n', encoding="utf-8"
    )
    today = dt.datetime.now().strftime("%Y-%m-%d")
    title = slug_to_title(cleaned)
    (project_dir / "project.md").write_text(
        PROJECT_TEMPLATE.format(title=title, slug=cleaned, today=today),
        encoding="utf-8",
    )
    (project_dir / "AGENTS.md").write_text(AGENTS_MD_TEMPLATE, encoding="utf-8")
    (project_dir / "CLAUDE.md").write_text(CLAUDE_MD_TEMPLATE, encoding="utf-8")
    (project_dir / "TODO.md").write_text(
        TODO_TEMPLATE.format(slug=cleaned), encoding="utf-8"
    )
    agenda.scaffold(project_dir / "AGENDA.md", cleaned, dt.date.today())
    _rebuild_projects_todo()
    print(f"Created project '{cleaned}' at {project_dir.relative_to(ROOT)}")
    print("  - project.md")
    print(
        "  - AGENTS.md      (provider-neutral project instructions; read project.md first)"
    )
    print(
        "  - CLAUDE.md      (Claude Code compatibility imports AGENTS.md + project.md)"
    )
    print(
        "  - TODO.md        (per-project todo; embedded into projects/TODO.md, P1 items surface in projects/TODO-widget.md)"
    )
    print(
        "  - AGENDA.md      (dormant autonomous-runner agenda; set enabled: true + dump tasks to opt in)"
    )
    print(
        "  - queries/       (default Q&A artifact dir; redefine in ## Rules if you want)"
    )
    print(
        "  - .claude/settings.local.json (enables qmd for Claude; Codex uses root .codex/config.toml)"
    )
    print(
        f"\nNext steps:\n"
        f"  1. Edit projects/{cleaned}/project.md — fill in Description, Layout, and Rules.\n"
        f"  2. Create whatever subfolders this project needs (papers/, meetings/, repos/, ...).\n"
        f"  3. Link relevant wiki pages: python3 tools/wiki.py project link {cleaned} <wiki-ref>"
    )
    return 0


def _project_subfolders(project: Project) -> list[dict]:
    """Enumerate every direct subfolder of the project root, with file counts."""
    rows: list[dict] = []
    for child in sorted(project.root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        md_files = [p for p in child.rglob("*.md") if p.is_file()]
        all_files = [p for p in child.rglob("*") if p.is_file()]
        rows.append(
            {
                "name": child.name,
                "rel": str(child.relative_to(project.root)),
                "md_count": len(md_files),
                "file_count": len(all_files),
            }
        )
    return rows


def _project_show(slug: str, as_json: bool) -> int:
    project = _find_project(slug)
    if project is None:
        print(f"Project '{slug}' not found in {PROJECTS_DIR.relative_to(ROOT)}/")
        return 1
    subfolders = _project_subfolders(project)

    if as_json:
        print(
            json.dumps(
                {
                    "slug": project.slug,
                    "title": project.title,
                    "status": project.status,
                    "domain": project.domain,
                    "summary": project.summary,
                    "tags": project.tags,
                    "wiki_refs": project.wiki_refs,
                    "path": str(project.path.relative_to(ROOT)),
                    "subfolders": subfolders,
                },
                indent=2,
            )
        )
        return 0

    print(f"# {project.title}")
    print(f"Slug:    {project.slug}")
    print(f"Status:  {project.status or '(unset)'}")
    if project.domain:
        print(f"Domain:  {project.domain}")
    if project.tags:
        print(f"Tags:    {', '.join(project.tags)}")
    print(f"Summary: {project.summary}")
    print(f"Path:    {project.path.relative_to(ROOT)}")
    if project.wiki_refs:
        print(f"\nLinked wiki pages ({len(project.wiki_refs)}):")
        for ref in project.wiki_refs:
            print(f"  - [[{ref}]]")
    if subfolders:
        print(f"\nSubfolders ({len(subfolders)}):")
        for entry in subfolders:
            print(
                f"  - {entry['name']:<20} "
                f"({entry['md_count']} md / {entry['file_count']} files)"
            )
    else:
        print("\nNo subfolders yet — create them as needed for this project.")
    return 0


def _project_link(slug: str, ref: str) -> int:
    project = _find_project(slug)
    if project is None:
        print(f"Project '{slug}' not found")
        return 1
    cleaned = normalize_link_target(ref)
    if not cleaned:
        print(f"Empty wiki reference: {ref!r}")
        return 1
    existing = project.wiki_refs
    if cleaned in existing:
        print(f"'{cleaned}' already linked in project '{slug}'")
        return 0
    new_refs = existing + [cleaned]
    today = dt.datetime.now().strftime("%Y-%m-%d")
    text = project.path.read_text(encoding="utf-8")
    text = _set_frontmatter_field(text, "wiki_refs", new_refs)
    text = _set_frontmatter_field(text, "updated", today)
    project.path.write_text(text, encoding="utf-8")
    print(f"Linked '{cleaned}' to project '{slug}' ({len(new_refs)} total wiki_refs)")
    return 0


def _project_freeze(slug: str, frozen: bool) -> int:
    project = _find_project(slug)
    if project is None:
        print(f"Project '{slug}' not found")
        return 1
    target = "frozen" if frozen else "active"
    if project.status == target:
        _rebuild_projects_todo()
        print(f"Project '{slug}' is already {target}; refreshed active-work views.")
        return 0
    if frozen and project.status == "archived":
        print(f"Project '{slug}' is archived; restore it before freezing it.")
        return 1
    if not frozen and not project.is_frozen:
        print(f"Project '{slug}' is not frozen (status: {project.status or 'unset'}).")
        return 1
    today = dt.date.today().isoformat()
    text = project.path.read_text(encoding="utf-8")
    text = _set_frontmatter_field(text, "status", target)
    text = _set_frontmatter_field(text, "updated", today)
    project.path.write_text(text, encoding="utf-8")
    _rebuild_projects_todo()
    if frozen:
        print(
            f"Froze project '{slug}'. It is excluded from active lists, TODO and "
            "deadline views, briefs, agent desks, scheduled work, and routed items."
        )
    else:
        print(f"Unfroze project '{slug}' (status: active) and refreshed project views.")
    return 0


def _agenda_path(slug: str):
    return PROJECTS_DIR / slug / "AGENDA.md"


def _all_agenda_slugs() -> list[str]:
    return [
        path.parent.name
        for path in sorted(PROJECTS_DIR.glob("*/AGENDA.md"))
        if not is_frozen_project(path.parent)
    ]


def _project_agenda(
    sub: str | None, proj: str | None, item_id: str | None, as_json: bool
) -> int:
    """Handle `project agenda <sub> [<slug>] [<id>]`, delegating to tools/agenda.py."""
    today = dt.date.today()

    if sub == "scaffold-all":
        created = agenda.scaffold_all(PROJECTS_DIR, today)
        if created:
            print(
                f"Scaffolded dormant AGENDA.md in {len(created)} project(s): "
                + ", ".join(created)
            )
        else:
            print("All projects already have an AGENDA.md.")
        return 0

    if sub in ("enable", "disable"):
        if not proj:
            print(f"Error: project agenda {sub} <slug> requires a slug")
            return 1
        path = _agenda_path(proj)
        if not path.exists():
            print(f"No AGENDA.md for '{proj}' — run: project agenda scaffold-all")
            return 1
        if sub == "enable" and is_frozen_project(path.parent):
            print(
                f"Project '{proj}' is frozen; unfreeze it before enabling its agenda."
            )
            return 1
        text = path.read_text(encoding="utf-8")
        text = _set_frontmatter_field(
            text, "enabled", "true" if sub == "enable" else "false"
        )
        text = _set_frontmatter_field(text, "updated", today.isoformat())
        path.write_text(text, encoding="utf-8")
        state = "enabled (nightly runs ON)" if sub == "enable" else "disabled (dormant)"
        print(f"Project '{proj}' agenda {state}.")
        return 0

    if sub == "due":
        rows = []
        for slug in agenda.due_projects(PROJECTS_DIR, today):
            _, tasks = agenda.parse_agenda(_agenda_path(slug))
            for t in agenda.due_tasks(tasks, today):
                rows.append(
                    {
                        "slug": slug,
                        "id": t.id,
                        "title": t.title,
                        "schedule": t.schedule,
                        "next_due": t.next_due.isoformat() if t.next_due else None,
                    }
                )
        if as_json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("No due tasks in any enabled project.")
        else:
            for r in rows:
                print(f"  {r['slug']:<24} [{r['id']}] {r['title']}  ({r['schedule']})")
        return 0

    if sub == "clarifications":
        rows = []
        for slug in _all_agenda_slugs():
            try:
                _, tasks = agenda.parse_agenda(_agenda_path(slug))
            except OSError:
                continue
            for t in tasks:
                if t.status == "needs-clarification":
                    rows.append(
                        {
                            "slug": slug,
                            "id": t.id,
                            "title": t.title,
                            "questions": t.questions,
                        }
                    )
        if as_json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("No pending clarifications.")
        else:
            for r in rows:
                print(f"  {r['slug']:<24} [{r['id']}] {r['title']}")
                for q in r["questions"]:
                    print(f"      - {q}")
        return 0

    if sub == "status":
        slugs = [proj] if proj else _all_agenda_slugs()
        for slug in slugs:
            path = _agenda_path(slug)
            if not path.exists():
                print(f"  {slug:<24} (no AGENDA.md)")
                continue
            fm, tasks = agenda.parse_agenda(path)
            counts: dict[str, int] = {}
            for t in tasks:
                counts[t.status] = counts.get(t.status, 0) + 1
            flag = "ON " if agenda.is_enabled(fm) else "off"
            paused = " PAUSED(review)" if agenda.is_paused_for_review(slug) else ""
            n_due = len(agenda.due_tasks(tasks, today))
            summary = (
                ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "no tasks"
            )
            print(f"  {slug:<24} [{flag}] due:{n_due}  {summary}{paused}")
        return 0

    if sub == "lint":
        slugs = [proj] if proj else _all_agenda_slugs()
        any_bad = False
        for slug in slugs:
            problems = agenda.lint(_agenda_path(slug))
            if problems:
                any_bad = True
                print(f"  {slug}:")
                for p in problems:
                    print(f"      - {p}")
        if not any_bad:
            print("All agendas lint clean.")
        return 1 if any_bad else 0

    if sub in ("complete", "resolve"):
        if not proj or not item_id:
            print(f"Error: project agenda {sub} <slug> <task-id> requires both")
            return 1
        fn = agenda.complete if sub == "complete" else agenda.resolve
        ok = fn(_agenda_path(proj), item_id, today)
        if not ok:
            print(f"Task '{item_id}' not found in project '{proj}'")
            return 1
        print(f"{sub.title()}d [{item_id}] in project '{proj}'.")
        return 0

    if sub == "new-id":
        if not proj:
            print("Error: project agenda new-id <slug> requires a slug")
            return 1
        print(agenda.new_id(_agenda_path(proj)))
        return 0

    if sub == "ack":
        if not proj:
            print("Error: project agenda ack <slug> requires a slug")
            return 1
        agenda.ack(proj)
        print(f"Acked '{proj}' — stacking counter reset; nightly runs resume.")
        return 0

    print(
        "Unknown agenda subcommand. Use one of: scaffold-all, enable, disable, status, "
        "lint, due, clarifications, complete, resolve, new-id, ack"
    )
    return 1


def cmd_project(
    action: str,
    slug: str | None,
    ref: str | None,
    as_json: bool,
    extra: str | None = None,
    include_frozen: bool = False,
    slugs_only: bool = False,
) -> int:
    if action == "list":
        return _project_list(as_json, include_frozen, slugs_only)
    if action == "new":
        if not slug:
            print("Error: project new <slug> requires a slug")
            return 1
        return _project_new(slug)
    if action == "show":
        if not slug:
            print("Error: project show <slug> requires a slug")
            return 1
        return _project_show(slug, as_json)
    if action == "link":
        if not slug or not ref:
            print("Error: project link <slug> <wiki-ref> requires both arguments")
            return 1
        return _project_link(slug, ref)
    if action in ("freeze", "unfreeze"):
        if not slug:
            print(f"Error: project {action} <slug> requires a slug")
            return 1
        return _project_freeze(slug, frozen=action == "freeze")
    if action == "agenda":
        # `project agenda <sub> [<slug>] [<id>]`: argparse packs the agenda
        # subcommand into `slug`, the target project into `ref`, the task id
        # into `extra`.
        return _project_agenda(sub=slug, proj=ref, item_id=extra, as_json=as_json)
    print(f"Unknown project action: {action}")
    return 1
