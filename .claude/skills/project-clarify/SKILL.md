---
name: project-clarify
description: Resolve the open questions the nightly project-runner could not decide alone. Use when the user runs /project-clarify, asks to clear project clarifications, wants to answer the runner's questions, or after a morning roll-up reports clarifications opened. Interviews the user one task at a time, then flips each resolved task back to clear so the next nightly run executes it.
---

# Project clarifier

The nightly project-runner (`wiki-project-runner`) runs unattended, so it never guesses: any
task it cannot decide with certainty is parked as `status:: needs-clarification` in that
project's `projects/<slug>/AGENDA.md`, with the specific open questions recorded. This skill is
the **interactive** counterpart — you sit with the operator, answer those questions, and hand the
task back to the runner as `clear`.

This is the attended half of the operator's "Interview on Uncertainty" rule: the runner logs the
open question (unattended); you conduct the interview (attended).

## When to use

The operator runs `/project-clarify` (optionally `/project-clarify <slug>` to scope to one
project), asks to "resolve clarifications / answer the runner's questions", or a morning
project-runner roll-up reported `Clarifications opened`.

## Procedure

1. **Gather** the pending clarifications:

   ```bash
   python3 tools/wiki.py project agenda clarifications --json
   ```

   Each entry is `{slug, id, title, questions}`. If the operator named a project, filter to that
   slug. If the list is empty, say so and stop.

2. **Interview, one task at a time.** For each task, show its `title` and the open `questions`,
   then ask the operator. Use the `AskUserQuestion` tool when the questions have a small set of
   concrete options; otherwise just ask in plain text. Keep it tight — do not re-derive the whole
   task, only resolve what is genuinely open. Batch the questions for a single task together.

3. **Write the resolution into the task.** Open `projects/<slug>/AGENDA.md` and edit the task's
   `### [id]` block so it is now executable without ambiguity:
   - Rewrite `acceptance::` into a single objective, self-verifiable line reflecting the answers.
   - Adjust `schedule::` if the operator clarified the cadence (`once` · `nightly` · `weekly:Mon` ·
     `every:3d` · `weekdays:Mon,Wed,Fri`).
   - Set/clear `output::` and add a `notes::` line capturing any decision the runner will need.
   - Do **not** hand-edit `status::`, `next_due::`, the `questions::` block, or the
     `## Clarifications` entry — the next step does that mechanically.

   If a task turns out to need a non-allowlisted network host, that is not a clarification — leave
   it for the runner to mark `blocked` (or tell the operator to add the host to
   `.devcontainer/allowlist.extra.txt` and rebuild). If the operator decides a task is not worth
   doing, set `status:: paused` (or delete the block) instead of resolving it.

4. **Flip it back to clear:**

   ```bash
   python3 tools/wiki.py project agenda resolve <slug> <id>
   ```

   This sets `status:: clear`, sets `next_due` to today (so the next nightly run executes it),
   removes the `questions::` block and the matching `## Clarifications` entry, and appends a
   run-log line. It is the only safe way to make these transitions — never replicate them by hand.

5. **Confirm.** Report which tasks are now `clear` and remind the operator they will run on the
   next eligible nightly tick (only for `enabled: true` projects — if the project is still dormant,
   note that `python3 tools/wiki.py project agenda enable <slug>` is needed to actually run them).

## Boundaries

- Write only inside `projects/<slug>/` — this skill edits AGENDA.md and nothing in `wiki/`/`raw/`.
- One project's clarifications at a time; finish (or explicitly skip) a task before moving on.
- If grooming the Inbox surfaces work that is already clear, you may add it as a `clear` task
  directly — but prefer to let the nightly runner groom, and keep this skill focused on resolving
  open questions.
