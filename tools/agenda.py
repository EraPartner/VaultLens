#!/usr/bin/env python3
"""AGENDA.md format, recurrence engine, and surgical state writers.

Single source of truth for the per-project autonomous-runner agenda
(`projects/<slug>/AGENDA.md`). Stdlib-only on purpose so the stdlib-only
scheduler (`tools/schedule/dispatch.py`) and the wiki CLI
(`tools/wiki_projects.py`) can both import it without pulling in `wiki.py`.

An AGENDA.md is one file per project:

    ---
    title / type / status / enabled / created / updated / summary /
    runner_scope / max_tasks_per_run / tags
    ---
    # <slug> — Agenda
    > how-it-works blockquote
    ## Inbox        ← free-form dump; the groomer empties it into ## Tasks
    ## Tasks        ← one `### [id] title` block per task, flat `- key:: value` lines
    ## Clarifications  ← human-readable surface (one entry per needs-clarification task)
    ## Run log      ← append-only audit trail

Per-task fields use Dataview inline-field syntax (`key:: value`) so they stay
human-editable and Obsidian-queryable while remaining trivially regex-parseable.
The runner (an LLM) authors prose; all *mechanical* state transitions go through
the surgical writers here, which rewrite only the targeted lines — bounding the
risk of the model corrupting the file.

Opt-in is the frontmatter `enabled` flag, NOT file presence: every project gets
a dormant (`enabled: false`) AGENDA.md, and only `enabled: true` projects are
ever considered by the nightly builder.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# --- Constants ---------------------------------------------------------------

STATUSES = ("clear", "needs-clarification", "blocked", "done", "paused")

# Closed schedule grammar (parsed by parse_schedule):
#   once | nightly (alias daily) | weekly:Mon | every:Nd | weekdays:Mon,Wed,Fri
_WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Stacking guard: if a project's runner has applied edits on this many consecutive
# nights without an operator `project agenda ack`, the builder skips it (paused)
# so unreviewed working-tree changes cannot pile up indefinitely.
MAX_UNACKED_NIGHTS = 2

# Per-project runner bookkeeping (unacked-run counter). Lives outside the iCloud
# vault — same rationale as the scheduler ledger (~/.brain/schedule-state.json).
RUNNER_STATE_PATH = Path(os.path.expanduser("~/.brain/project-runner-state.json"))

_TASK_HEADING = re.compile(r"^###\s+\[([^\]]+)\]\s*(.*)$")
_FIELD_LINE = re.compile(r"^\s*-?\s*([a-z_]+)::\s*(.*)$")
_SECTION_HEADING = re.compile(r"^##\s+(.*)$")


# --- Frontmatter (minimal, stdlib-only) --------------------------------------


def parse_frontmatter(text: str) -> dict:
    """Parse the leading `--- ... ---` YAML block into a dict.

    Handles the scalar/list/bool/int shapes the agenda schema uses; not a full
    YAML parser. Returns {} when no well-formed frontmatter is present.
    """
    norm = text.replace("\r\n", "\n")
    if not norm.startswith("---\n"):
        return {}
    end = norm.find("\n---\n", 4)
    if end == -1:
        return {}
    out: dict = {}
    for line in norm[4:end].split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        out[key.strip()] = _coerce_scalar(raw.strip())
    return out


def _coerce_scalar(raw: str):
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw.strip().strip("'\"")


def is_enabled(fm: dict) -> bool:
    return fm.get("enabled") is True


# --- Task model + parsing ----------------------------------------------------


@dataclass
class Task:
    id: str
    title: str = ""
    status: str = "clear"
    schedule: str = "once"
    last_run: dt.date | None = None
    next_due: dt.date | None = None
    acceptance: str = ""
    output: str = ""
    notes: str = ""
    questions: list[str] = field(default_factory=list)
    blocked_reason: str = ""


def _parse_date(raw: str) -> dt.date | None:
    raw = (raw or "").strip()
    if not raw or raw in ("—", "-", "none", "None", "null"):
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def _strip_comments_preserve_lines(text: str) -> str:
    """Blank out `<!-- ... -->` comment spans while keeping every newline, so the
    result has the same line count/indices as the input. Lets parsing and the
    writers' block-finding ignore the scaffolded example task (and any other
    commented content) without misaligning line indices used to mutate the file."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("<!--", i):
            j = text.find("-->", i)
            j = n if j == -1 else j + 3
            out.append("".join("\n" if c == "\n" else "" for c in text[i:j]))
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _search_view(lines: list[str]) -> list[str]:
    """Comment-stripped, index-aligned copy of `lines` for pattern matching."""
    return _strip_comments_preserve_lines("\n".join(lines)).split("\n")


