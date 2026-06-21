#!/usr/bin/env python3
"""Wiki agent wrapper - invoke the Claude Code CLI with the vault's custom agents.

Agent definitions live in .claude/agents/*.md (Claude subagent format). This
launcher injects the agent body as a headless `claude -p` system prompt and adds
the orchestration Claude doesn't do natively: enhance loops with strategy
cycling, the CoS live-context gather, PDF pre-extraction, inbox promotion, and
auto-logging."""

import argparse
import datetime as _dt
import itertools
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = ROOT / ".claude" / "agents"
TOOLS_DIR = ROOT / "tools"


def _in_container() -> bool:
    """True when running inside the Brain devcontainer.

    DEVCONTAINER=true is set by the sandbox launcher (.devcontainer/bin/agent
    passes -e DEVCONTAINER=true). /.dockerenv exists only under Docker, not
    apple/container, so the env var is the reliable signal here. Either suffices.
    """
    return os.environ.get("DEVCONTAINER") == "true" or Path("/.dockerenv").exists()


def _enforce_container(args) -> int | None:
    """Refuse to invoke the agent CLIs outside the egress-locked sandbox.

    This runner shells out to the claude CLI, which must only run inside the
    container. --help and --debug (a dry run that prints the command without
    executing) are still allowed on the host. Set BRAIN_AGENT_ALLOW_HOST=1 to
    override (not recommended).
    """
    if _in_container() or args.debug or os.environ.get("BRAIN_AGENT_ALLOW_HOST") == "1":
        return None
    sys.stderr.write(
        "wiki-agent.py must run inside the Brain devcontainer (it invokes the\n"
        "agent CLIs in the egress-locked sandbox). Run it via:\n"
        "    brain-wiki <agent> [args]     e.g. brain-wiki enhance --strategy coverage\n"
        "Or pass --debug to dry-run on the host. Set BRAIN_AGENT_ALLOW_HOST=1 to\n"
        "override the sandbox requirement (not recommended).\n"
    )
    return 2


