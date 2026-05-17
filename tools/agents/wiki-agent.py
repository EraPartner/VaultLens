#!/usr/bin/env python3
"""Wiki agent wrapper - invoke AI agents with configurable models and effort."""

import argparse
import datetime as _dt
import itertools
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = ROOT / "tools" / "agents"
TOOLS_DIR = ROOT / "tools"


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
        from wiki import extract_pdf_to_markdown  # type: ignore[import]
    except ImportError as exc:
        print(f"Warning: could not import wiki.extract_pdf_to_markdown: {exc}")
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
    "quality": "wiki-quality-reviewer.agent.md",
    "verify": "wiki-source-verifier.agent.md",
    "ingest": "wiki-ingest.agent.md",
    "contradict": "wiki-contradiction-detector.agent.md",
    "search": "wiki-search.agent.md",
    "enhance": "wiki-enhancer.agent.md",
}

# Symlink stems in .opencode/agents/ are just the agent file stripped of `.agent.md`.
OPENCODE_AGENT_NAMES = {
    key: filename.removesuffix(".agent.md") for key, filename in AGENT_FILES.items()
}

CLI_OPTIONS = {
    "opencode": "opencode",
    "claude": "claude",
    "ollama": "ollama",
    "copilot": "copilot",
}

# Per-agent runtime permissions. Used by the copilot and claude branches to
# pass a minimal set of --allow-tool / --allowedTools / --add-dir flags.
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
    "enhance": {"shell": True, "write": True, "writable_dirs": ["wiki"]},
}

