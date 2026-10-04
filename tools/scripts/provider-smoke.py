#!/usr/bin/env python3
"""Verify native runtime preflight and scoped search; model calls are opt-in.

The default check reads scope metadata and synthetic boundary canaries only.
An authenticated check asks the selected native CLI for a literal response and
does not print provider output or import any host credentials.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from local_access import resolve_scope  # noqa: E402
from local_runtime import (  # noqa: E402
    _verify_preflight,
    native_executable,
    prepared_run,
    runtime_command,
)
from process_control import (  # noqa: E402
    ProcessCleanupError,
    launch_supervised,
    terminate_group,
)
from provider_commands import ProviderCommandRequest, build_provider_command  # noqa: E402

MARKER = "VAULTLENS_NATIVE_PROVIDER_SMOKE"


class _SmokeInterrupted(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def command(
    provider: str,
    root: Path,
    mcp_config: Path,
    *,
    os_isolation_delegated: bool = False,
    execution_cwd: Path | None = None,
) -> list[str]:
    executable = native_executable(provider)
    return build_provider_command(
        provider,
        ProviderCommandRequest(
            "",
            None,
            "This is an authenticated connection check. Do not read notes, call tools or edit files.",
            "Return exactly " + MARKER + " and nothing else.",
            execution_cwd or root,
            False,
            False,
            mcp_config=mcp_config,
            os_isolation_delegated=os_isolation_delegated,
        ),
        executable=str(executable),
    )


def _invoke(argv: list[str], root: Path, env: dict[str, str], timeout: int):
    process = launch_supervised(
        argv,
        run=Path(env["VAULTLENS_RUNTIME_MANIFEST"]).parent,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    previous = {}

    def stop(signum, _frame):
        raise _SmokeInterrupted(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        try:
            for signum in previous:
                signal.signal(signum, signal.SIG_IGN)
            terminate_group(process)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def run(
    root: Path,
    provider: str,
    execute: bool,
    timeout: int,
    *,
    profile="wiki-read",
    project=None,
    read_paths=(),
) -> dict:
    if not 1 <= timeout <= 600:
        raise ValueError("Timeout must be between 1 and 600 seconds")
    root = root.resolve()
    scope = resolve_scope(root, profile, project=project, read_paths=tuple(read_paths))
    result = {
        "filesystem_boundaries": "unverified",
        "provider": provider,
        "authenticated_provider": "skipped (pass --run-provider)",
        "scoped_search": "unverified",
        "provider_file_tools": "unverified (connection check does not exercise file tools)",
    }
    with prepared_run(scope, provider if execute else None, snapshot=False) as (
        runtime,
        directory,
        env,
    ):
        _verify_preflight(runtime, directory, env, root)
        result["filesystem_boundaries"] = "passed (whole-process synthetic preflight)"

        def wrapped(arguments: list[str]) -> list[str]:
            return runtime_command(runtime, directory / "settings.json", arguments)

        status_command = [
            str(Path(sys.executable).resolve()),
            str(root / "tools/scoped_search.py"),
            "--manifest",
            str(directory / "scope.json"),
            "status",
            "--json",
        ]
        try:
            status = _invoke(
                wrapped(status_command), directory / "workspace", env, min(timeout, 30)
            )
            payload = json.loads(status.stdout)
            valid = (
                status.returncode == 0
                and isinstance(payload, dict)
                and payload.get("mode") == "lexical"
                and payload.get("persistent_index") is False
                and payload.get("embeddings") is False
            )
            result["scoped_search"] = (
                "passed (lexical scope; no global index or embeddings)"
                if valid
                else "failed (status verification)"
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result["scoped_search"] = "failed (status invocation)"
        if execute and not result["scoped_search"].startswith("passed"):
            result["authenticated_provider"] = (
                "skipped (scoped search verification failed)"
            )
        elif execute:
            try:
                response = _invoke(
                    wrapped(
                        command(
                            provider,
                            root,
                            directory / "mcp.json",
                            os_isolation_delegated=True,
                            execution_cwd=directory / "workspace",
                        )
                    ),
                    directory / "workspace",
                    env,
                    timeout,
                )
                valid = response.returncode == 0 and response.stdout.strip() == MARKER
                result["authenticated_provider"] = (
                    "passed (literal response; no file tools)"
                    if valid
                    else f"failed (exit {response.returncode}; response verification)"
                )
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                result["authenticated_provider"] = f"failed ({type(exc).__name__})"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--profile", default="wiki-read")
    parser.add_argument("--project")
    parser.add_argument("--read-path", action="append", default=[])
    parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    parser.add_argument("--run-provider", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    try:
        report = run(
            args.root,
            args.provider,
            args.run_provider,
            args.timeout,
            profile=args.profile,
            project=args.project,
            read_paths=args.read_path,
        )
        print(json.dumps(report, indent=2))
        return int(any(str(value).startswith("failed") for value in report.values()))
    except ProcessCleanupError as exc:
        print(f"Native smoke cleanup is unconfirmed: {exc}", file=sys.stderr)
        return 125
    except _SmokeInterrupted as exc:
        print(f"Native smoke interrupted by signal {exc.signum}", file=sys.stderr)
        return 128 + exc.signum
    except KeyboardInterrupt:
        print("Native smoke interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f"Native smoke check refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
