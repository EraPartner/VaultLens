#!/usr/bin/env python3
"""Project workspaces that consume the wiki KB (list/new/show/link).

A project lives under `projects/<slug>/` with its own `project.md` (the source of
truth), the CLAUDE.md agent shim, and a TODO.md that feeds
the aggregated `projects/TODO.md`. This module scaffolds new projects from
templates and manages their `wiki_refs`. The `Project` dataclass and
`list_projects` loader live in `wiki.py` (shared with the linter).
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

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
override the defaults in the root `CLAUDE.md` (`## Working inside a project`)
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


# Project CLAUDE.md: imports project.md deterministically (it's the per-project
# source of truth, so don't leave loading it to the agent's discretion). The
# root vault CLAUDE.md (the wiki operating schema) loads automatically — Claude
# Code walks ancestor directories — so no explicit pointer is needed.
CLAUDE_MD_TEMPLATE = """\
@project.md

# Project Agent Context

This is a project workspace inside the Brain wiki. The root vault schema
(`../../CLAUDE.md`) is loaded automatically; `## Rules` in `project.md`
overrides it where they conflict.

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


def _project_list(as_json: bool) -> int:
    projects = list_projects()
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
    """Regenerate the two project-TODO aggregators.

    Writes both `projects/TODO.md` (live, embed-based for desktop Obsidian)
    and `projects/TODO-widget.md` (P1-only inlined for the iOS widget).
    Delegates to the shell script so the rebuild logic stays single-sourced.
    """
    script = ROOT / "tools" / "scripts" / "rebuild-projects-todo.sh"
    subprocess.run([str(script)], check=True)


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
    today = dt.datetime.now().strftime("%Y-%m-%d")
    title = slug_to_title(cleaned)
    (project_dir / "project.md").write_text(
        PROJECT_TEMPLATE.format(title=title, slug=cleaned, today=today),
        encoding="utf-8",
    )
    (project_dir / "CLAUDE.md").write_text(CLAUDE_MD_TEMPLATE, encoding="utf-8")
    (project_dir / "TODO.md").write_text(
        TODO_TEMPLATE.format(slug=cleaned), encoding="utf-8"
    )
    _rebuild_projects_todo()
    print(f"Created project '{cleaned}' at {project_dir.relative_to(ROOT)}")
    print("  - project.md")
    print(
        "  - CLAUDE.md      (AI entrypoint → @project.md + operating principles; root schema auto-loads)"
    )
    print(
        "  - TODO.md        (per-project todo; embedded into projects/TODO.md, P1 items surface in projects/TODO-widget.md)"
    )
    print(
        "  - queries/       (default Q&A artifact dir; redefine in ## Rules if you want)"
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


def cmd_project(
    action: str,
    slug: str | None,
    ref: str | None,
    as_json: bool,
) -> int:
    if action == "list":
        return _project_list(as_json)
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
    print(f"Unknown project action: {action}")
    return 1
