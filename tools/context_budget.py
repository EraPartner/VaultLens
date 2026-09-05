"""Opt-in, deterministic live-context selection. Budgets count Unicode characters.

This does not estimate model tokens or replace semantic evaluation. Mandatory
profile, consent information, and project/queue overview are never truncated.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from context_sources import read_inbox_preview


CONSENT = (
    "Consent required: raw/review-inbox entries are names only. Ask the operator "
    "before reading, summarizing, moving, or ingesting any item."
)


def task_priority(text: str, today: dt.date) -> tuple[int, int]:
    """Scan every task before selecting; overdue and priority marks sort first."""
    dates = re.findall(r"📅\s*(\d{4}-\d{2}-\d{2})", text)
    due = any(date <= today.isoformat() for date in dates)
    marks = ["🔺", "⏫", "🔼", "🔽", "⏬"]
    priority = next((i for i, mark in enumerate(marks) if mark in text), 3)
    return (0 if due else 1, priority)


@dataclass
class ContextSource:
    path: str
    items: list[str]
    total_items: int | None = None
    critical_indexes: set[int] = field(default_factory=set)
    critical_label: str = "urgent"
    prioritize_critical: bool = False
    unit: str = "items"

    def __post_init__(self):
        if self.total_items is None:
            self.total_items = len(self.items)
        if self.total_items < len(self.items):
            raise ValueError("source total cannot be smaller than candidate count")


def select_context(
    mandatory: str,
    sources: list[ContextSource | tuple[str, list[str]]],
    budget: int,
) -> str:
    """Round-robin across ranked sources, retaining whole evidence lines."""
    if budget <= 0:
        raise ValueError("VAULTLENS_COS_CONTEXT_CHARS must be a positive integer")

    normalized = [
        source if isinstance(source, ContextSource) else ContextSource(*source)
        for source in sources
    ]

    def footer(selections: list[set[int]], reserve: bool = False) -> str:
        rows = ["", "## Context selection (characters, not tokens)"]
        for source, chosen in zip(normalized, selections):
            shown = len(source.items) if reserve else len(chosen)
            omitted = (
                source.total_items if reserve else source.total_items - len(chosen)
            )
            preselected = source.total_items - len(source.items)
            budget_omitted = (
                len(source.items) if reserve else len(source.items) - len(chosen)
            )
            rows.append(
                f"- {source.path}: included {shown}; omitted {omitted} {source.unit} (preselection {preselected}; budget {budget_omitted})."
            )
            critical_omitted = len(
                source.critical_indexes if reserve else source.critical_indexes - chosen
            )
            if critical_omitted:
                rows.append(
                    f"REQUIRED RETRIEVAL: {source.path}: {critical_omitted} {source.critical_label} items omitted; inspect source before giving advice or a health verdict."
                )
        rows.append(
            "Omitted content is unknown. Read source paths for required detail."
        )
        return "\n".join(rows)

    selected: list[str] = []
    selections: list[set[int]] = [set() for _ in normalized]
    reserved = footer(selections, reserve=True)
    remaining = budget - len(mandatory) - len(reserved)
    if remaining < 0:
        raise ValueError(
            f"mandatory context needs at least {len(mandatory) + len(reserved)} "
            f"characters, exceeding budget {budget}; nothing was silently truncated"
        )

    def consider(source_index: int, index: int):
        nonlocal remaining
        source = normalized[source_index]
        item = f"\n[{source.path}] {source.items[index]}"
        if len(item) <= remaining:
            selected.append(item)
            selections[source_index].add(index)
            remaining -= len(item)

    # Scheduler failures take precedence over optional successes and task detail.
    # Other urgent candidates then get fair turns across projects. Noncritical
    # context is considered last. Whole oversized evidence remains omitted with
    # a mandatory retrieval requirement, never a silently shortened paraphrase.
    for source_index, source in enumerate(normalized):
        if source.prioritize_critical:
            for index in sorted(source.critical_indexes):
                consider(source_index, index)
    for critical_pass in (True, False):
        candidates = [
            [
                i
                for i in range(len(source.items))
                if (i in source.critical_indexes) == critical_pass
                and not (critical_pass and source.prioritize_critical)
            ]
            for source in normalized
        ]
        for turn in range(max((len(items) for items in candidates), default=0)):
            for source_index, indexes in enumerate(candidates):
                if turn >= len(indexes):
                    continue
                index = indexes[turn]
                consider(source_index, index)
    result = mandatory + "".join(selected) + footer(selections)
    assert len(result) <= budget
    return result


def gather_context(
    root: Path,
    mode: str,
    project_filter: str | None,
    budget: int,
    today: dt.date,
) -> str:
    """Collect full mandatory data and all task candidates before selection."""
    from project_state import project_status

    required = ["## Live context", f"Date: {today.isoformat()}", CONSENT]
    profile = root / "wiki/entities/user-background.md"
    try:
        profile_text = profile.read_text(encoding="utf-8")
    except FileNotFoundError:
        profile_text = None
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            "mandatory operator profile is unreadable; context was not sent"
        ) from exc
    if profile_text is not None:
        required.extend(
            [f"## Operator profile ({profile.relative_to(root)})", profile_text]
        )
    sources: list[ContextSource] = []
    projects = root / "projects"
    required.append("## Project overview (all selected projects)")
    for project in sorted(projects.iterdir()) if projects.is_dir() else []:
        if not project.is_dir() or project.name.startswith("."):
            continue
        if project_filter and project.name != project_filter:
            continue
        try:
            status = project_status(project)
        except UnicodeError:
            status = ""
        if not project_filter and status == "frozen":
            continue
        todo = project / "TODO.md"
        path = str(todo.relative_to(root))
        metadata = (
            status or "unknown (project metadata missing, unreadable or malformed)"
        )
        try:
            todo_text = todo.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            required.append(
                f"- {project.name}: status {metadata}; TODO unavailable, open count unknown; source {path}."
            )
            continue
        tasks = [
            (n, line)
            for n, line in enumerate(todo_text.splitlines(), 1)
            if "- [ ]" in line
        ]
        urgent = sum(task_priority(line, today) < (1, 2) for _, line in tasks)
        required.append(
            f"- {project.name}: status {metadata}; {len(tasks)} open; {urgent} due/high-priority; source {path}."
        )
        ranked = sorted(
            tasks, key=lambda pair: (task_priority(pair[1], today), pair[0])
        )
        sources.append(
            ContextSource(
                path,
                [f"line {n}: {line}" for n, line in ranked],
                critical_indexes={
                    i
                    for i, (_, line) in enumerate(ranked)
                    if task_priority(line, today) < (1, 2)
                },
                unit="open tasks",
            )
        )

    if mode in ("brief", "status"):
        import agenda

        # This is the complete structured status overview, never a sliced prefix.
        desks = agenda.desk_status(projects, today)
        if project_filter:
            desks = [desk for desk in desks if desk.get("slug") == project_filter]
        required.append(agenda.format_desk_status(desks))

    for queue in ("raw/inbox", "raw/review-inbox"):
        directory = root / queue
        entries = (
            sorted(p for p in directory.iterdir() if not p.name.startswith("."))
            if directory.is_dir()
            else []
        )
        required.append(f"## Queue overview: {queue}/ ({len(entries)} entries)")
        names = [f"{p.name} ({p.stat().st_size} bytes)" for p in entries]
        if queue.endswith("review-inbox"):
            required.extend(names)  # names only; do not open review content
        else:
            sources.append(ContextSource(queue + "/", names, unit="entries"))
            if mode == "inbox":
                previewed = 0
                for path in entries[:8]:
                    if path.suffix.lower() in (".md", ".txt"):
                        content = read_inbox_preview(root, path)
                        if content is None:
                            required.append(
                                f"- {path.relative_to(root)}: preview unavailable or unsafe; content unknown."
                            )
                            continue
                        lines = content.splitlines()
                        sources.append(
                            ContextSource(
                                str(path.relative_to(root)),
                                [
                                    f"line {n}: {line}"
                                    for n, line in enumerate(lines[:30], 1)
                                ],
                                total_items=len(lines),
                                unit="lines",
                            )
                        )
                        previewed += 1
                required.append(
                    f"Inbox previews: {previewed} files considered; {len(entries) - previewed} entries not previewed (selection limit, unsupported type or unsafe/unreadable source); source {queue}/."
                )

    if mode in ("brief", "surface", "status"):
        path = root / "wiki/log.md"
        if path.is_file():
            lines = path.read_text().splitlines()
            start = max(0, len(lines) - 60)
            sources.append(
                ContextSource(
                    "wiki/log.md",
                    [
                        f"line {n}: {line}"
                        for n, line in enumerate(lines[start:], start + 1)
                    ],
                    total_items=len(lines),
                    unit="lines",
                )
            )
    if mode in ("brief", "status"):
        path = root / "wiki/reports/schedule-status.md"
        source_path = "wiki/reports/schedule-status.md"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            required.append(
                f"## Scheduler health: UNKNOWN (status source missing or unreadable). Inspect {source_path} or scheduler state before a health verdict."
            )
        else:
            attention = scheduler_attention(lines)
            verdict = (
                f"ATTENTION: {len(attention)} failure/staleness/limit indicators"
                if attention
                else "no attention indicators detected; this is not runtime health verification"
            )
            required.append(
                f"## Scheduler health summary: {verdict}; scanned all {len(lines)} lines of {source_path}. Failure details are prioritized; retrieve any omitted attention lines before a health verdict."
            )
            sources.append(
                ContextSource(
                    source_path,
                    [f"line {n}: {line}" for n, line in enumerate(lines, 1)],
                    critical_indexes=attention,
                    critical_label="scheduler attention",
                    prioritize_critical=True,
                    unit="lines",
                )
            )
    return select_context("\n".join(required), sources, budget)


def scheduler_attention(lines: list[str]) -> set[int]:
    """Find warnings and non-success results across the full scheduler report.

    Match the actual dispatcher table as well as plain warning/error prose.
    This is deterministic triage, not a factual claim that a service is unhealthy.
    """
    markers = re.compile(
        r"(?<![a-z])(?:fail(?:ed|ure|ing)?|error|warning|timeout|timed.out|cancelled|blocked|stale|limited|unconfirmed|transient)(?![a-z])",
        re.I,
    )
    attention = set()
    for index, line in enumerate(lines):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        failed_result = (
            len(cells) == 5
            and cells[1] in {"daily", "weekly"}
            and cells[3] not in {"ok", "noop", "none"}
        )
        if markers.search(line) or failed_result:
            attention.add(index)
    return attention