# Shell commands granted to any agent with shell access. Strictly read-only;
# helper utilities for navigation, text inspection, search, and wiki tooling.
READ_ONLY_SHELL_COMMANDS = (
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
# (mkdir/touch) and text editors used in scripted edits (sed/awk in-place).
WRITE_SHELL_COMMANDS = ("touch", "mkdir", "mv", "cp", "sed", "awk")


def _agent_permissions(agent: str) -> dict:
    """Return the permission profile for an agent, defaulting to read-only."""
    return AGENT_PERMISSIONS.get(
        agent, {"shell": False, "write": False, "writable_dirs": []}
    )


STRATEGY_HINTS = {
    "coverage": (
        "Selection strategy: use **Strategy C — Sparse coverage** from "
        "wiki-enhancer.agent.md. Run `python3 tools/wiki.py coverage --json` "
        "and pick the topic with the lowest coverage score where a dense "
        "source exists."
    ),
    "random": (
        "Selection strategy: use **Strategy B — Random page** from "
        "wiki-enhancer.agent.md. Pick a random concept page; first glance "
        "at `tail -20 wiki/log.md` to avoid repeating recent work."
    ),
    "stub": (
        "Selection strategy: use **Strategy A — Shallowest stub** from "
        "wiki-enhancer.agent.md. Pick the concept page with the fewest lines."
    ),
    "source-gap": (
        "Selection strategy: use **Strategy D — Source-driven gap discovery** "
        "from wiki-enhancer.agent.md. Pick a source from wiki/sources/ "
        "(prefer least-recently-enhanced per `tail -40 wiki/log.md`), read "
        "its pre-extracted text at raw/sources-text/<stem>.md, build a gap "
        "list of topics covered well in the source but missing or shallow "
        "in wiki/concepts/, and create new pages or deeply expand the "
        "existing shallow ones — 2-5 gaps per run."
    ),
    "auto": (
        "Selection strategy: use **Strategy E — Mixed / agent-chosen** from "
        "wiki-enhancer.agent.md. Read `tail -30 wiki/log.md`, glance at the "
        "concept page size distribution, and pick whichever of Strategies "
        "A-D would most benefit the wiki right now. Avoid the strategy "
        "used in the most recent log entry."
    ),
}

ALTERNATE_CYCLE = ["coverage", "source-gap", "random", "stub"]


def _resolve_strategy(strategy: str | None, iteration_index: int) -> str | None:
    """Resolve the per-iteration strategy.

    'alternate' cycles through ALTERNATE_CYCLE (coverage -> source-gap ->
    random -> stub) across iterations.
    """
    if strategy == "alternate":
        return ALTERNATE_CYCLE[iteration_index % len(ALTERNATE_CYCLE)]
    return strategy


EFFORT_MAP = {
    "low": {
        "opencode": ["--variant", "minimal"],
        "claude": [],
        "ollama": [],
        "copilot": ["--effort", "low"],
    },
    "medium": {"opencode": [], "claude": [], "ollama": [], "copilot": []},
    "high": {
        "opencode": ["--variant", "xhigh"],
        "claude": [],
        "ollama": [],
        "copilot": ["--effort", "high"],
    },
    "max": {
        "opencode": ["--variant", "xhigh"],
        "claude": [],
        "ollama": [],
        "copilot": ["--effort", "xhigh"],
    },
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wiki agent wrapper - invoke AI agents with configurable CLI/model/effort",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run quality review with opencode
  python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md

  # Run with Claude Sonnet
  python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md --cli claude --model sonnet

  # Run deep analysis with high effort
  python3 tools/agents/wiki-agent.py verify --source wiki/sources/my-source.md --effort high

  # Run ingest with Ollama
  python3 tools/agents/wiki-agent.py ingest --source raw/sources/paper.pdf --cli ollama --model llama3

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
        default="medium",
        help="Thinking effort (default: medium)",
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

    return parser


def get_default_model(cli: str) -> str:
    """Get default model for CLI."""
    defaults = {
        "opencode": "github-copilot/gpt-5.3-codex",
        "claude": "sonnet",
        "ollama": "qwen3.5:4b",
        "copilot": "gpt-5.3-codex",
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
    cli: str = "",
    strategy: str | None = None,
) -> str:
    """Build the task prompt - simple description, full instructions come from .agent.md via system prompt."""
    if custom:
        return custom

    # opencode attaches files via -f, so reference "the attached file" instead
    # of embedding the path (opencode interprets path-like strings as filenames).
    _attached_note = (
        "The source file is already attached to this conversation via -f. "
        "Do NOT attempt to read it from disk or glob for it in raw/sources/. "
        "Only read/write files inside the wiki/ directory."
    )
    if cli == "opencode":
        prompts = {
            "quality": "Analyze the attached wiki page",
            "verify": f"Verify claims in the attached wiki source page. {_attached_note}",
            "ingest": (
                "Process the attached source material into the wiki. The attached file "
                "is the pre-extracted markdown of the source (raw/sources-text/*.md); "
                "treat it as the canonical text. Never try to Read raw .pdf files. "
                f"{_attached_note}"
            ),
            "contradict": "Find potential contradictions across wiki pages",
            "search": f"Search the wiki for: {source if source else page}",
            "enhance": (
                "Enhance the wiki for the attached target. Any attached "
                "raw/sources-text/*.md file is the pre-extracted ground-truth source — "
                "re-read it carefully, fix correctness issues, fill sparse coverage, and "
                "strengthen cross-topic interlinking. Never try to Read raw .pdf files. "
                "Follow wiki-enhancer.agent.md."
            ),
        }
    else:
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
                f"strengthen cross-topic interlinking. Follow wiki-enhancer.agent.md."
            ),
        }

    base = prompts.get(agent, "Analyze and report.")
    if agent == "enhance" and strategy and strategy in STRATEGY_HINTS:
        base = f"{base}\n\n{STRATEGY_HINTS[strategy]}"
    return base