def _section_lines(text: str, name: str) -> list[str]:
    """Return the body lines of the `## <name>` section (exclusive of headings)."""
    lines = _strip_comments_preserve_lines(text.replace("\r\n", "\n")).split("\n")
    out: list[str] = []
    capturing = False
    for line in lines:
        m = _SECTION_HEADING.match(line)
        if m:
            capturing = m.group(1).strip().lower() == name.lower()
            continue
        if capturing:
            out.append(line)
    return out


def parse_tasks(text: str) -> list[Task]:
    """Parse every `### [id]` block under the `## Tasks` section."""
    body = _section_lines(text, "Tasks")
    tasks: list[Task] = []
    cur: Task | None = None
    pending_questions = False
    for line in body:
        head = _TASK_HEADING.match(line)
        if head:
            if cur is not None:
                tasks.append(cur)
            cur = Task(id=head.group(1).strip(), title=head.group(2).strip())
            pending_questions = False
            continue
        if cur is None:
            continue
        fm = _FIELD_LINE.match(line)
        if fm:
            key, val = fm.group(1), fm.group(2).strip()
            pending_questions = False
            if key == "status":
                cur.status = val or "clear"
            elif key == "schedule":
                cur.schedule = val or "once"
            elif key == "last_run":
                cur.last_run = _parse_date(val)
            elif key == "next_due":
                cur.next_due = _parse_date(val)
            elif key == "acceptance":
                cur.acceptance = val
            elif key == "output":
                cur.output = val
            elif key == "notes":
                cur.notes = val
            elif key == "blocked_reason":
                cur.blocked_reason = val
            elif key == "questions":
                if val:
                    cur.questions.append(val)
                else:
                    pending_questions = True
            continue
        # Indented sub-bullet belonging to a `questions::` line.
        if pending_questions:
            sub = re.match(r"^\s+-\s+(.*)$", line)
            if sub:
                cur.questions.append(sub.group(1).strip())
                continue
            if line.strip():
                pending_questions = False
    if cur is not None:
        tasks.append(cur)
    return tasks


def parse_agenda(path: str | Path) -> tuple[dict, list[Task]]:
    text = Path(path).read_text(encoding="utf-8")
    return parse_frontmatter(text), parse_tasks(text)


# --- Recurrence engine -------------------------------------------------------


def parse_schedule(schedule: str) -> tuple[str, object]:
    """Validate + normalize a schedule string. Raises ValueError if malformed.

    Returns (kind, param):
      ("once", None) | ("nightly", None) | ("every", N) |
      ("weekly", [wd]) | ("weekdays", [wd, ...])   (wd: 0=Mon..6=Sun)
    """
    s = (schedule or "").strip().lower()
    if s in ("once", ""):
        return ("once", None)
    if s in ("nightly", "daily"):
        return ("nightly", None)
    if s.startswith("weekly:"):
        wd = s.split(":", 1)[1].strip()[:3]
        if wd not in _WEEKDAYS:
            raise ValueError(f"bad weekly day: {schedule!r}")
        return ("weekly", [_WEEKDAYS[wd]])
    if s.startswith("every:"):
        rest = s.split(":", 1)[1].strip().rstrip("d")
        if not rest.isdigit() or int(rest) < 1:
            raise ValueError(f"bad every:Nd interval: {schedule!r}")
        return ("every", int(rest))
    if s.startswith("weekdays:"):
        days = [d.strip()[:3] for d in s.split(":", 1)[1].split(",") if d.strip()]
        wds = []
        for d in days:
            if d not in _WEEKDAYS:
                raise ValueError(f"bad weekday in {schedule!r}")
            wds.append(_WEEKDAYS[d])
        if not wds:
            raise ValueError(f"empty weekdays list: {schedule!r}")
        return ("weekdays", sorted(set(wds)))
    raise ValueError(f"unknown schedule: {schedule!r}")


def _next_weekday(today: dt.date, weekdays: list[int]) -> dt.date:
    """Next date strictly after `today` whose weekday is in `weekdays`."""
    best = None
    for wd in weekdays:
        ahead = (wd - today.weekday() + 7) % 7
        ahead = ahead or 7  # strictly after today
        cand = today + dt.timedelta(days=ahead)
        best = cand if best is None or cand < best else best
    return best


