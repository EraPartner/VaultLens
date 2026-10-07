#!/usr/bin/env python3
"""Portable checks for explicit native provider configuration and extension."""

from __future__ import annotations

import json
import contextlib
import io
import os
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Collection
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any, cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _loader import LoadedModule, load_module  # noqa: E402
from local_access import RunScope  # noqa: E402
from provider_commands import (  # noqa: E402
    ProviderCommandRequest,
    build_provider_command,
    provider_names,
    register_provider_adapter,
)

# Parsed TOML overrides are dynamically typed; tests index them with string keys.
TomlTable = dict[str, Any]  # pyright: ignore[reportExplicitAny] -- see comment above


def request(**changes: object) -> ProviderCommandRequest:
    return replace(
        ProviderCommandRequest(
            model="",
            effort=None,
            role_prompt="ROLE",
            task_prompt="TASK",
            cwd=Path("/selected/vault"),
            shell=True,
            write=False,
        ),
        **changes,
    )


def option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def overrides(command: list[str]) -> TomlTable:
    values: TomlTable = {}
    for index, part in enumerate(command):
        if part == "-c":
            parsed = tomllib.loads(command[index + 1])

            def merge(destination: TomlTable, source: TomlTable) -> None:
                for key, value in source.items():
                    if isinstance(value, dict) and isinstance(
                        destination.get(key), dict
                    ):
                        # isinstance leaves the key type Unknown; TOML tables are string-keyed.
                        merge(destination[key], cast(TomlTable, value))
                    else:
                        destination[key] = value

            merge(values, parsed)
    return values