def _prepare_system_prompt(agent_file: Path, system_addon: str) -> tuple[str, Path]:
    """Build system prompt text and return (text, file_path).

    If there is no addon, returns the original agent file path.
    Otherwise writes a temp file combining agent instructions + addon.
    """
    agent_instructions = agent_file.read_text(encoding="utf-8")
    if not system_addon:
        return agent_instructions, agent_file

    combined = f"{agent_instructions}\n\nAdditional context:\n{system_addon}"
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".agent.md",
        delete=False,
        encoding="utf-8",
    )
    tmp.write(combined)
    tmp.close()
    return combined, Path(tmp.name)


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

    effort_flags = EFFORT_MAP.get(effort, {}).get(cli, [])
    system_file = agent_file  # may be replaced by temp file for claude/ollama

    # Build command based on CLI
    if cli == "opencode":
        # opencode run --pure --agent NAME -m MODEL [-f file] "prompt"
        # --pure disables external plugins.
        # Agents registered via symlinks in agent/ → tools/agents/*.agent.md
        agent_name = OPENCODE_AGENT_NAMES[agent]
        cmd = ["opencode", "run", "--pure", "--agent", agent_name]
        if model:
            cmd.extend(["-m", model])
        cmd.extend(effort_flags)
        for arg in extra_args:
            cmd.extend(["-f", arg])
        # Use -- to stop option parsing so the message isn't treated as a file
        cmd.append("--")
        if system_addon:
            cmd.append(f"{prompt}\n\nAdditional context:\n{system_addon}")
        else:
            cmd.append(prompt)

    elif cli == "copilot":
        system_text, system_file = _prepare_system_prompt(agent_file, system_addon)
        full_prompt = f"{system_text}\n\n---\n\nTask: {prompt}"
        if extra_args:
            paths = "\n".join(f"- {p}" for p in extra_args)
            full_prompt += f"\n\nFiles to read:\n{paths}"
        perms = _agent_permissions(agent)
        # -C ROOT pins copilot's cwd to the repo root. Copilot's default path
        # policy restricts file access to cwd and its subdirectories, so we get
        # a repo-scoped sandbox without --allow-all-paths.
        cmd = ["copilot", "-p", full_prompt, "-C", str(ROOT)]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(effort_flags)
        # Reading source/wiki pages is always required.
        cmd.append("--allow-tool=read")
        # Shell allowlist — curated per profile, no blanket shell access.
        if perms["shell"]:
            for c in READ_ONLY_SHELL_COMMANDS:
                cmd.append(f"--allow-tool=shell({c}:*)")
            if perms["write"]:
                for c in WRITE_SHELL_COMMANDS:
                    cmd.append(f"--allow-tool=shell({c}:*)")
        # The write tool covers create/edit/delete via the native file tool
        # (separate from shell redirection, which --allow-all-tools would gate).
        if perms["write"]:
            cmd.append("--allow-tool=write")
        # qmd MCP server is used by search/enhance agents for vector lookup.
        cmd.append("--allow-tool=qmd")
        # Belt-and-braces: explicitly add the writable subdirs. Redundant with
        # -C ROOT for in-repo paths, but documents intent.
        for d in perms.get("writable_dirs", []):
            cmd.extend(["--add-dir", str(ROOT / d)])

    elif cli in ("claude", "ollama"):
        system_text, system_file = _prepare_system_prompt(agent_file, system_addon)
        if cli == "claude":
            # claude -p --model MODEL --system-prompt "text" "task"
            perms = _agent_permissions(agent)
            cmd = ["claude", "-p"]
            if model:
                cmd.extend(["--model", model])
            cmd.extend(effort_flags)
            cmd.extend(["--system-prompt", system_text])
            # Build the --allowedTools list to match the agent's profile.
            allowed_tools = ["Read"]
            if perms["shell"]:
                allowed_tools.extend(f"Bash({c} *)" for c in READ_ONLY_SHELL_COMMANDS)
                if perms["write"]:
                    allowed_tools.extend(f"Bash({c} *)" for c in WRITE_SHELL_COMMANDS)
            if perms["write"]:
                allowed_tools.extend(["Edit", "Write", "NotebookEdit"])
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
            # Scope writes to the agent's writable_dirs. Claude reads from the
            # whole repo by default; --add-dir is needed when the writable
            # target lives outside the launching directory.
            for d in perms.get("writable_dirs", []):
                cmd.extend(["--add-dir", str(ROOT / d)])
            # Permission mode: acceptEdits allows non-interactive edits within
            # the allowed tool set; default still prompts for anything else.
            cmd.extend(
                ["--permission-mode", "acceptEdits" if perms["write"] else "default"]
            )
            cmd.extend(extra_args)
            cmd.append(prompt)
        else:
            # ollama run MODEL --system "text" "prompt"
            cmd = ["ollama", "run"]
            if model:
                cmd.append(model)
            else:
                cmd.append(get_default_model("ollama"))
            cmd.extend(["--system", system_text])
            cmd.extend(effort_flags)
            cmd.append(prompt)

    else:
        cmd = [cli, prompt]

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

    result = subprocess.run(cmd)

    # Clean up temp file if one was created for claude/ollama/copilot
    if cli in ("claude", "ollama", "copilot") and system_file != agent_file:
        system_file.unlink(missing_ok=True)

    return result.returncode