def compute_next_due(
    schedule: str, last_run: dt.date | None, today: dt.date
) -> dt.date | None:
    """Next due date AFTER a run on `today`. None for one-shot tasks.

    Recurring cadences are computed from the run date (`today`), not `last_run`,
    so a project that was asleep for a week resumes its cadence instead of firing
    every night to "catch up".
    """
    kind, param = parse_schedule(schedule)
    if kind == "once":
        return None
    if kind == "nightly":
        return today + dt.timedelta(days=1)
    if kind == "every":
        return today + dt.timedelta(days=int(param))
    if kind in ("weekly", "weekdays"):
        return _next_weekday(today, list(param))
    return None


def due_tasks(tasks: list[Task], today: dt.date) -> list[Task]:
    return [
        t
        for t in tasks
        if t.status == "clear" and t.next_due is not None and t.next_due <= today
    ]


def project_is_due(agenda_path: str | Path, today: dt.date) -> bool:
    """True iff the AGENDA is enabled AND has at least one clear, due task."""
    try:
        fm, tasks = parse_agenda(agenda_path)
    except (OSError, ValueError):
        return False
    if not is_enabled(fm):
        return False
    return bool(due_tasks(tasks, today))


def due_projects(projects_dir: str | Path, today: dt.date) -> list[str]:
    """Slugs of enabled projects with at least one due task. Malformed agendas
    are skipped (never crash the nightly tick)."""
    root = Path(projects_dir)
    out: list[str] = []
    if not root.is_dir():
        return out
    for agenda_path in sorted(root.glob("*/AGENDA.md")):
        try:
            if project_is_due(agenda_path, today):
                out.append(agenda_path.parent.name)
        except Exception:
            continue
    return out


# --- Surgical writers --------------------------------------------------------


def _task_block_range(lines: list[str], task_id: str) -> tuple[int, int] | None:
    """[start, end) line range of a real (non-commented) `### [id]` block."""
    search = _search_view(lines)
    start = None
    for i, line in enumerate(search):
        m = _TASK_HEADING.match(line)
        if m and m.group(1).strip() == task_id:
            start = i
            break
    if start is None:
        return None
    end = len(search)
    for j in range(start + 1, len(search)):
        if search[j].startswith("### ") or search[j].startswith("## "):
            end = j
            break
    return (start, end)


def _set_task_field(lines: list[str], task_id: str, key: str, value: str) -> bool:
    rng = _task_block_range(lines, task_id)
    if rng is None:
        return False
    start, end = rng
    search = _search_view(lines)
    new_line = f"- {key}:: {value}"
    for i in range(start + 1, end):
        m = _FIELD_LINE.match(search[i])
        if m and m.group(1) == key:
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            lines[i] = f"{indent}{new_line}"
            return True
    # Not present — insert right after the heading.
    lines.insert(start + 1, new_line)
    return True


def _append_run_log(lines: list[str], entry: str) -> None:
    """Append `- <entry>` at the end of the `## Run log` section."""
    search = _search_view(lines)
    log_start = None
    for i, line in enumerate(search):
        m = _SECTION_HEADING.match(line)
        if m and m.group(1).strip().lower() == "run log":
            log_start = i
            break
    line = f"- {entry}"
    if log_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["## Run log", line])
        return
    end = len(search)
    for j in range(log_start + 1, len(search)):
        if search[j].startswith("## "):
            end = j
            break
    insert_at = end
    while insert_at - 1 > log_start and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, line)


def _remove_clarification_entry(lines: list[str], task_id: str) -> None:
    """Drop a `### [id]` block from the `## Clarifications` section, if present."""
    search = _search_view(lines)
    clar_start = None
    for i, line in enumerate(search):
        m = _SECTION_HEADING.match(line)
        if m and m.group(1).strip().lower() == "clarifications":
            clar_start = i
            break
    if clar_start is None:
        return
    clar_end = len(search)
    for j in range(clar_start + 1, len(search)):
        if search[j].startswith("## "):
            clar_end = j
            break
    block_start = None
    for i in range(clar_start + 1, clar_end):
        m = _TASK_HEADING.match(search[i])
        if m and m.group(1).strip() == task_id:
            block_start = i
            break
    if block_start is None:
        return
    block_end = clar_end
    for j in range(block_start + 1, clar_end):
        if search[j].startswith("### "):
            block_end = j
            break
    del lines[block_start:block_end]


def _today_str(today: dt.date | None) -> str:
    return (today or dt.date.today()).isoformat()


def _stamp_updated(lines: list[str], today: dt.date | None) -> None:
    """Bump the frontmatter `updated:` field."""
    if not lines or lines[0].strip() != "---":
        return
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            break
        if lines[i].startswith("updated:"):
            lines[i] = f"updated: {_today_str(today)}"
            return


