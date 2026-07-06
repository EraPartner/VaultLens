#!/usr/bin/env python3
"""Self-contained tests for the AGENDA.md format + recurrence + state writers.

Exercises tools/agenda.py: frontmatter parsing, the schedule grammar and
compute_next_due, task parsing, due-filtering, the surgical writers (which must
preserve unrelated content), lint, and scaffolding. Run with:

    python3 tools/tests/test_agenda.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agenda  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


SAMPLE = """\
---
title: Thesis — Agenda
type: agenda
status: active
enabled: true
created: 2026-06-28
updated: 2026-06-28
summary: test
runner_scope: [edits, artifacts, research]
max_tasks_per_run: 5
tags: [agenda, project-runner]
---

# Thesis — Agenda

> how this works

## Inbox
- some loose thing

## Tasks

### [T1] Regenerate results table
- status:: clear
- schedule:: weekly:Mon
- last_run:: 2026-06-22
- next_due:: 2026-06-22
- acceptance:: results/table.md regenerated from newest csv.
- output:: projects/alpha/results/table.md
- notes:: deterministic

### [T2] Survey recent papers
- status:: needs-clarification
- schedule:: once
- last_run:: —
- next_due:: 2026-06-29
- acceptance:: (pending) a dated summary under research/.
- output:: projects/alpha/research/survey.md
- questions::
    - What counts as "recent"?
    - Is arxiv.org acceptable?

### [T3] One-shot cleanup
- status:: clear
- schedule:: once
- last_run:: —
- next_due:: 2026-06-28
- acceptance:: tidy notes/.
- output:: projects/alpha/notes/

## Clarifications

### [T2] Survey recent papers — opened 2026-06-28
- What counts as "recent"?
- Is arxiv.org acceptable?

