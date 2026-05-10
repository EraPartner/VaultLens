#!/usr/bin/env python3
"""Wiki agent wrapper - invoke AI agents with configurable models and effort."""

import argparse
import re
import shutil
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

EFFORT_MAP = {
    "low": {"opencode": ["--variant", "minimal"], "claude": [], "ollama": [], "copilot": ["--effort", "low"]},
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
  # Run quality review with opencode (default)
  python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md

  # Run with Claude Sonnet
  python3 tools/agents/wiki-agent.py quality --page wiki/concepts/my-concept.md --cli claude --model sonnet

  # Run deep analysis with high effort
  python3 tools/agents/wiki-agent.py verify --source wiki/sources/my-source.md --effort high

  # Run ingest with Ollama
  python3 tools/agents/wiki-agent.py ingest --source raw/sources/paper.pdf --cli ollama --model llama3
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
        help="Enhance mode: let the agent pick the sparsest target from wiki.py coverage",
    )
    parser.add_argument(
        "--pdf",
        help="Original PDF to attach when enhancing (defaults to --source if it ends with .pdf)",
    )
    parser.add_argument(
        "--cli",
        choices=list(CLI_OPTIONS.keys()),
        default="opencode",
        help="CLI to use (default: opencode)",
    )
    parser.add_argument(
        "--model",
        help="Model to use (defaults to CLI's best option)",
    )
    parser.add_argument(
        "--effort",
        choices=["low", "medium", "high"],
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


def build_prompt(agent: str, page: str, source: str, custom: str, cli: str = "") -> str:
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

    return prompts.get(agent, "Analyze and report.")


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
        cmd = ["copilot", "-p", full_prompt]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(effort_flags)
        cmd.extend([
            "--allow-all-paths",
            "--allow-tool=read",
            "--allow-tool=shell(ls:*)",
            "--allow-tool=shell(find:*)",
            "--allow-tool=shell(grep:*)",
            "--allow-tool=shell(cat:*)",
            "--allow-tool=shell(head:*)",
            "--allow-tool=shell(tail:*)",
            "--allow-tool=shell(wc:*)",
            "--allow-tool=shell(python3:*)",
            "--allow-tool=shell(qmd:*)",
            "--allow-tool=qmd",
        ])

    elif cli in ("claude", "ollama"):
        system_text, system_file = _prepare_system_prompt(agent_file, system_addon)
        if cli == "claude":
            # claude -p --model MODEL --system-prompt "text" "task"
            cmd = ["claude", "-p"]
            if model:
                cmd.extend(["--model", model])
            cmd.extend(effort_flags)
            cmd.extend(["--system-prompt", system_text])
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


def run_agent(args) -> int:
    """Run the specified agent."""
    # Build prompt
    page = args.page or ""
    source = args.source or ""
    prompt = build_prompt(args.agent, page, source, args.prompt or "", args.cli)

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

    if args.agent == "enhance" and not (
        args.page or args.topic or args.source or args.pdf or args.coverage
    ):
        print(
            "Error: enhance requires one of --page, --topic, --source, --pdf, or --coverage"
        )
        parser.print_help()
        return 1

    return run_agent(args)


if __name__ == "__main__":
    sys.exit(main())