def _resolve_pdf_to_markdown(path_str: str) -> str:
    """If path_str points to a PDF in raw/sources/, return its raw/sources-text/ markdown sibling.

    Auto-runs `python3 tools/wiki.py preprocess --pdf <path>` if the sibling is missing
    or older than the PDF. Returns the original path if it is not a PDF or extraction fails.
    """
    if not path_str or not path_str.lower().endswith(".pdf"):
        return path_str

    pdf_abs = (
        (ROOT / path_str).resolve()
        if not Path(path_str).is_absolute()
        else Path(path_str).resolve()
    )
    if not pdf_abs.exists():
        return path_str

    if TOOLS_DIR not in [Path(p) for p in sys.path]:
        sys.path.insert(0, str(TOOLS_DIR))
    try:
        from wiki_ingest import extract_pdf_to_markdown  # type: ignore[import]
    except ImportError as exc:
        print(f"Warning: could not import wiki_ingest.extract_pdf_to_markdown: {exc}")
        return path_str

    try:
        text_path, _status = extract_pdf_to_markdown(pdf_abs, force=False)
        print(f"Pre-extracted PDF -> {text_path.relative_to(ROOT)}")
        return str(text_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: PDF preprocess failed for {pdf_abs.name}: {exc}")
        print("Falling back to attaching the original PDF.")
        return str(pdf_abs)


AGENT_FILES = {
    "quality": "wiki-quality-reviewer.md",
    "verify": "wiki-source-verifier.md",
    "ingest": "wiki-ingest.md",
    "contradict": "wiki-contradiction-detector.md",
    "search": "wiki-search.md",
    "enhance": "wiki-enhancer.md",
    "cos": "wiki-cos.md",
    "challenge": "wiki-challenge.md",
    "connect": "wiki-connect.md",
    "emerge": "wiki-emerge.md",
    "discover": "wiki-idea-discovery.md",
}

CLI_OPTIONS = {
    "claude": "claude",
}

# Per-agent runtime permissions, used to build the claude headless
# --allowedTools / --add-dir flags. The same profiles are mirrored in the
# `tools:` frontmatter of .claude/agents/*.md for interactive subagent use.
#   shell:         grant a curated read-only shell command set.
#   write:         grant the file write/edit tool (+ extra shell tools needed to manage files).
#   writable_dirs: paths (relative to ROOT) the agent must be able to modify. Empty for read-only
#                  agents. CLIs that scope writes to specific directories use this list.
AGENT_PERMISSIONS: dict[str, dict] = {
    "quality": {"shell": False, "write": False, "writable_dirs": []},
    "verify": {"shell": False, "write": False, "writable_dirs": []},
    "search": {"shell": True, "write": False, "writable_dirs": []},
    "contradict": {"shell": True, "write": False, "writable_dirs": []},
    "ingest": {"shell": True, "write": True, "writable_dirs": ["wiki"]},
    "enhance": {
        "shell": True,
        "write": True,
        # The author profile mounts only wiki/ RW (raw/ is read-only in the
        # sandbox), so raw/sources-text cannot be a write target here.
        # Pre-extraction to raw/sources-text is a host/ingest-time step.
        "writable_dirs": ["wiki"],
    },
    "cos": {"shell": True, "write": False, "writable_dirs": []},
    # Read-only "thinking" agents — search the vault, emit text, never write.
    "challenge": {"shell": True, "write": False, "writable_dirs": []},
    "connect": {"shell": True, "write": False, "writable_dirs": []},
    "emerge": {"shell": True, "write": False, "writable_dirs": []},
    "discover": {"shell": True, "write": False, "writable_dirs": []},
}

# Shell commands granted to any agent with shell access. Strictly read-only;
# helper utilities for navigation, text inspection, search, and wiki tooling.
# `set` stays so multi-line scripts prefixed with `set -euo pipefail` match
# the allowlist on their first command identifier.
READ_ONLY_SHELL_COMMANDS = (
    "set",
    "ls",
    "find",
    "grep",
    "cat",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "cut",
    "tr",
    "date",
    "python3",
    "qmd",
)

# Shell commands granted only to write-capable agents. Filesystem mutators
# (mkdir/touch/mv/cp) and text editors used in scripted edits (sed/awk in-place).
# NOTE: sed/awk are full scripting engines whose blast radius is bounded only by
# the container MOUNT profile, NOT this allowlist (a `sed -i` can rewrite any
# writable path, not just wiki/). They are safe only because write agents run
# under the author/scoped profile (wiki/ RW, everything else RO). Never run a
# write agent under the master profile (whole workspace RW), or sed/awk could
# rewrite raw/ or projects/ despite the agent's prompt forbidding it.
WRITE_SHELL_COMMANDS = ("touch", "mkdir", "mv", "cp", "sed", "awk")


def _agent_permissions(agent: str) -> dict:
    """Return the permission profile for an agent, defaulting to read-only."""
    return AGENT_PERMISSIONS.get(
        agent, {"shell": False, "write": False, "writable_dirs": []}
    )


STRATEGY_HINTS = {
    "coverage": (
        "Selection strategy: use **Strategy C — Sparse coverage** from "
        "your wiki-enhancer instructions. Run `python3 tools/wiki.py coverage --json` "
        "and pick the topic with the lowest coverage score where a dense "
        "source exists."
    ),
    "random": (
        "Selection strategy: use **Strategy B — Random page** from "
        "your wiki-enhancer instructions. Pick a random concept page; first glance "
        "at `tail -20 wiki/log.md` to avoid repeating recent work."
    ),
    "stub": (
        "Selection strategy: use **Strategy A — Shallowest stub** from "
        "your wiki-enhancer instructions. Pick the concept page with the fewest lines."
    ),
    "source-gap": (
        "Selection strategy: use **Strategy D — Source-driven gap discovery** "
        "from your wiki-enhancer instructions. This is SOURCE-FIRST: do NOT start by "
        "picking a shallow concept page. Instead: (1) pick a source document "
        "from wiki/sources/ — either random "
        "(`python3 -c \"import random, glob; print(random.choice(glob.glob('wiki/sources/src-*.md')))\"`) "
        "or a reasoned choice (least-recently-enhanced per `tail -40 wiki/log.md`, "
        "or one whose Coverage Notes admit untouched chapters); "
        "(2) read its raw text at raw/sources-text/<stem>.md and enumerate "
        "15-40 candidate topics from the source's own chapter/section/named-unit "
        "structure — not from the wiki; "
        "(3) cross-check each enumerated topic against wiki/concepts/ using "
        "`ls`, `qmd query`, and `wiki.py search`, classifying as MISSING "
        "(no page exists anywhere), BAD (page exists but misrepresents the "
        "source), THIN (page exists, correct, but <100 lines vs dense source "
        "treatment), or COVERED; "
        "(4) prioritize MISSING and BAD over THIN — a run that produces only "
        "THIN expansions has drifted into Strategy A; restart with a different "
        "source if your gap list has zero MISSING or BAD entries. Pick 2-5 "
        "highest-value gaps with at least one MISSING or BAD if available."
    ),
    "auto": (
        "Selection strategy: use **Strategy E — Mixed / agent-chosen** from "
        "your wiki-enhancer instructions. Read `tail -30 wiki/log.md`, glance at the "
        "concept page size distribution, and pick whichever of Strategies "
        "A-D would most benefit the wiki right now. Avoid the strategy "
        "used in the most recent log entry."
    ),
}

ALTERNATE_CYCLE = ["coverage", "source-gap", "random", "stub"]

# ---------------------------------------------------------------------------
# Chief of Staff — live context gathering
# ---------------------------------------------------------------------------


def _gather_cos_context(mode: str, project_filter: str | None) -> str:
    """Gather live project state and inject it as context for the CoS agent.

    Reads project TODOs, wiki log tail, and inbox listing from the vault.
    Runs inside the container where ROOT is the live workspace.
    """
    today = _dt.date.today()
    parts: list[str] = [
        "## Live context",
        f"Date: {today.strftime('%Y-%m-%d (%A)')}",
        "",
    ]

    # --- Operator profile (who we're advising) -------------------------------
    # Inject wiki/entities/user-background.md so the CoS calibrates its brief to
    # the operator's background, goals, and working preferences.
    operator_page = ROOT / "wiki" / "entities" / "user-background.md"
    if operator_page.exists():
        try:
            parts.append("## Operator profile (wiki/entities/user-background.md)")
            parts.append(operator_page.read_text(encoding="utf-8"))
            parts.append("")
        except OSError:
            pass

    # --- Project task lists --------------------------------------------------
    projects_root = ROOT / "projects"
    project_dirs: list[Path] = []
    if projects_root.is_dir():
        project_dirs = sorted(
            [
                d
                for d in projects_root.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            ],
            key=lambda d: d.name,
        )
    if project_filter:
        project_dirs = [d for d in project_dirs if d.name == project_filter]

    active_projects: list[str] = []
    todo_blocks: list[str] = []
    for proj_dir in project_dirs:
        todo_file = proj_dir / "TODO.md"
        if not todo_file.exists():
            continue
        try:
            content = todo_file.read_text(encoding="utf-8")
        except OSError:
            continue
        open_items = [ln for ln in content.splitlines() if "- [ ]" in ln]
        if not open_items:
            continue
        active_projects.append(proj_dir.name)
        todo_blocks.append(f"\n### {proj_dir.name} ({len(open_items)} open tasks)")
        for item in open_items[:40]:
            todo_blocks.append(item)
        if len(open_items) > 40:
            todo_blocks.append(
                f"  ... ({len(open_items) - 40} more open tasks not shown)"
            )

    parts.append(
        f"Active projects with open tasks: {', '.join(active_projects) or '(none found)'}"
    )
    parts.append("")
    parts.append("## Open tasks by project")
    parts.extend(todo_blocks)

    # --- Wiki log tail (recent activity) -------------------------------------
    if mode in ("brief", "surface", "status"):
        log_file = ROOT / "wiki" / "log.md"
        if log_file.exists():
            try:
                log_lines = log_file.read_text(encoding="utf-8").splitlines()
                recent = log_lines[-60:]
                parts.append("\n## Recent wiki activity (last entries in wiki/log.md)")
                parts.extend(recent)
            except OSError:
                parts.append("\n## Recent wiki activity — (could not read wiki/log.md)")

    # --- Scheduler health (nightly batch run status) -------------------------
    # The dispatcher's run ledger lives outside the vault (~/.brain), unreachable
    # from this sandbox; dispatch.py mirrors a compact summary into wiki/reports
    # so the CoS can flag automation failures the operator would otherwise only
    # catch as a transient macOS notification.
    if mode in ("brief", "status"):
        status_file = ROOT / "wiki" / "reports" / "schedule-status.md"
        if status_file.exists():
            try:
                parts.append("\n## Scheduler health (nightly batch)")
                parts.append(status_file.read_text(encoding="utf-8"))
            except OSError:
                pass

    # --- Inbox listing -------------------------------------------------------
    inbox_dir = ROOT / "raw" / "inbox"
    if inbox_dir.is_dir():
        # Stat each file defensively: on synced storage (iCloud) a file can vanish
        # between iterdir() and stat(), which must not crash the brief.
        inbox_entries: list[tuple[Path, os.stat_result]] = []
        for f in inbox_dir.iterdir():
            if f.name.startswith("."):
                continue
            try:
                inbox_entries.append((f, f.stat()))
            except OSError:
                continue
        inbox_entries.sort(key=lambda fs: fs[1].st_mtime, reverse=True)
        parts.append(f"\n## Inbox: raw/inbox/ ({len(inbox_entries)} files)")
        for f, st in inbox_entries:
            size = st.st_size
            size_str = f"{size // 1024}KB" if size >= 1024 else f"{size}B"
            parts.append(f"- {f.name} ({size_str})")

        if mode == "inbox" and inbox_entries:
            parts.append("\n## Inbox file previews")
            for f, _st in inbox_entries[:8]:
                if f.suffix.lower() in (".md", ".txt"):
                    try:
                        preview = f.read_text(encoding="utf-8").splitlines()[:30]
                        parts.append(f"\n### {f.name}")
                        parts.extend(preview)
                        parts.append("...")
                    except OSError:
                        parts.append(f"\n### {f.name} (could not read)")
    else:
        parts.append("\n## Inbox: raw/inbox/ — directory not found")

    return "\n".join(parts)


def _resolve_strategy(strategy: str | None, iteration_index: int) -> str | None:
    """Resolve the per-iteration strategy.

    'alternate' cycles through ALTERNATE_CYCLE (coverage -> source-gap ->
    random -> stub) across iterations.
    """
    if strategy == "alternate":
        return ALTERNATE_CYCLE[iteration_index % len(ALTERNATE_CYCLE)]
    return strategy


# Effort currently maps to no claude CLI flags (the headless CLI inherits the
# session/settings effort level); the arg is kept for interface stability with
# brain-wiki / dispatch.py callers.
EFFORT_MAP: dict[str, list[str]] = {
    "low": [],
    "medium": [],
    "high": [],
    "xhigh": [],
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wiki agent wrapper - invoke AI agents with configurable CLI/model/effort",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run quality review
  python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md

  # Run with a specific Claude model
  python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md --model opus

  # Run deep analysis with high effort
  python3 tools/agents/wiki-agent.py verify --source wiki/sources/my-source.md --effort high

  # Ingest a PDF
  python3 tools/agents/wiki-agent.py ingest --source raw/sources/paper.pdf

  # Background enhancement loop (alternate strategies, never stop, survives errors)
  python3 tools/agents/wiki-agent.py enhance --background &
  disown
  tail -f wiki/log/bg-enhance-*.log     # follow progress
  kill <pid printed at startup>         # stop it
""",
    )

    parser.add_argument(
        "agent",
        choices=list(AGENT_FILES.keys()),
        help="Agent to invoke",
    )
    parser.add_argument("--page", help="Page path relative to wiki/")
    parser.add_argument("--source", help="Source path (for verify/ingest/enhance)")
    parser.add_argument(
        "--topic",
        help="Topic page path (for enhance, relative to wiki/)",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Enhance mode: shorthand for --strategy coverage (sparsest target from wiki.py coverage)",
    )
    parser.add_argument(
        "--strategy",
        choices=["coverage", "random", "stub", "source-gap", "auto", "alternate"],
        help=(
            "Enhance mode: selection strategy when no concrete target is given. "
            "'coverage' = sparsest topic, 'random' = random concept page, "
            "'stub' = shallowest stub, 'source-gap' = find topics in a source "
            "that are missing or shallow in the wiki and add them, "
            "'auto' = let the agent pick the most useful strategy this run, "
            "'alternate' = cycle coverage -> source-gap -> random -> stub "
            "across iterations. Defaults to 'alternate' when --iterations > 1."
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Enhance mode: run the agent N times in a loop (default: 1).",
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        help="Enhance mode: iterate indefinitely until Ctrl-C / kill (overrides --iterations).",
    )
    parser.add_argument(
        "--continue-on-error",
        dest="continue_on_error",
        action="store_true",
        help="Enhance mode: do not abort the loop when an iteration fails; log and continue.",
    )
    parser.add_argument(
        "--max-failures",
        dest="max_failures",
        type=int,
        default=5,
        help=(
            "Enhance mode: abort after N consecutive failed iterations even when "
            "--continue-on-error is set (default: 5). Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--log-file",
        dest="log_file",
        help=(
            "Path to a log file. Stdout and stderr (including subprocess output) "
            "are redirected here in append mode."
        ),
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help=(
            "Enhance mode: convenience flag that enables --continue-on-error, "
            "--forever (unless --iterations N is set), and auto-routes output to "
            "wiki/log/bg-enhance-<timestamp>.log. Combine with shell `&` and "
            "`disown`, or `nohup ... &`, to detach from your shell."
        ),
    )
    parser.add_argument(
        "--pdf",
        help="Original PDF to attach when enhancing (defaults to --source if it ends with .pdf)",
    )
    parser.add_argument(
        "--cli",
        choices=list(CLI_OPTIONS.keys()),
        default="claude",
        help="CLI to use (default: claude)",
    )
    parser.add_argument(
        "--model",
        help="Model to use (defaults to CLI's best option)",
    )
    parser.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh"],
        default="high",
        help="Thinking effort (default: high)",
    )
    parser.add_argument(
        "--system",
        help="Additional system prompt to append",
    )
    parser.add_argument(
        "--prompt",
        help="Custom prompt (overrides auto-generated)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the full command without executing",
    )

    # CoS-specific args
    parser.add_argument(
        "--mode",
        choices=["brief", "status", "surface", "inbox"],
        default="brief",
        help=(
            "CoS mode: 'brief' = daily brief (default), 'status' = project status report, "
            "'surface' = commitment surface, 'inbox' = inbox triage."
        ),
    )
    parser.add_argument(
        "--project",
        help="CoS status/surface mode: scope to this project slug (folder name under projects/).",
    )

    return parser


def get_default_model(cli: str) -> str:
    """Get default model for CLI."""
    defaults = {
        "claude": "sonnet",
    }
    return defaults.get(cli, "default")


def validate_cli(cli: str) -> bool:
    """Check that the chosen CLI is installed."""
    return shutil.which(cli) is not None


def build_prompt(
    agent: str,
    page: str,
    source: str,
    custom: str,
    strategy: str | None = None,
    mode: str = "brief",
    project: str | None = None,
) -> str:
    """Build the task prompt - simple description, full instructions come from the agent definition via system prompt."""
    if custom:
        return custom

    # Chief of Staff: mode-specific prompts (no file attachments)
    if agent == "cos":
        cos_prompts = {
            "brief": "Review the live context in your system prompt and produce a full chief-of-staff daily brief.",
            "status": (
                f"Produce a status report for project: {project or page or '(no project specified — infer from context)'}. "
                "Read the project's TODO.md and project.md for full context."
            ),
            "surface": "Surface all active commitments and at-risk items across all projects in the live context.",
            "inbox": (
                "Triage each file in raw/inbox/ from the live context. "
                "Read file previews as needed, then produce a routing table and the exact commands to execute."
            ),
        }
        return cos_prompts.get(mode, cos_prompts["brief"])

    prompts = {
        "quality": f"Analyze the wiki page at: {page}",
        "verify": f"Verify claims in the wiki source page: {source}",
        "ingest": f"Process new source material: {source}",
        "contradict": "Find potential contradictions across wiki pages",
        "search": f"Search the wiki for: {source if source else page}",
        "enhance": (
            f"Enhance wiki coverage. Target: "
            f"{page or source or 'auto-pick sparsest area via python3 tools/wiki.py coverage'}. "
            f"Re-read the source PDF if available. Fix correctness, expand sparse "
            f"sections, create new concept pages where the source is dense, and "
            f"strengthen cross-topic interlinking. Follow your wiki-enhancer instructions."
        ),
        "challenge": (
            "Red-team this position against the operator's own vault history: "
            f"{source or page or '(no explicit position given — report that one is required)'}"
        ),
        "connect": (
            "Bridge these two domains using the wiki link graph and produce 3-5 "
            f"non-obvious connection ideas. Domain A: {source or '(missing)'}. "
            f"Domain B: {page or '(missing — report that a second domain is required)'}"
        ),
        "emerge": (
            "Surface unnamed patterns from recent wiki activity. Timeframe: "
            f"{source or 'last 30 days'}."
        ),
        "discover": (
            "Rank 3-5 next-direction candidates from existing vault material "
            "(open questions, ungraduated ideas, orphan and sparse pages)."
        ),
    }

    base = prompts.get(agent, "Analyze and report.")
    if agent == "enhance" and strategy and strategy in STRATEGY_HINTS:
        base = f"{base}\n\n{STRATEGY_HINTS[strategy]}"
    if agent == "contradict" and page:
        # Fold the optional scope into the prompt. (It used to be emitted as a
        # bogus `--domain <page>` in extra_args, which no CLI understands.)
        base = f"{base}\n\nScope: prioritise contradictions involving the page or domain '{page}'."
    return base


def _prepare_system_prompt(agent_file: Path, system_addon: str) -> str:
    """Return the system-prompt text: the agent instructions plus the addon (if any).

    Strips the YAML frontmatter from the .claude/agents/*.md definition: the
    frontmatter (name/description/tools) is consumed by Claude Code's native
    subagent loader; only the body below it is the system prompt.
    """
    agent_instructions = agent_file.read_text(encoding="utf-8")
    # Tolerate a UTF-8 BOM and CRLF line endings from non-Unix editors so the
    # frontmatter still strips (otherwise the YAML leaks into the system prompt).
    if agent_instructions.startswith(chr(0xFEFF)):  # UTF-8 BOM
        agent_instructions = agent_instructions[1:]
    agent_instructions = agent_instructions.replace("\r\n", "\n")
    if agent_instructions.startswith("---\n"):
        end = agent_instructions.find("\n---\n", 4)
        if end != -1:
            agent_instructions = agent_instructions[end + 5 :].lstrip("\n")
    if not system_addon:
        return agent_instructions
    return f"{agent_instructions}\n\nAdditional context:\n{system_addon}"


def _build_allowed_tools(perms: dict) -> list[str]:
    """The `--allowedTools` list for a permission profile. Read/Grep/Glob are
    always granted (read-only navigation), so bash:false agents like quality and
    verify can still check links and find orphans. shell and write add to it.
    `Bash(<cmd> *)` is Claude Code's prefix-match form (confirmed against the
    permissions docs); kept pure so tools/tests/test_agent.py can assert it."""
    tools = ["Read", "Grep", "Glob"]
    if perms["shell"]:
        tools.extend(f"Bash({c} *)" for c in READ_ONLY_SHELL_COMMANDS)
        if perms["write"]:
            tools.extend(f"Bash({c} *)" for c in WRITE_SHELL_COMMANDS)
    if perms["write"]:
        tools.extend(["Edit", "Write"])
    return tools


def invoke_agent(
    agent: str,
    cli: str,
    model: str,
    effort: str,
    prompt: str,
    system_addon: str,
    extra_args: list,
    debug: bool = False,
) -> int:
    """Invoke the AI agent."""
    agent_file = AGENTS_DIR / AGENT_FILES[agent]

    if not agent_file.exists():
        print(f"Error: Agent file not found: {agent_file}")
        return 1

    effort_flags = EFFORT_MAP.get(effort, [])

    # claude -p --model MODEL --system-prompt "text" "task"
    system_text = _prepare_system_prompt(agent_file, system_addon)
    perms = _agent_permissions(agent)
    cmd = ["claude", "-p"]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(effort_flags)
    cmd.extend(["--system-prompt", system_text])
    # Build the --allowedTools list to match the agent's profile.
    cmd.extend(["--allowedTools", ",".join(_build_allowed_tools(perms))])
    # Hard "no subagents": a wiki agent must never spawn its own
    # sub-agent. The orchestrator (this script) is the ONLY multi-agent
    # layer; a self-spawned subagent would be a privilege/injection vector
    # (e.g. a read-only agent reaching for a write-capable one) — and the
    # container mount is the kernel-level backstop regardless, since any
    # subagent runs in THIS profile's container. Task is already absent
    # from the allowlist above; deny it explicitly so the intent survives
    # future edits to allowed_tools.
    cmd.extend(["--disallowedTools", "Task"])
    # Scope writes to the agent's writable_dirs. Claude reads from the
    # whole repo by default; --add-dir is needed when the writable
    # target lives outside the launching directory.
    for d in perms.get("writable_dirs", []):
        cmd.extend(["--add-dir", str(ROOT / d)])
    # Permission mode: acceptEdits allows non-interactive edits within
    # the allowed tool set; default still prompts for anything else.
    cmd.extend(["--permission-mode", "acceptEdits" if perms["write"] else "default"])
    # extra_args are file paths. claude has no attachment flag and keeps only
    # the FIRST positional, silently dropping the rest — so passing paths as
    # positionals would discard the real instruction. Fold them into the
    # prompt as a "Files to read:" block (claude opens them via Read).
    claude_prompt = prompt
    if extra_args:
        paths = "\n".join(f"- {p}" for p in extra_args)
        claude_prompt = f"{prompt}\n\nFiles to read:\n{paths}"
    cmd.append(claude_prompt)

    print(f"Invoking {agent} agent with {cli}" + (f" ({model})" if model else ""))
    print(f"Effort: {effort}")
    print(f"Agent: {agent_file.name}")
    print()

    if debug:
        import shlex

        print("DEBUG command:")
        print(shlex.join(cmd))
        print()
        for i, part in enumerate(cmd):
            print(f"  argv[{i}]: {part}")
        return 0

    try:
        result = subprocess.run(cmd)
    except OSError as exc:
        # e.g. the CLI binary was removed after validate_cli() passed (TOCTOU),
        # or exec failed. Return a non-zero rc so the --forever loop's error
        # handling can catch it instead of an unhandled traceback aborting it.
        print(f"Error: failed to launch {cli}: {exc}")
        return 127
    return result.returncode


def _promote_inbox_pdf(source_path: str) -> str:
    """Promote an ingested inbox PDF to raw/sources/; return its canonical path.

    The ingest agent runs sandboxed (only wiki/ is writable), so the launcher
    performs the move after a successful run. Returns the (possibly unchanged)
    path the rest of the flow should treat as canonical for logging.
    """
    if not source_path.lower().endswith(".pdf"):
        return source_path

    pdf_abs = (
        (ROOT / source_path).resolve()
        if not Path(source_path).is_absolute()
        else Path(source_path).resolve()
    )
    if not pdf_abs.exists():
        return source_path

    if TOOLS_DIR not in [Path(p) for p in sys.path]:
        sys.path.insert(0, str(TOOLS_DIR))
    try:
        from wiki_ingest import promote_inbox_pdf  # type: ignore[import]
    except ImportError as exc:
        print(f"Warning: could not import wiki_ingest.promote_inbox_pdf: {exc}")
        return source_path

    try:
        dest = promote_inbox_pdf(pdf_abs)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not promote {pdf_abs.name} to raw/sources/: {exc}")
        return source_path

    if dest is None:
        return source_path
    rel = dest.relative_to(ROOT).as_posix()
    print(f"Promoted ingested PDF: raw/inbox/{dest.name} -> {rel}")
    return rel


def run_agent(args, strategy: str | None = None) -> int:
    """Run the specified agent."""
    # Build prompt
    page = args.page or ""
    source = args.source or ""
    cos_mode = getattr(args, "mode", "brief")
    cos_project = getattr(args, "project", None)
    prompt = build_prompt(
        args.agent,
        page,
        source,
        args.prompt or "",
        strategy=strategy,
        mode=cos_mode,
        project=cos_project,
    )

    # Get model
    model = args.model or get_default_model(args.cli)
    effort = args.effort

    # Build extra args based on agent — resolve to absolute paths for -f flags
    extra_args = []
    if args.agent == "quality" and args.page:
        extra_args = [str((ROOT / args.page).resolve())]
    elif args.agent == "verify" and args.source:
        extra_args = [str((ROOT / args.source).resolve())]
    elif args.agent == "ingest" and args.source:
        # PDFs get pre-extracted to raw/sources-text/*.md so the model can read them.
        extra_args = [_resolve_pdf_to_markdown(args.source)]
    elif args.agent == "search":
        # The query IS the prompt (see the prompt builder). Only attach it as a
        # file to read when it is an actual existing path; a free-text search term
        # must never become a bogus "Files to read" entry that wastes a model turn.
        query = args.source or args.page or ""
        extra_args = (
            [str((ROOT / query).resolve())] if query and (ROOT / query).exists() else []
        )
    elif args.agent == "enhance":
        # Attach whatever is relevant: target wiki page(s) + extracted source markdown.
        targets = []
        pdf_path = args.pdf or (
            args.source if args.source and args.source.endswith(".pdf") else ""
        )
        if pdf_path:
            targets.append(_resolve_pdf_to_markdown(pdf_path))
        if args.page:
            targets.append(str((ROOT / args.page).resolve()))
        if args.topic:
            targets.append(str((ROOT / args.topic).resolve()))
        if args.source and args.source != pdf_path:
            # Source may be a non-PDF (e.g. wiki/sources/src-*.md); attach as-is.
            targets.append(str((ROOT / args.source).resolve()))
        extra_args = targets

    if not validate_cli(args.cli):
        print(f"Error: CLI '{args.cli}' not found in PATH.")
        print(f"Available CLIs: {', '.join(CLI_OPTIONS.keys())}")
        return 1

    # CoS: gather live vault context and inject it into the system prompt.
    system_addon = args.system or ""
    if args.agent == "cos":
        print(
            f"[cos] Gathering live context (mode={cos_mode}"
            + (f", project={cos_project}" if cos_project else "")
            + ")..."
        )
        cos_ctx = _gather_cos_context(cos_mode, cos_project)
        system_addon = (system_addon + "\n\n" + cos_ctx).strip()

    rc = invoke_agent(
        args.agent,
        args.cli,
        model,
        effort,
        prompt,
        system_addon,
        extra_args,
        debug=args.debug,
    )

    if args.agent == "ingest" and rc == 0 and args.source:
        # Promote the PDF out of raw/inbox/ into raw/sources/ now that it is in the
        # wiki. The ingest agent writes its own richer wiki/log.md entry (step 4 of
        # wiki-ingest.md, like enhance), so the launcher no longer double-logs here.
        _promote_inbox_pdf(args.source)

    return rc


_STOP_REQUESTED = False


def _install_signal_handlers() -> None:
    """Set _STOP_REQUESTED on SIGINT/SIGTERM so the loop exits between iterations."""

    def _handler(signum, _frame):
        global _STOP_REQUESTED
        _STOP_REQUESTED = True
        name = signal.Signals(signum).name
        print(
            f"\n[wiki-agent] {name} received; finishing current iteration then exiting."
        )

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _redirect_output_to_log(log_path: Path) -> bool:
    """Send stdout, stderr, and inherited subprocess output to log_path (append).

    Returns False (leaving stdio untouched) when the log destination is not
    writable — e.g. a reader/scoped profile where the vault is mounted read-only.
    The run then continues with console output instead of crashing on EROFS.
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log_path, "a", buffering=1, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(
            f"[wiki-agent] WARN: cannot write log file {log_path} ({exc}); "
            "continuing with console output (read-only profile?).\n"
        )
        return False
    os.dup2(fh.fileno(), sys.stdout.fileno())
    os.dup2(fh.fileno(), sys.stderr.fileno())
    return True


def _ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Refuse to run the agent CLIs on the host — they belong in the sandbox.
    guard_rc = _enforce_container(args)
    if guard_rc is not None:
        return guard_rc

    # Validate required args
    if args.agent in ["quality", "verify", "ingest"]:
        required = "page" if args.agent == "quality" else "source"
        if not getattr(args, required):
            print(f"Error: --{required} required for {args.agent}")
            parser.print_help()
            return 1

    if args.agent == "search" and not (args.source or args.page):
        print("Error: search requires --source or --page (the query text).")
        parser.print_help()
        return 1

    # Thinking agents: position/domains come via --source (and --page for connect's
    # second domain). Warn on missing input rather than hard-failing — challenge can
    # still infer from an appended --system context block, connect cannot.
    if args.agent == "challenge" and not (args.source or args.page or args.prompt):
        print(
            'Warning: challenge works best with --source "<the position to red-team>". '
            "Without it the agent will report that a position is required."
        )
    if args.agent == "connect" and not (args.source and args.page):
        print(
            'Error: connect requires two domains — pass --source "<A>" and --page "<B>".'
        )
        parser.print_help()
        return 1

    if (
        args.agent == "cos"
        and args.mode == "status"
        and not args.project
        and not args.page
    ):
        print(
            "Warning: --mode status works best with --project <slug>. Continuing without a project filter."
        )

    if args.agent == "cos" and args.mode == "inbox":
        # Inbox mode reads the vault — must be in reader or broader profile; confirm.
        inbox_dir = ROOT / "raw" / "inbox"
        count = (
            len([f for f in inbox_dir.iterdir() if not f.name.startswith(".")])
            if inbox_dir.is_dir()
            else 0
        )
        print(f"[cos] Inbox mode: {count} files in raw/inbox/")

    # --coverage is a shorthand for --strategy coverage
    if args.agent == "enhance" and args.coverage and not args.strategy:
        args.strategy = "coverage"

    # --background bundles sensible defaults for fire-and-forget loops.
    iterations_explicit = args.iterations != 1
    if args.agent == "enhance" and args.background:
        args.continue_on_error = True
        if not iterations_explicit and not args.forever:
            args.forever = True
        if not args.log_file:
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            args.log_file = str(ROOT / "wiki" / "log" / f"bg-enhance-{stamp}.log")

    # Multi-iteration / forever runs without a concrete target default to alternating.
    has_concrete_target = bool(args.page or args.topic or args.source or args.pdf)
    if (
        args.agent == "enhance"
        and (args.iterations > 1 or args.forever)
        and not args.strategy
        and not has_concrete_target
    ):
        args.strategy = "alternate"

    if args.agent == "enhance" and not (has_concrete_target or args.strategy):
        print(
            "Error: enhance requires one of --page, --topic, --source, --pdf, "
            "--coverage, or --strategy"
        )
        parser.print_help()
        return 1

    if args.iterations < 1:
        print("Error: --iterations must be >= 1")
        return 1

    if args.log_file:
        log_path = Path(args.log_file).expanduser()
        if not log_path.is_absolute():
            log_path = ROOT / log_path
        if _redirect_output_to_log(log_path):
            print(f"[wiki-agent] {_ts()} pid={os.getpid()} logging to {log_path}")
            print(f"[wiki-agent] stop with: kill {os.getpid()}")

    _install_signal_handlers()

    is_enhance = args.agent == "enhance"
    if is_enhance and args.forever:
        iter_source = itertools.count()
        total_label = "inf"
    elif is_enhance:
        iter_source = range(args.iterations)
        total_label = str(args.iterations)
    else:
        iter_source = range(1)
        total_label = "1"

    last_rc = 0
    successes = 0
    failures = 0
    consecutive_failures = 0
    strategy_counts: dict[str, int] = {}
    iter_count = 0

    # No-op watchdog for long enhance loops: if wiki/log.md does not change for
    # NO_PROGRESS_LIMIT consecutive successful iterations, the loop is doing no
    # logged work (e.g. a CLI exiting 0 without enhancing) and would burn budget
    # indefinitely, since the consecutive-FAILURE guard never trips on rc==0.
    _log_md = ROOT / "wiki" / "log.md"

    def _log_sig():
        try:
            st = _log_md.stat()
            return (st.st_size, st.st_mtime)
        except OSError:
            return None

    _prev_log_sig = _log_sig()
    no_progress = 0
    NO_PROGRESS_LIMIT = (
        10 if (is_enhance and (args.forever or args.iterations > 1)) else 0
    )

    for i in iter_source:
        if _STOP_REQUESTED:
            break
        iter_count = i + 1
        per_iter_strategy = _resolve_strategy(args.strategy, i)
        if is_enhance and (args.forever or args.iterations > 1):
            label = per_iter_strategy or "default"
            print(
                f"\n=== [{_ts()}] Iteration {iter_count}/{total_label} "
                f"— strategy: {label} ===\n"
            )
            strategy_counts[label] = strategy_counts.get(label, 0) + 1

        last_rc = run_agent(args, strategy=per_iter_strategy)

        if last_rc == 0:
            successes += 1
            consecutive_failures = 0
            if NO_PROGRESS_LIMIT:
                _cur_sig = _log_sig()
                if _cur_sig is not None and _cur_sig == _prev_log_sig:
                    no_progress += 1
                    if no_progress >= NO_PROGRESS_LIMIT:
                        print(
                            f"[wiki-agent] {_ts()} {NO_PROGRESS_LIMIT} consecutive "
                            "iterations made no change to wiki/log.md (no-op loop); stopping."
                        )
                        break
                else:
                    no_progress = 0
                    _prev_log_sig = _cur_sig
        else:
            failures += 1
            consecutive_failures += 1
            print(
                f"[wiki-agent] {_ts()} iteration {iter_count} failed with rc={last_rc}"
            )
            if not args.continue_on_error:
                print(
                    "[wiki-agent] stopping loop (use --continue-on-error to keep going)."
                )
                break
            if args.max_failures and consecutive_failures >= args.max_failures:
                print(
                    f"[wiki-agent] {consecutive_failures} consecutive failures "
                    f">= --max-failures={args.max_failures}; aborting."
                )
                break

    if is_enhance and iter_count > 1:
        print(
            f"\n[wiki-agent] {_ts()} loop ended — "
            f"iterations={iter_count} successes={successes} failures={failures}"
        )
        if strategy_counts:
            breakdown = ", ".join(
                f"{k}={v}" for k, v in sorted(strategy_counts.items())
            )
            print(f"[wiki-agent] strategy breakdown: {breakdown}")

    return last_rc


if __name__ == "__main__":
    sys.exit(main())