## Run log
- 2026-06-22 [T1] executed → advanced (next_due 2026-06-29)
"""


def main() -> int:
    today = date(2026, 6, 28)  # a Sunday

    print("frontmatter:")
    fm = agenda.parse_frontmatter(SAMPLE)
    check("enabled is bool True", fm.get("enabled") is True)
    check("is_enabled", agenda.is_enabled(fm) is True)
    check("max_tasks_per_run int", fm.get("max_tasks_per_run") == 5)
    check(
        "runner_scope list",
        fm.get("runner_scope") == ["edits", "artifacts", "research"],
    )
    check(
        "disabled when missing",
        agenda.is_enabled(agenda.parse_frontmatter("no fm")) is False,
    )

    print("schedule grammar:")
    check("once", agenda.parse_schedule("once") == ("once", None))
    check("nightly alias daily", agenda.parse_schedule("daily") == ("nightly", None))
    check("weekly:Mon", agenda.parse_schedule("weekly:Mon") == ("weekly", [0]))
    check("every:3d", agenda.parse_schedule("every:3d") == ("every", 3))
    check(
        "weekdays sorted/deduped",
        agenda.parse_schedule("weekdays:Fri,Mon,Mon") == ("weekdays", [0, 4]),
    )
    for bad in ("weekly:Xyz", "every:0d", "every:-2d", "weekdays:", "bogus"):
        try:
            agenda.parse_schedule(bad)
            check(f"reject {bad!r}", False, "no error raised")
        except ValueError:
            check(f"reject {bad!r}", True)

    print("compute_next_due (run on Sunday 2026-06-28):")
    check(
        "nightly +1",
        agenda.compute_next_due("nightly", None, today) == date(2026, 6, 29),
    )
    check(
        "every:3d +3",
        agenda.compute_next_due("every:3d", None, today) == date(2026, 7, 1),
    )
    check(
        "weekly:Mon -> next Mon",
        agenda.compute_next_due("weekly:Mon", None, today) == date(2026, 6, 29),
    )
    check(
        "weekly:Sun strictly after -> +7",
        agenda.compute_next_due("weekly:Sun", None, today) == date(2026, 7, 5),
    )
    check("once -> None", agenda.compute_next_due("once", None, today) is None)
    check(
        "weekdays next match",
        agenda.compute_next_due("weekdays:Mon,Wed,Fri", None, today)
        == date(2026, 6, 29),
    )

    print("task parsing:")
    tasks = agenda.parse_tasks(SAMPLE)
    check("3 tasks", len(tasks) == 3, f"got {len(tasks)}")
    t1, t2, t3 = tasks
    check(
        "T1 fields",
        t1.id == "T1" and t1.status == "clear" and t1.schedule == "weekly:Mon",
    )
    check(
        "T1 dates",
        t1.last_run == date(2026, 6, 22) and t1.next_due == date(2026, 6, 22),
    )
    check(
        "T2 multi-line questions",
        len(t2.questions) == 2 and "arxiv.org" in t2.questions[1],
    )
    check("T2 status", t2.status == "needs-clarification")

    print("due filtering:")
    due = agenda.due_tasks(tasks, today)
    due_ids = {t.id for t in due}
    check("T1 due (next_due<=today, clear)", "T1" in due_ids)
    check("T3 due", "T3" in due_ids)
    check("T2 not due (needs-clarification)", "T2" not in due_ids)

    print("new_id:")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "AGENDA.md"
        p.write_text(SAMPLE, encoding="utf-8")
        check("new_id == T4", agenda.new_id(p) == "T4")

    print("complete (recurring advances, log appended, unrelated preserved):")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "AGENDA.md"
        p.write_text(SAMPLE, encoding="utf-8")
        ok = agenda.complete(p, "T1", today)
        _, tks = agenda.parse_agenda(p)
        t1b = next(t for t in tks if t.id == "T1")
        check("complete returns True", ok)
        check("T1 last_run stamped", t1b.last_run == today)
        check("T1 next_due advanced", t1b.next_due == date(2026, 6, 29))
        check("T1 stays clear", t1b.status == "clear")
        body = p.read_text(encoding="utf-8")
        check(
            "run-log line appended",
            "[T1] executed" in body and body.count("## Run log") == 1,
        )
        check("Inbox preserved", "some loose thing" in body)
        check("T2 questions preserved", body.count("Is arxiv.org acceptable?") >= 1)

    print("complete (one-shot -> done):")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "AGENDA.md"
        p.write_text(SAMPLE, encoding="utf-8")
        agenda.complete(p, "T3", today)
        _, tks = agenda.parse_agenda(p)
        t3b = next(t for t in tks if t.id == "T3")
        check("T3 -> done", t3b.status == "done")
        check("T3 next_due cleared", t3b.next_due is None)

    print("resolve (needs-clarification -> clear, questions + clar entry dropped):")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "AGENDA.md"
        p.write_text(SAMPLE, encoding="utf-8")
        agenda.resolve(p, "T2", today)
        fm2, tks = agenda.parse_agenda(p)
        t2b = next(t for t in tks if t.id == "T2")
        body = p.read_text(encoding="utf-8")
        check("T2 -> clear", t2b.status == "clear")
        check("T2 next_due == today", t2b.next_due == today)
        check("T2 questions removed", t2b.questions == [])
        check("clarifications entry removed", "opened 2026-06-28" not in body)
        check("updated stamped", fm2.get("updated") == "2026-06-28")

    print("lint:")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "AGENDA.md"
        p.write_text(SAMPLE, encoding="utf-8")
        check("sample has no lint problems", agenda.lint(p) == [], str(agenda.lint(p)))
        bad = SAMPLE.replace("schedule:: weekly:Mon", "schedule:: weekly:Xyz")
        p.write_text(bad, encoding="utf-8")
        check("bad schedule flagged", any("bad weekly" in x for x in agenda.lint(p)))

    print("scaffold + due_projects:")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "alpha").mkdir()
        (root / "alpha" / "project.md").write_text(
            "---\ntype: project\n---\n", encoding="utf-8"
        )
        (root / "beta").mkdir()
        (root / "beta" / "project.md").write_text(
            "---\ntype: project\n---\n", encoding="utf-8"
        )
        created = agenda.scaffold_all(root, today)
        check("scaffolded both", set(created) == {"alpha", "beta"})
        check(
            "alpha AGENDA dormant",
            agenda.is_enabled(agenda.parse_agenda(root / "alpha" / "AGENDA.md")[0])
            is False,
        )
        check("no due projects while dormant", agenda.due_projects(root, today) == [])
        # Enable alpha with a due task.
        ag = (root / "alpha" / "AGENDA.md").read_text(encoding="utf-8")
        ag = ag.replace("enabled: false", "enabled: true").replace(
            "## Tasks\n",
            "## Tasks\n\n### [T1] go\n- status:: clear\n- schedule:: nightly\n- next_due:: 2026-06-28\n- acceptance:: do it.\n\n",
            1,
        )
        (root / "alpha" / "AGENDA.md").write_text(ag, encoding="utf-8")
        check("alpha now due", agenda.due_projects(root, today) == ["alpha"])
        # scaffold_all is idempotent.
        check("scaffold_all idempotent", agenda.scaffold_all(root, today) == [])

    print("inbox grooming + append (CoS proposals seam):")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "q").mkdir()
        ag = root / "q" / "AGENDA.md"
        agenda.scaffold(ag, "q", today)
        # enable it but leave it with no Tasks and a pristine (comment-only) Inbox
        ag.write_text(
            ag.read_text(encoding="utf-8").replace("enabled: false", "enabled: true"),
            encoding="utf-8",
        )
        check(
            "pristine inbox is not groomable",
            agenda.inbox_has_groomable_content(ag.read_text(encoding="utf-8")) is False,
        )
        check(
            "enabled + empty inbox + no tasks => not due",
            agenda.project_is_due(ag, today) is False,
        )
        n = agenda.append_inbox_items(
            ag,
            [
                "[cos] draft reply to supervisor — overdue",
                "[cos] (→ vision) triage failed imports",
            ],
            today,
        )
        check("appended both inbox items", n == 2)
        body = ag.read_text(encoding="utf-8")
        check("inbox now groomable", agenda.inbox_has_groomable_content(body) is True)
        check(
            "enabled + inbox content => due", agenda.project_is_due(ag, today) is True
        )
        check(
            "items landed under ## Inbox",
            "- [cos] draft reply to supervisor — overdue" in body,
        )
        check(
            "other sections preserved",
            "## Tasks" in body and "## Clarifications" in body and "## Run log" in body,
        )
        n2 = agenda.append_inbox_items(
            ag,
            ["[cos] draft reply to supervisor — overdue", "[cos] a brand new item"],
            today,
        )
        check("dedupe skips existing, appends only the new one", n2 == 1)
        check(
            "no duplicated inbox line",
            ag.read_text(encoding="utf-8").count(
                "- [cos] draft reply to supervisor — overdue"
            )
            == 1,
        )
        ag.write_text(
            ag.read_text(encoding="utf-8").replace("enabled: true", "enabled: false"),
            encoding="utf-8",
        )
        check(
            "disabled + inbox content => not due",
            agenda.project_is_due(ag, today) is False,
        )

    print("desk status (CoS brief agents view):")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)

        def _mk(slug, enabled):
            (root / slug).mkdir()
            p = root / slug / "AGENDA.md"
            agenda.scaffold(p, slug, today)
            if enabled:
                p.write_text(
                    p.read_text(encoding="utf-8").replace(
                        "enabled: false", "enabled: true"
                    ),
                    encoding="utf-8",
                )
            return p

        pa = _mk("alpha", True)  # enabled: a due task + a routed handoff in its inbox
        pa.write_text(
            pa.read_text(encoding="utf-8").replace(
                "## Tasks\n",
                "## Tasks\n\n### [T1] go\n- status:: clear\n- schedule:: nightly\n- next_due:: 2026-06-28\n- acceptance:: do.\n\n",
                1,
            ),
            encoding="utf-8",
        )
        agenda.append_inbox_items(pa, ["[from:cos] handle this — why"], today)
        pb = _mk("beta", True)  # enabled: a blocked task
        pb.write_text(
            pb.read_text(encoding="utf-8").replace(
                "## Tasks\n",
                "## Tasks\n\n### [T1] x\n- status:: blocked\n- schedule:: once\n- blocked_reason:: host\n\n",
                1,
            ),
            encoding="utf-8",
        )
        _mk("gamma", False)  # dormant

        st = agenda.desk_status(root, today)
        check("three desks", len(st) == 3)
        check(
            "active desks sorted before dormant",
            [s["slug"] for s in st] == ["alpha", "beta", "gamma"],
        )
        a = next(s for s in st if s["slug"] == "alpha")
        check("alpha due counted", a["due"] == 1)
        check(
            "alpha routed handoff detected",
            a["inbox_routed"] == 1 and a["routed_sources"] == ["cos"],
        )
        check(
            "beta blocked counted",
            next(s for s in st if s["slug"] == "beta")["blocked"] == 1,
        )
        check(
            "gamma dormant",
            next(s for s in st if s["slug"] == "gamma")["enabled"] is False,
        )
        rendered = agenda.format_desk_status(st)
        check("renders active line", "**alpha** (active)" in rendered)
        check("renders routed source", "routed ← cos" in rendered)
        check("renders blocked", "1 blocked" in rendered)
        check("renders dormant tail", "dormant (1): gamma" in rendered)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
