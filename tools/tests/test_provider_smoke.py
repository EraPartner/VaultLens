#!/usr/bin/env python3
"""Native smoke harness checks; no provider or live runtime is invoked."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from _loader import load_module

# A recorded mock call; typeshed only exposes the type as the private `_Call`.
from unittest.mock import _Call as MockCall  # pyright: ignore[reportPrivateUsage] - no public name for the call-record type

smoke = load_module(
    "provider_smoke", Path(__file__).resolve().parents[1] / "scripts/provider-smoke.py"
)

import local_runtime as runtime  # noqa: E402
from local_access import RunScope  # noqa: E402


class ProviderSmokeTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable] - assigned in setUp
    env: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable] - assigned in setUp

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.env = {"VAULTLENS_RUNTIME_MANIFEST": str(self.root / "scope.json")}
        (self.root / "mcp.json").write_text('{"mcpServers":{}}')

    def run_mocked(
        self,
        execute: bool,
        invocation: Callable[
            [list[str], Path, dict[str, str], int], subprocess.CompletedProcess[str]
        ],
        *,
        preflight: BaseException | None = None,
    ) -> tuple[dict[str, str], list[MockCall], MockCall]:
        def prepared_context(
            *_args: object, **_kwargs: object
        ) -> contextlib.AbstractContextManager[tuple[Path, Path, dict[str, str]]]:
            return contextlib.nullcontext(
                (Path("/runtime/srt"), self.root, {"HOME": "/isolated"})
            )

        prepared = mock.Mock(side_effect=prepared_context)
        with (
            mock.patch.object(smoke, "resolve_scope", return_value="approved-scope"),
            mock.patch.object(smoke, "prepared_run", prepared),
            mock.patch.object(smoke, "_verify_preflight", side_effect=preflight),
            mock.patch.object(
                smoke, "native_executable", return_value=Path(sys.executable)
            ),
            mock.patch.object(smoke, "_invoke", side_effect=invocation) as invoke,
        ):
            return (
                smoke.run(self.root, "claude", execute, 10),
                invoke.call_args_list,
                prepared.call_args,
            )

    @staticmethod
    def status(argv: list[str], *_args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {"mode": "lexical", "embeddings": False, "persistent_index": False}
            ),
            "",
        )

    def test_default_skips_provider_and_uses_runtime_scoped_search(self) -> None:
        result, calls, prepared = self.run_mocked(False, self.status)
        self.assertTrue(result["filesystem_boundaries"].startswith("passed"))
        self.assertTrue(result["scoped_search"].startswith("passed"))
        self.assertTrue(result["authenticated_provider"].startswith("skipped"))
        self.assertEqual(prepared.args, ("approved-scope", None))
        self.assertFalse(prepared.kwargs["snapshot"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0][0], "/runtime/srt")
        self.assertIn(str(self.root / "tools/scoped_search.py"), calls[0].args[0])
        self.assertIn(str(self.root / "scope.json"), calls[0].args[0])

    def test_preflight_failure_prevents_all_runtime_and_provider_commands(self) -> None:
        with (
            mock.patch.object(smoke, "resolve_scope", return_value="approved"),
            mock.patch.object(
                smoke,
                "prepared_run",
                return_value=contextlib.nullcontext(
                    (Path("/runtime/srt"), self.root, dict[str, str]())
                ),
            ),
            mock.patch.object(
                smoke,
                "_verify_preflight",
                side_effect=ValueError("confinement rejected"),
            ),
            mock.patch.object(smoke, "_invoke") as invoke,
        ):
            with self.assertRaisesRegex(ValueError, "confinement rejected"):
                smoke.run(self.root, "claude", True, 10)
            invoke.assert_not_called()

    def test_authenticated_response_requires_exact_marker_and_hides_outputs(self) -> None:
        def invoke(argv: list[str], *_args: object) -> subprocess.CompletedProcess[str]:
            if any("scoped_search.py" in argument for argument in argv):
                return self.status(argv)
            return subprocess.CompletedProcess(
                argv, 0, smoke.MARKER + "\n", "private configuration"
            )

        result, calls, prepared = self.run_mocked(True, invoke)
        self.assertTrue(result["authenticated_provider"].startswith("passed"))
        self.assertTrue(result["provider_file_tools"].startswith("unverified"))
        self.assertNotIn("private configuration", json.dumps(result))
        self.assertEqual(prepared.args, ("approved-scope", "claude"))
        self.assertEqual(calls[-1].args[-1], 10)

    def test_successful_cli_without_literal_response_fails(self) -> None:
        def invoke(argv: list[str], *_args: object) -> subprocess.CompletedProcess[str]:
            if any("scoped_search.py" in argument for argument in argv):
                return self.status(argv)
            return subprocess.CompletedProcess(argv, 0, "different response", "")

        result, _calls, _prepared = self.run_mocked(True, invoke)
        self.assertTrue(result["authenticated_provider"].startswith("failed"))

    def test_provider_timeout_is_reported_without_sensitive_details(self) -> None:
        def invoke(argv: list[str], *_args: object) -> subprocess.CompletedProcess[str]:
            if any("scoped_search.py" in argument for argument in argv):
                return self.status(argv)
            raise subprocess.TimeoutExpired(argv, 10, output="private output")

        result, _calls, _prepared = self.run_mocked(True, invoke)
        self.assertEqual(result["authenticated_provider"], "failed (TimeoutExpired)")
        self.assertNotIn("private output", json.dumps(result))

    def test_provider_commands_share_closed_native_defaults(self) -> None:
        with mock.patch.object(
            smoke, "native_executable", return_value=Path(sys.executable)
        ):
            for provider in ("claude", "codex"):
                command = smoke.command(provider, self.root, self.root / "mcp.json")
                self.assertNotIn("--dangerously-skip-permissions", command)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
                self.assertIn(smoke.MARKER, command[-1])
            claude = smoke.command("claude", self.root, self.root / "mcp.json")
            self.assertEqual(claude[claude.index("--permission-mode") + 1], "dontAsk")
            codex = smoke.command("codex", self.root, self.root / "mcp.json")
            self.assertIn("--ignore-user-config", codex)
            self.assertIn("read-only", codex)

    def test_smoke_uses_runtime_discovery_and_exact_selected_provider_path(self) -> None:
        selected = Path("/public-home/.local/share/claude/versions/reviewed-cli")
        with mock.patch.object(
            smoke, "native_executable", return_value=selected
        ) as discovery:
            command = smoke.command("claude", self.root, self.root / "mcp.json")
        self.assertEqual(command[0], str(selected))
        discovery.assert_called_once_with("claude")
        with mock.patch.object(
            smoke,
            "native_executable",
            side_effect=ValueError("retired launcher fixture"),
        ):
            with self.assertRaisesRegex(ValueError, "retired launcher fixture"):
                smoke.command("claude", self.root, self.root / "mcp.json")

    def test_delegated_codex_uses_outer_boundary_and_keeps_tool_controls(self) -> None:
        with mock.patch.object(
            smoke, "native_executable", return_value=Path(sys.executable)
        ):
            direct = smoke.command("codex", self.root, self.root / "mcp.json")
            delegated = smoke.command(
                "codex",
                self.root,
                self.root / "mcp.json",
                os_isolation_delegated=True,
            )
        self.assertIn("read-only", direct)
        self.assertIn("danger-full-access", delegated)
        self.assertNotIn("read-only", delegated)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", delegated)
        self.assertEqual(
            [part for part in delegated if part != "danger-full-access"],
            [part for part in direct if part != "read-only"],
        )

    def test_status_failure_does_not_count_as_pass(self) -> None:
        def malformed_status(
            argv: list[str], *_args: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, "private malformed output", "")

        result, _calls, _prepared = self.run_mocked(False, malformed_status)
        self.assertTrue(result["scoped_search"].startswith("failed"))
        self.assertNotIn("private malformed output", json.dumps(result))

    def test_every_invocation_exit_cleans_the_group_and_restores_handlers(self) -> None:
        outcomes = (
            ("public output", "public diagnostic"),
            subprocess.TimeoutExpired("public fixture", 10),
            KeyboardInterrupt(),
        )
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                process = mock.Mock(pid=1234, returncode=0)
                if isinstance(outcome, BaseException):
                    process.communicate.side_effect = outcome
                else:
                    process.communicate.return_value = outcome
                previous = {
                    smoke.signal.SIGTERM: object(),
                    smoke.signal.SIGINT: object(),
                }
                handlers: dict[int, object] = {}

                def install(signum: int, handler: object) -> object:
                    handlers[signum] = handler
                    return previous[signum]

                with (
                    mock.patch.object(
                        smoke, "launch_supervised", return_value=process
                    ) as execute,
                    mock.patch.object(smoke.signal, "signal", side_effect=install),
                    mock.patch.object(smoke, "terminate_group") as cleanup,
                ):
                    if isinstance(outcome, BaseException):
                        with self.assertRaises(type(outcome)):
                            smoke._invoke(["/runtime/srt"], self.root, self.env, 10)
                    else:
                        result = smoke._invoke(
                            ["/runtime/srt"], self.root, self.env, 10
                        )
                        self.assertEqual(result.stdout, "public output")
                        self.assertEqual(result.returncode, 0)
                    cleanup.assert_called_once_with(process)
                self.assertEqual(execute.call_args.kwargs["run"], self.root)
                self.assertNotIn("start_new_session", execute.call_args.kwargs)
                self.assertEqual(handlers, previous)
                process.stdout.close.assert_called_once()
                process.stderr.close.assert_called_once()

    def test_signal_interruption_cleans_the_group_and_preserves_exit_signal(self) -> None:
        process = mock.Mock(pid=1234)
        previous = {smoke.signal.SIGTERM: object(), smoke.signal.SIGINT: object()}
        handlers: dict[int, object] = {}

        def install(signum: int, handler: object) -> object:
            handlers[signum] = handler
            return previous[signum]

        def communicate(**_kwargs: object) -> None:
            handler = handlers[smoke.signal.SIGTERM]
            assert callable(handler)
            handler(smoke.signal.SIGTERM, None)

        process.communicate.side_effect = communicate
        with (
            mock.patch.object(smoke, "launch_supervised", return_value=process),
            mock.patch.object(smoke.signal, "signal", side_effect=install),
            mock.patch.object(smoke, "terminate_group") as cleanup,
        ):
            with self.assertRaises(smoke._SmokeInterrupted) as interruption:
                smoke._invoke(["/runtime/srt"], self.root, self.env, 10)
        self.assertEqual(interruption.exception.signum, smoke.signal.SIGTERM)
        cleanup.assert_called_once_with(process)
        self.assertEqual(handlers, previous)

    def test_cleanup_failure_overrides_timeout_and_restores_handlers(self) -> None:
        process = mock.Mock(pid=1234)
        process.communicate.side_effect = subprocess.TimeoutExpired(
            "public fixture", 10
        )
        previous = {smoke.signal.SIGTERM: object(), smoke.signal.SIGINT: object()}
        handlers: dict[int, object] = {}

        def install(signum: int, handler: object) -> object:
            handlers[signum] = handler
            return previous[signum]

        with (
            mock.patch.object(smoke, "launch_supervised", return_value=process),
            mock.patch.object(smoke.signal, "signal", side_effect=install),
            mock.patch.object(
                smoke,
                "terminate_group",
                side_effect=smoke.ProcessCleanupError(
                    "public group still alive", group_id=1234
                ),
            ),
        ):
            with self.assertRaises(smoke.ProcessCleanupError) as failure:
                smoke._invoke(["/runtime/srt"], self.root, self.env, 10)
        self.assertEqual(failure.exception.group_id, 1234)
        self.assertEqual(handlers, previous)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_handler_setup_failure_still_cleans_spawned_group(self) -> None:
        process = mock.Mock(pid=1234)
        with (
            mock.patch.object(smoke, "launch_supervised", return_value=process),
            mock.patch.object(
                smoke.signal,
                "signal",
                side_effect=ValueError("public invalid handler thread"),
            ),
            mock.patch.object(smoke, "terminate_group") as cleanup,
        ):
            with self.assertRaisesRegex(ValueError, "invalid handler thread"):
                smoke._invoke(["/runtime/srt"], self.root, self.env, 10)
        cleanup.assert_called_once_with(process)
        process.communicate.assert_not_called()

    def test_unconfirmed_smoke_cleanup_reaches_runtime_quarantine(self) -> None:
        (self.root / "tools").mkdir()
        (self.root / "wiki").mkdir()
        scope = RunScope(
            self.root,
            "public-scope",
            (self.root / "wiki",),
            (),
            (),
            (),
            self.root / "wiki/reports/agents",
        )
        with (
            mock.patch.object(smoke, "resolve_scope", return_value=scope),
            mock.patch.object(
                runtime, "runtime_executable", return_value=Path("/runtime/srt")
            ),
            mock.patch.object(runtime, "require_verified_runtime"),
            mock.patch.object(smoke, "_verify_preflight"),
            mock.patch.object(
                smoke,
                "_invoke",
                side_effect=smoke.ProcessCleanupError(
                    "public cancellation incomplete", group_id=1234
                ),
            ),
        ):
            with self.assertRaises(smoke.ProcessCleanupError):
                smoke.run(self.root, "claude", False, 10)
            gate = self.root / "tools/runtime-state/cancellation-unconfirmed.json"
            self.assertEqual(json.loads(gate.read_text())["group_id"], 1234)
            with self.assertRaisesRegex(ValueError, "Previous tool cancellation"):
                with runtime.prepared_run(scope, None, snapshot=False):
                    self.fail("Smoke cancellation did not block subsequent workloads")

    def test_provider_cleanup_failure_is_not_reduced_to_a_reported_timeout(self) -> None:
        error = smoke.ProcessCleanupError("public provider tools remain", group_id=1234)

        def invoke(argv: list[str], *_args: object) -> subprocess.CompletedProcess[str]:
            if any("scoped_search.py" in argument for argument in argv):
                return self.status(argv)
            raise error

        with self.assertRaises(smoke.ProcessCleanupError) as failure:
            self.run_mocked(True, invoke)
        self.assertIs(failure.exception, error)

    def test_cli_reports_unconfirmed_cleanup_as_exit_125_without_traceback(self) -> None:
        diagnostic = io.StringIO()
        with (
            mock.patch.object(
                smoke,
                "run",
                side_effect=smoke.ProcessCleanupError(
                    "public unresolved group", group_id=1234
                ),
            ),
            contextlib.redirect_stderr(diagnostic),
        ):
            self.assertEqual(smoke.main(["--root", str(self.root)]), 125)
        self.assertIn("cleanup is unconfirmed", diagnostic.getvalue())
        self.assertNotIn("Traceback", diagnostic.getvalue())

    def test_cli_returns_interruption_status_after_group_cleanup(self) -> None:
        for error, status in (
            (KeyboardInterrupt(), 130),
            (smoke._SmokeInterrupted(smoke.signal.SIGTERM), 143),
        ):
            with self.subTest(error=type(error).__name__):
                with (
                    mock.patch.object(smoke, "run", side_effect=error),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(smoke.main(["--root", str(self.root)]), status)


if __name__ == "__main__":
    unittest.main()