def _slug_to_title(stem: str) -> str:
    """Convert filename stem to a human-readable title."""
    cleaned = re.sub(r"[-_]+", " ", stem).strip()
    return cleaned.title()


def _auto_log_ingest(source_path: str) -> None:
    """Append an ingest log entry after a successful agent run."""
    tools_dir = str(ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    try:
        from wiki import append_log_entry  # type: ignore[import]
    except ImportError as exc:
        print(f"Warning: could not import wiki.py for auto-log: {exc}")
        return

    name = Path(source_path).stem
    title = _slug_to_title(name)
    append_log_entry(
        operation="ingest",
        title=title,
        summary=f"Ingested {source_path} via wiki-agent.py",
        pages=[],
        sources=[source_path],
        notes="Auto-logged by wiki-agent.py",
    )


def run_agent(args, strategy: str | None = None) -> int:
    """Run the specified agent."""
    # Build prompt
    page = args.page or ""
    source = args.source or ""
    prompt = build_prompt(
        args.agent, page, source, args.prompt or "", args.cli, strategy=strategy
    )

    # Get model
    model = args.model or get_default_model(args.cli)
    effort = args.effort

    # Auto-upgrade effort for codex models (deep reasoning by default)
    if "codex" in model and effort == "medium":
        effort = "max"

    # Build extra args based on agent — resolve to absolute paths for -f flags
    extra_args = []
    if args.agent == "quality" and args.page:
        extra_args = [str((ROOT / args.page).resolve())]
    elif args.agent == "verify" and args.source:
        extra_args = [str((ROOT / args.source).resolve())]
    elif args.agent == "ingest" and args.source:
        # PDFs get pre-extracted to raw/sources-text/*.md so the model can read them.
        extra_args = [_resolve_pdf_to_markdown(args.source)]
    elif args.agent == "contradict" and args.page:
        extra_args = ["--domain", args.page]
    elif args.agent == "search":
        extra_args = [args.source or args.page or ""]
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

    rc = invoke_agent(
        args.agent,
        args.cli,
        model,
        effort,
        prompt,
        args.system or "",
        extra_args,
        debug=args.debug,
    )

    if args.agent == "ingest" and rc == 0 and args.source:
        _auto_log_ingest(args.source)

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


def _redirect_output_to_log(log_path: Path) -> None:
    """Send stdout, stderr, and inherited subprocess output to log_path (append)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "a", buffering=1, encoding="utf-8")
    os.dup2(fh.fileno(), sys.stdout.fileno())
    os.dup2(fh.fileno(), sys.stderr.fileno())


def _ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Validate required args
    if args.agent in ["quality", "verify", "ingest"]:
        required = "page" if args.agent == "quality" else "source"
        if not getattr(args, required):
            print(f"Error: --{required} required for {args.agent}")
            parser.print_help()
            return 1

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
        _redirect_output_to_log(log_path)
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