class ProviderCommandsTests(unittest.TestCase):
    def test_request_is_immutable_and_normalizes_root_sequence(self) -> None:
        roots = [Path("/selected/vault/wiki")]
        command_request = request(writable_roots=roots)
        roots.append(Path("/excluded"))
        self.assertEqual(
            command_request.writable_roots, (Path("/selected/vault/wiki"),)
        )
        with self.assertRaises(FrozenInstanceError):
            # Direct assignment is a strict type error on a frozen dataclass; setattr
            # exercises the same runtime rejection.
            setattr(command_request, "model", "changed")
        with self.assertRaises(ValueError):
            request(cwd=Path("relative"))
        with self.assertRaises(ValueError):
            request(mcp_config=Path("relative.json"))
        self.assertFalse(command_request.os_isolation_delegated)
        for ambiguous in (None, "false", 1):
            with self.subTest(delegation=ambiguous), self.assertRaises(ValueError):
                request(os_isolation_delegated=ambiguous)

    def test_registry_extensions_keep_launcher_provider_independent(self) -> None:
        class FixtureAdapter:
            name: str = "fixture-native"

            # The parameter is named `request` to match the ProviderAdapter protocol.
            def build_command(
                self, request: ProviderCommandRequest, *, executable: str
            ) -> list[str]:
                return [executable, request.model, request.task_prompt]

        adapter = FixtureAdapter()
        register_provider_adapter(adapter)
        self.assertIn(adapter.name, provider_names())
        self.assertEqual(
            build_provider_command(
                adapter.name, request(model="opaque-v1"), executable="/bin/provider"
            ),
            ["/bin/provider", "opaque-v1", "TASK"],
        )
        with self.assertRaises(ValueError):
            register_provider_adapter(adapter)
        with self.assertRaises(ValueError):
            build_provider_command("missing-provider", request())

    def test_claude_disables_ambient_settings_hooks_mcp_and_delegation(self) -> None:
        command = build_provider_command("claude", request(shell=False))
        self.assertEqual(command[:2], ["claude", "-p"])
        self.assertEqual(option(command, "--setting-sources"), "")
        self.assertTrue(json.loads(option(command, "--settings"))["disableAllHooks"])
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(
            json.loads(option(command, "--mcp-config")), {"mcpServers": {}}
        )
        self.assertIn("--disable-slash-commands", command)
        self.assertIn("--no-chrome", command)
        self.assertEqual(option(command, "--disallowedTools"), "Agent,Task")
        self.assertEqual(option(command, "--permission-mode"), "dontAsk")
        self.assertEqual(option(command, "--permission-prompts"), "none")
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(option(command, "--tools"), "Read")
        self.assertNotIn("--model", command)
        self.assertNotIn("--effort", command)
        self.assertEqual(command[-1], "TASK")

    def test_claude_grants_only_requested_edit_roots_and_research_tools(self) -> None:
        writer = build_provider_command(
            "claude",
            request(write=True, writable_roots=(Path("/selected/vault/projects/one"),)),
        )
        self.assertIn("Edit", option(writer, "--tools"))
        self.assertIn("Write", option(writer, "--tools"))
        self.assertNotIn("WebFetch", option(writer, "--tools"))
        self.assertEqual(option(writer, "--add-dir"), "/selected/vault/projects/one")
        self.assertEqual(writer[-2:], ["--", "TASK"])
        researcher = build_provider_command("claude", request(web_search=True))
        self.assertIn("WebSearch", option(researcher, "--tools"))
        self.assertIn("WebFetch", option(researcher, "--allowedTools"))

    def test_codex_disables_ambient_config_rules_hooks_mcp_and_delegation(self) -> None:
        command = build_provider_command("codex", request())
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(option(command, "--sandbox"), "read-only")
        settings = overrides(command)
        self.assertEqual(settings["approval_policy"], "never")
        self.assertFalse(settings["agents"]["enabled"])
        self.assertFalse(settings["features"]["multi_agent"])
        self.assertFalse(settings["features"]["hooks"])
        self.assertFalse(settings["features"]["memories"])
        self.assertFalse(settings["features"]["remote_plugin"])
        self.assertFalse(settings["allow_login_shell"])
        self.assertEqual(settings["web_search"], "disabled")
        self.assertEqual(
            settings["projects"]["/selected/vault"]["trust_level"], "untrusted"
        )
        self.assertEqual(settings["project_doc_max_bytes"], 0)
        self.assertEqual(settings["mcp_servers"], {})
        self.assertEqual(settings["history"]["persistence"], "none")
        self.assertEqual(settings["cli_auth_credentials_store"], "file")
        self.assertNotIn("model_reasoning_effort", settings)
        self.assertNotIn("--model", command)
        self.assertIn("ROLE", command[-1])
        self.assertIn("TASK", command[-1])

    def test_codex_writer_roots_and_network_are_explicit(self) -> None:
        command = build_provider_command(
            "codex",
            request(write=True, writable_roots=(Path("/selected/vault/wiki"),)),
        )
        self.assertEqual(option(command, "--sandbox"), "workspace-write")
        settings = overrides(command)["sandbox_workspace_write"]
        self.assertEqual(settings["writable_roots"], ["/selected/vault/wiki"])
        self.assertFalse(settings["network_access"])
        self.assertTrue(settings["exclude_slash_tmp"])
        self.assertTrue(settings["exclude_tmpdir_env_var"])
        research = overrides(
            build_provider_command(
                "codex", request(write=True, network_access=True, web_search=True)
            )
        )
        self.assertTrue(research["sandbox_workspace_write"]["network_access"])
        self.assertEqual(research["web_search"], "live")

    def test_codex_delegation_changes_isolation_and_private_interactive_trust(
        self,
    ) -> None:
        for interactive in (False, True):
            for write in (False, True):
                with self.subTest(interactive=interactive, write=write):
                    ordinary = request(
                        interactive=interactive,
                        write=write,
                        writable_roots=(Path("/selected/vault/projects/one"),)
                        if write
                        else (),
                    )
                    direct = build_provider_command("codex", ordinary)
                    delegated = build_provider_command(
                        "codex", replace(ordinary, os_isolation_delegated=True)
                    )
                    self.assertEqual(
                        option(direct, "--sandbox"),
                        "workspace-write" if write else "read-only",
                    )
                    self.assertEqual(
                        option(delegated, "--sandbox"), "danger-full-access"
                    )
                    self.assertNotIn(
                        "--dangerously-bypass-approvals-and-sandbox", delegated
                    )
                    restored = delegated.copy()
                    trust_key = 'projects."/selected/vault".trust_level'
                    self.assertEqual(
                        overrides(delegated)["projects"]["/selected/vault"][
                            "trust_level"
                        ],
                        "trusted" if interactive else "untrusted",
                    )
                    if interactive:
                        restored[restored.index(trust_key + '="trusted"')] = (
                            trust_key + '="untrusted"'
                        )
                    restored[restored.index("--sandbox") + 1] = option(
                        direct, "--sandbox"
                    )
                    self.assertEqual(restored, direct)
                    policy = overrides(delegated)
                    self.assertEqual(policy["approval_policy"], "never")
                    self.assertFalse(policy["agents"]["enabled"])
                    self.assertFalse(policy["features"]["multi_agent"])
                    self.assertFalse(policy["features"]["hooks"])
                    self.assertFalse(policy["allow_login_shell"])
                    self.assertEqual(policy["web_search"], "disabled")
                    self.assertEqual(policy["mcp_servers"], {})

    def test_claude_keeps_tool_policy_when_outer_isolation_is_delegated(self) -> None:
        for write in (False, True):
            with self.subTest(write=write):
                direct = request(write=write)
                self.assertEqual(
                    build_provider_command("claude", direct),
                    build_provider_command(
                        "claude", replace(direct, os_isolation_delegated=True)
                    ),
                )

    def test_models_and_effort_are_opaque_and_toml_escaped(self) -> None:
        model = "custom/provider:v1"
        effort = 'provider-defined"value\\with space'
        claude = build_provider_command("claude", request(model=model, effort=effort))
        self.assertEqual(option(claude, "--model"), model)
        self.assertEqual(option(claude, "--effort"), effort)
        codex = build_provider_command("codex", request(model=model, effort=effort))
        self.assertEqual(option(codex, "--model"), model)
        self.assertEqual(overrides(codex)["model_reasoning_effort"], effort)

    def test_interactive_commands_keep_closed_defaults_in_disposable_state(
        self,
    ) -> None:
        claude = build_provider_command("claude", request(interactive=True))
        self.assertNotIn("-p", claude)
        self.assertNotIn("--no-session-persistence", claude)
        self.assertNotIn("--permission-prompts", claude)
        self.assertEqual(option(claude, "--permission-mode"), "dontAsk")
        self.assertEqual(option(claude, "--setting-sources"), "")
        codex = build_provider_command("codex", request(interactive=True))
        self.assertEqual(codex[:2], ["codex", "--no-daemon"])
        self.assertNotIn("exec", codex)
        self.assertEqual(overrides(codex)["web_search"], "disabled")
        self.assertEqual(
            overrides(codex)["projects"]["/selected/vault"]["trust_level"], "untrusted"
        )

    def test_interactive_sessions_add_the_boundary_without_a_synthetic_turn(
        self,
    ) -> None:
        claude = build_provider_command("claude", request(interactive=True))
        # Appending keeps Claude Code's own interactive prompt.
        self.assertEqual(option(claude, "--append-system-prompt"), "ROLE")
        self.assertNotIn("--system-prompt", claude)
        self.assertEqual(claude[-1], "TASK")
        headless = build_provider_command("claude", request())
        self.assertEqual(option(headless, "--system-prompt"), "ROLE")
        codex = build_provider_command("codex", request(interactive=True))
        self.assertEqual(overrides(codex)["developer_instructions"], "ROLE")
        self.assertEqual(codex[-2:], ["--", "TASK"])
        # With no task the TUI opens idle instead of submitting the instruction.
        idle = build_provider_command("codex", request(interactive=True, task_prompt=""))
        self.assertNotIn("--", idle)
        self.assertEqual(overrides(idle)["developer_instructions"], "ROLE")

    def test_explicit_stdio_mcp_is_translated_without_ambient_servers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "scoped-mcp.json"
            server = {
                "type": "stdio",
                "command": "/usr/bin/python3",
                "args": [
                    "/approved/scoped_server.py",
                    "--scope",
                    "/runtime/scope.json",
                ],
                "cwd": "/selected/vault",
            }
            config_path.write_text(
                json.dumps({"mcpServers": {"qmd": server}}), encoding="utf-8"
            )
            claude = build_provider_command("claude", request(mcp_config=config_path))
            self.assertEqual(option(claude, "--mcp-config"), str(config_path))
            codex = build_provider_command("codex", request(mcp_config=config_path))
            settings = overrides(codex)
            self.assertEqual(set(settings["mcp_servers"]), {"qmd"})
            actual = settings["mcp_servers"]["qmd"]
            self.assertEqual(actual["command"], server["command"])
            self.assertEqual(actual["args"], server["args"])
            self.assertEqual(actual["cwd"], server["cwd"])
            self.assertTrue(actual["required"])
            self.assertNotIn("type", actual)

    def test_malformed_and_remote_mcp_configuration_fails_closed(self) -> None:
        fixtures: list[object] = [
            {"mcpServers": {"remote": {"type": "http", "url": "https://example.com"}}},
            {"mcpServers": {"local": {"command": "python3", "args": []}}},
            {"mcpServers": {"local": {"command": "/bin/python3", "args": [1]}}},
            {
                "mcpServers": {
                    "local": {"command": "/bin/python3", "env": {"TOKEN": "secret"}}
                }
            },
            {"mcpServers": {"bad.name": {"command": "/bin/python3"}}},
            {"mcpServers": [], "extra": True},
            {},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            for fixture in fixtures:
                path.write_text(json.dumps(fixture), encoding="utf-8")
                for cli in ("claude", "codex"):
                    with (
                        self.subTest(cli=cli, fixture=fixture),
                        self.assertRaises(ValueError),
                    ):
                        build_provider_command(cli, request(mcp_config=path))
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_provider_command("codex", request(mcp_config=path))
            path.unlink()
            with self.assertRaises(ValueError):
                build_provider_command("claude", request(mcp_config=path))


class HeadlessDelegationTests(unittest.TestCase):
    """Command construction uses real manifest parsing and simulated OS denials."""

    root: Path
    note: Path
    run_dir: Path
    scope: RunScope
    canaries: set[Path]
    env: dict[str, str]
    agent: LoadedModule

    def setUp(self) -> None:
        fixture = tempfile.TemporaryDirectory(prefix="vaultlens-command-boundary-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name).resolve()
        (self.root / "tools").mkdir()
        self.note = self.root / "wiki/concepts/approved.md"
        self.note.parent.mkdir(parents=True)
        self.note.write_text("Public synthetic note\n")
        self.run_dir = self.root / "synthetic-runtime"
        self.run_dir.mkdir()
        self.scope = RunScope(
            self.root,
            "reader",
            (self.note,),
            (),
            (self.root / "wiki/private",),
            (),
            self.root / "wiki/reports",
        )
        (self.run_dir / "scope.json").write_text(json.dumps(self.scope.manifest()))
        self.canaries = {self.run_dir / "read-canary", self.run_dir / "write-canary"}
        for path in self.canaries:
            path.write_text("Public synthetic boundary canary\n")
        self.env = {
            "VAULTLENS_RUNTIME_MANIFEST": str(self.run_dir / "scope.json"),
            "VAULTLENS_PROVIDER_CLI": "codex",
            "VAULTLENS_PROVIDER_EXECUTABLE": "/public/synthetic/native-codex",
        }
        tools = Path(__file__).resolve().parents[1]
        self.agent = load_module(
            "command_boundary_agent", tools / "agents/wiki-agent.py"
        )
        root = mock.patch.object(self.agent, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)

    def command(self, *, write: bool = False) -> list[str]:
        return self.agent.build_cli_command(
            "codex", "", None, "ROLE", "TASK", {"shell": True, "write": write}
        )

    def denial(
        self, paths: Collection[Path]
    ) -> contextlib.AbstractContextManager[object]:
        original = Path.open

        def confined_open(
            path: Path,
            mode: str = "r",
            buffering: int = -1,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ) -> IO[str]:
            if path in paths:
                raise PermissionError("Public simulated OS confinement")
            return original(path, mode, buffering, encoding, errors, newline)

        return mock.patch.object(Path, "open", confined_open)

    def test_unmanaged_headless_builder_preserves_codex_sandbox_defaults(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            for write in (False, True):
                with self.subTest(write=write):
                    self.assertEqual(
                        option(self.command(write=write), "--sandbox"),
                        "workspace-write" if write else "read-only",
                    )

    def test_missing_unreadable_or_forged_boundary_never_builds_a_provider(self) -> None:
        for marker in (None, self.run_dir / "absent.json", self.run_dir / "scope.json"):
            with (
                self.subTest(marker=marker),
                mock.patch.dict(
                    os.environ,
                    {"VAULTLENS_RUNTIME_MANIFEST": str(marker)} if marker else {},
                    clear=True,
                ),
                mock.patch.object(
                    self.agent,
                    "resolve_role_settings",
                    return_value=(SimpleNamespace(cli="codex", model=""), None),
                ),
                mock.patch.object(
                    self.agent,
                    "launch_headless",
                    side_effect=ValueError("Public missing runtime fixture"),
                ),
                mock.patch.object(self.agent, "build_provider_command") as builder,
                mock.patch.object(self.agent, "run_agent") as agent,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(self.agent.main(["search", "--source", "query"]), 2)
                builder.assert_not_called()
                agent.assert_not_called()

    def test_manifest_without_both_denials_cannot_delegate(self) -> None:
        cases: tuple[tuple[set[Path], str], ...] = (
            (set(), "read confinement is absent"),
            ({self.run_dir / "read-canary"}, "write confinement is absent"),
        )
        for denied, message in cases:
            with (
                self.subTest(denied=denied),
                mock.patch.dict(os.environ, self.env, clear=True),
                self.denial(denied),
                mock.patch.object(self.agent, "build_provider_command") as builder,
            ):
                with self.assertRaisesRegex(ValueError, message):
                    self.command()
                builder.assert_not_called()

    def test_verified_reader_delegates_without_expanding_outer_access_policy(self) -> None:
        from local_runtime import compile_settings

        outer = compile_settings(self.scope, self.run_dir, "codex")
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            self.denial(self.canaries),
        ):
            command = self.command()
        self.assertEqual(option(command, "--sandbox"), "danger-full-access")
        self.assertEqual(overrides(command)["approval_policy"], "never")
        self.assertTrue(self.scope.readable(self.note))
        self.assertFalse(self.scope.writable(self.note))
        self.assertFalse(self.scope.readable(self.root / "wiki/private/hidden.md"))
        self.assertEqual(compile_settings(self.scope, self.run_dir, "codex"), outer)
        self.assertIn(str(self.note), outer["filesystem"]["allowRead"])
        self.assertTrue(
            all(
                Path(grant).is_relative_to(self.run_dir)
                for grant in outer["filesystem"]["allowWrite"]
            )
        )
        self.assertIn(str(self.run_dir / "write-canary"), outer["filesystem"]["denyWrite"])
        self.assertIn(str(self.scope.reports), outer["filesystem"]["denyWrite"])
        self.assertEqual(
            outer["network"]["allowedDomains"],
            ["api.openai.com:443", "chatgpt.com:443", "auth.openai.com:443"],
        )

    def test_verified_boundary_for_another_vault_cannot_delegate(self) -> None:
        from dataclasses import replace

        foreign = replace(self.scope, root=self.root / "another-vault")
        (self.run_dir / "scope.json").write_text(json.dumps(foreign.manifest()))
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            self.denial(self.canaries),
            mock.patch.object(self.agent, "build_provider_command") as builder,
        ):
            with self.assertRaisesRegex(ValueError, "another vault"):
                self.command()
            builder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