def _write_lines(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def complete(path: str | Path, task_id: str, today: dt.date | None = None) -> bool:
    """Advance a task after the runner executed it: stamp last_run, then either
    mark `done` (one-shot) or compute the next `next_due` (recurring)."""
    today = today or dt.date.today()
    p = Path(path)
    _, tasks = parse_agenda(p)
    task = next((t for t in tasks if t.id == task_id), None)
    if task is None:
        return False
    lines = p.read_text(encoding="utf-8").split("\n")
    _set_task_field(lines, task_id, "last_run", today.isoformat())
    try:
        nxt = compute_next_due(task.schedule, today, today)
    except ValueError:
        nxt = None
    if nxt is None:
        _set_task_field(lines, task_id, "status", "done")
        _set_task_field(lines, task_id, "next_due", "—")
        outcome = "done"
    else:
        _set_task_field(lines, task_id, "next_due", nxt.isoformat())
        outcome = f"advanced (next_due {nxt.isoformat()})"
    _append_run_log(lines, f"{today.isoformat()} [{task_id}] executed → {outcome}")
    _stamp_updated(lines, today)
    _write_lines(p, lines)
    return True


def resolve(path: str | Path, task_id: str, today: dt.date | None = None) -> bool:
    """Flip a needs-clarification task back to `clear` once the operator has
    answered: drop its questions + the Clarifications entry, set next_due = today
    so the next nightly run executes it."""
    today = today or dt.date.today()
    p = Path(path)
    lines = p.read_text(encoding="utf-8").split("\n")
    if _task_block_range(lines, task_id) is None:
        return False
    _set_task_field(lines, task_id, "status", "clear")
    _set_task_field(lines, task_id, "next_due", today.isoformat())
    _drop_task_field(lines, task_id, "questions")
    _remove_clarification_entry(lines, task_id)
    _append_run_log(
        lines, f"{today.isoformat()} [{task_id}] resolved via /project-clarify → clear"
    )
    _stamp_updated(lines, today)
    _write_lines(p, lines)
    return True


def _drop_task_field(lines: list[str], task_id: str, key: str) -> None:
    """Remove a `- key:: ...` line (and, for `questions`, its indented sub-bullets)."""
    rng = _task_block_range(lines, task_id)
    if rng is None:
        return
    start, end = rng
    i = start + 1
    while i < end and i < len(lines):
        m = _FIELD_LINE.match(lines[i])
        if m and m.group(1) == key:
            j = i + 1
            if key == "questions" and not m.group(2).strip():
                while j < end and re.match(r"^\s+-\s+", lines[j]):
                    j += 1
            del lines[i:j]
            end -= j - i
            continue
        i += 1


def set_status(
    path: str | Path, task_id: str, status: str, today: dt.date | None = None
) -> bool:
    if status not in STATUSES:
        raise ValueError(f"bad status: {status!r}")
    p = Path(path)
    lines = p.read_text(encoding="utf-8").split("\n")
    if not _set_task_field(lines, task_id, "status", status):
        return False
    _stamp_updated(lines, today)
    _write_lines(p, lines)
    return True


def new_id(path: str | Path) -> str:
    """Next free `T<n>` id for a project's agenda."""
    _, tasks = parse_agenda(path)
    nums = [int(m.group(1)) for t in tasks if (m := re.fullmatch(r"T(\d+)", t.id))]
    return f"T{(max(nums) + 1) if nums else 1}"


def lint(path: str | Path) -> list[str]:
    """Return a list of format problems for an AGENDA.md (empty == clean)."""
    p = Path(path)
    problems: list[str] = []
    try:
        fm, tasks = parse_agenda(p)
    except OSError as e:
        return [f"unreadable: {e}"]
    for key in ("type", "enabled", "status"):
        if key not in fm:
            problems.append(f"frontmatter missing '{key}'")
    if "enabled" in fm and not isinstance(fm["enabled"], bool):
        problems.append("frontmatter 'enabled' must be true/false")
    seen: set[str] = set()
    for t in tasks:
        where = f"[{t.id}]"
        if t.id in seen:
            problems.append(f"{where} duplicate task id")
        seen.add(t.id)
        if t.status not in STATUSES:
            problems.append(f"{where} bad status '{t.status}'")
        try:
            parse_schedule(t.schedule)
        except ValueError as e:
            problems.append(f"{where} {e}")
        if t.status == "clear" and not t.acceptance:
            problems.append(f"{where} clear task has no acceptance line")
        if t.status == "needs-clarification" and not t.questions:
            problems.append(f"{where} needs-clarification task has no questions")
    return problems


# --- Scaffolding -------------------------------------------------------------

AGENDA_TEMPLATE = """\
---
title: {title} — Agenda
type: agenda
status: active
enabled: false
created: {today}
updated: {today}
summary: Autonomous nightly project-runner agenda for {slug}.
runner_scope: [edits, artifacts, research]
max_tasks_per_run: 5
tags: [agenda, project-runner]
---

# {title} — Agenda

> **How this works.** Dump tasks under **## Inbox** in any format. Each night the
> project-runner grooms them into **## Tasks** with a stable id, a schedule, and an
> acceptance line, then executes the tasks that are 100% clear and due. Anything
> ambiguous is filed under **## Clarifications** for you to resolve with
> `/project-clarify` (it never guesses overnight). Set `enabled: true` in the
> frontmatter to turn nightly runs on (off by default). The runner edits this
> project's files for real but **never commits**; a morning roll-up at
> `wiki/reports/scheduled-project-runner-<date>.md` lists every change and a one-line
> restore command. It writes only inside this project, and network use is bounded by
> the devcontainer egress allowlist (a task needing a non-allowlisted host is marked
> `blocked`, never run silently).
>
> Statuses: `clear` · `needs-clarification` · `blocked` · `done` · `paused`.
> Schedules: `once` · `nightly` · `weekly:Mon` · `every:3d` · `weekdays:Mon,Wed,Fri`.

## Inbox
<!-- Dump anything here, any format. The groomer empties this into ## Tasks each night. -->

## Tasks
<!-- Managed by the runner. Example shape (delete the comment, keep real tasks):
### [T1] Short imperative title
- status:: clear
- schedule:: weekly:Mon
- last_run:: —
- next_due:: {today}
- acceptance:: Objective, self-verifiable definition of done.
- output:: projects/{slug}/notes/example.md
- notes:: optional free text
-->

## Clarifications
<!-- One entry per needs-clarification task; resolve with `/project-clarify {slug}`. -->

## Run log
<!-- Append-only, newest at bottom: "YYYY-MM-DD [Tn] action → result". -->
"""


def scaffold(path: str | Path, slug: str, today: dt.date | None = None) -> bool:
    """Write a dormant AGENDA.md if one does not already exist. Returns True if
    created, False if it was already there."""
    p = Path(path)
    if p.exists():
        return False
    title = re.sub(r"[-_]+", " ", slug).strip().title()
    p.write_text(
        AGENDA_TEMPLATE.format(slug=slug, title=title, today=_today_str(today)),
        encoding="utf-8",
    )
    return True


def scaffold_all(projects_dir: str | Path, today: dt.date | None = None) -> list[str]:
    """Create a dormant AGENDA.md in every project that lacks one. Returns the
    list of slugs that got a new file."""
    root = Path(projects_dir)
    created: list[str] = []
    if not root.is_dir():
        return created
    for project_md in sorted(root.glob("*/project.md")):
        proj = project_md.parent
        if scaffold(proj / "AGENDA.md", proj.name, today):
            created.append(proj.name)
    return created


# --- Runner bookkeeping (unacked-run stacking guard) -------------------------


def load_runner_state() -> dict:
    try:
        return json.loads(RUNNER_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_runner_state(state: dict) -> None:
    RUNNER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNNER_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def unacked_count(slug: str) -> int:
    return int(load_runner_state().get(slug, {}).get("unacked", 0))


def is_paused_for_review(slug: str, threshold: int = MAX_UNACKED_NIGHTS) -> bool:
    return unacked_count(slug) >= threshold


def record_run(slug: str, executed: int, when: str | None = None) -> None:
    """Note a runner pass. Only edit-producing passes (executed > 0) advance the
    unacked counter that drives the stacking guard."""
    state = load_runner_state()
    rec = state.setdefault(slug, {"unacked": 0, "last_run": None})
    rec["last_run"] = when or dt.datetime.now().isoformat(timespec="seconds")
    if executed > 0:
        rec["unacked"] = int(rec.get("unacked", 0)) + 1
    save_runner_state(state)


def ack(slug: str) -> None:
    """Operator reviewed `slug`'s changes — reset its stacking counter."""
    state = load_runner_state()
    rec = state.setdefault(slug, {"unacked": 0, "last_run": None})
    rec["unacked"] = 0
    rec["acked_at"] = dt.datetime.now().isoformat(timespec="seconds")
    save_runner_state(state)
