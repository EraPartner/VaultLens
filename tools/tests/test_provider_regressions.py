"""Offline fixtures for provider capabilities, dry runs, and adapter export."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from collections.abc import Callable

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from agent_capabilities import (  # noqa: E402
    CAPABILITIES,
    claude_builtin_tools,
    claude_tools,
    profile_capabilities,
)
from agent_profiles import AGENT_FILES, load_role  # noqa: E402
from _loader import LoadedModule, load_module  # noqa: E402

IngestProvider = Callable[..., int]


class ProviderRegressionTests(unittest.TestCase):
    root: Path
    roles: Path
    agent: LoadedModule
    generator: LoadedModule

    def setUp(self) -> None:
        fixture = tempfile.TemporaryDirectory(prefix="vaultlens-provider-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.roles = self.root / ".agents" / "roles"
        self.roles.mkdir(parents=True)
        for source in (TOOLS.parent / ".agents" / "roles").glob("*.md"):
            shutil.copy2(source, self.roles / source.name)
        shutil.copy2(TOOLS.parent / ".agents" / "context-policy.md", self.roles.parent)
        (self.root / "tools").mkdir()
        shutil.copy2(TOOLS / "model-profiles.json", self.root / "tools")
        for directory in (self.root, self.root / "wiki", self.root / "projects"):
            directory.mkdir(exist_ok=True)
            (directory / "AGENTS.md").write_text("Fixture instructions\n")
        self.agent = load_module("regression_agent", TOOLS / "agents" / "wiki-agent.py")
        boundary = mock.patch.object(self.agent, "verify_active_boundary")
        boundary.start()
        self.addCleanup(boundary.stop)
        self.generator = load_module("regression_generator", TOOLS / "agents" / "generate-adapters.py")

    def generator_paths(self) -> contextlib.AbstractContextManager[object]:
        return mock.patch.multiple(
            self.generator,
            ROOT=self.root,
            ROLES_DIR=self.roles,
            CLAUDE_DIR=self.root / ".claude" / "agents",
            CODEX_DIR=self.root / ".codex" / "agents",
        )

    def test_every_role_shares_capabilities_and_grants_qmd(self) -> None:
        for name, filename in AGENT_FILES.items():
            with self.subTest(role=name):
                role = load_role(self.roles / filename)
                perms = profile_capabilities(role.permission_profile)
                self.assertEqual(self.agent._agent_permissions(name), perms)
                allowed = claude_tools(perms)
                native = claude_tools(perms, scoped_shell=False)
                self.assertIn("mcp__qmd__*", allowed)
                self.assertIn("mcp__qmd__*", native)
                self.assertEqual("Bash" in native, perms["shell"])
                for tool in ("Edit", "Write"):
                    self.assertEqual(tool in native, perms["write"])
                    self.assertEqual(tool in allowed, perms["write"])
                self.assertEqual("Bash(python3 *)" in allowed, name == "project-run")
                self.assertNotIn("Task", native)
                self.assertNotIn("Agent", native)
                with self.generator_paths():
                    manifest = self.generator.claude_manifest(role)
                self.assertIn(f"tools: {', '.join(native)}\n", manifest)
                self.assertIn("disallowedTools: Agent, Task\n", manifest)

    def test_headless_tool_availability_and_retention_for_every_profile(self) -> None:
        for profile in CAPABILITIES:
            with self.subTest(profile=profile):
                perms = profile_capabilities(profile)
                command = self.agent.build_cli_command(
                    "claude", "", None, "ROLE", "TASK", perms
                )
                available = command[command.index("--tools") + 1].split(",")
                allowed = command[command.index("--allowedTools") + 1].split(",")
                expected = ["Read", "Grep", "Glob"]
                if perms["shell"]:
                    expected.append("Bash")
                if perms["write"]:
                    expected.extend(["Edit", "Write"])
                self.assertEqual(available, expected)
                self.assertEqual(claude_builtin_tools(perms), expected)
                # --tools controls built-ins only. MCP permission patterns stay
                # in --allowedTools rather than being mistaken for tool names.
                self.assertIn("mcp__qmd__*", allowed)
                self.assertNotIn("mcp__qmd__*", available)
                self.assertNotIn("Bash", allowed)
                self.assertTrue(all("(" not in name for name in available))
                self.assertIn("--no-session-persistence", command)
                self.assertIn("-p", command)
                self.assertEqual(command[-1], "TASK")
                self.assertNotIn("--dangerously-skip-permissions", command)
                if perms["shell"]:
                    self.assertIn("Bash(python3 tools/wiki.py lint)", allowed)
                    self.assertEqual(
                        "Bash(python3 *)" in allowed,
                        profile == "project-write",
                    )

    def test_debug_pdf_tasks_do_not_preprocess_or_promote(self) -> None:
        inbox = self.root / "raw" / "inbox"
        inbox.mkdir(parents=True)
        pdf = inbox / "example.pdf"
        original = b"fixture PDF: debug must not read or move it"
        pdf.write_bytes(original)
        for flags in (
            ["ingest", "--source", "raw/inbox/example.pdf"],
            ["enhance", "--source", "raw/inbox/example.pdf"],
            ["enhance", "--pdf", "raw/inbox/example.pdf"],
        ):
            with self.subTest(flags=flags):
                args = self.agent.build_parser().parse_args([*flags, "--debug"])
                args.cli, args.model, args.effort = "claude", "sonnet", "medium"
                with (
                    mock.patch.object(self.agent, "ROOT", self.root),
                    mock.patch.object(self.agent, "AGENTS_DIR", self.roles),
                    mock.patch.object(self.agent, "validate_cli", return_value=True),
                    mock.patch.object(
                        self.agent, "_resolve_pdf_to_markdown"
                    ) as preprocess,
                    mock.patch.object(self.agent.subprocess, "run") as provider,
                    contextlib.redirect_stdout(io.StringIO()) as output,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(self.agent.run_agent(args), 0)
                preprocess.assert_not_called()
                provider.assert_not_called()
                self.assertIn(str(pdf), output.getvalue())
                self.assertEqual(pdf.read_bytes(), original)
                self.assertFalse((self.root / "raw" / "sources").exists())

    def source_page(
        self, pdf_name: str = "example.pdf", *, summary: str = "A source summary."
    ) -> str:
        return (
            "---\n"
            "title: Example\ntype: source\nstatus: active\n"
            "created: 2026-10-03\nupdated: 2026-10-03\n"
            f"summary: {summary}\n"
            "source_id: src-2026-10-03-001\nsource_type: pdf\n"
            "origin: fixture\ningested_on: 2026-10-03\n---\n\n"
            f"# Example\n\nEvidence extracted.\n\n## Sources\n"
            f"- Source PDF: [[raw/inbox/{pdf_name}]]\n"
        )

    def run_ingest_fixture(self, provider: IngestProvider) -> tuple[int, int, str]:
        inbox = self.root / "raw" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "example.pdf").write_bytes(b"fixture PDF")
        args = self.agent.build_parser().parse_args(
            ["ingest", "--source", "raw/inbox/example.pdf"]
        )
        args.cli, args.model, args.effort = "claude", "sonnet", "medium"
        with (
            mock.patch.object(self.agent, "ROOT", self.root),
            mock.patch.object(self.agent, "validate_cli", return_value=True),
            mock.patch.object(
                self.agent, "_resolve_pdf_to_markdown", return_value="prepared.md"
            ) as preprocess,
            mock.patch.object(self.agent, "invoke_agent", side_effect=provider),
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            result = self.agent.run_agent(args)
        preprocess.assert_called_once_with(args.source)
        self.assertEqual((inbox / "example.pdf").read_bytes(), b"fixture PDF")
        return result, 0, error.getvalue()

    def test_ingest_accepts_changed_valid_page_without_mutating_sources(self) -> None:
        sources = self.root / "wiki" / "sources"
        sources.mkdir()
        page = sources / "src-2026-10-03-001.md"
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    page.write_text(self.source_page(summary="Previous summary."))
                else:
                    page.unlink(missing_ok=True)

                def provider(*_args: object, **_kwargs: object) -> int:
                    page.write_text(self.source_page())
                    return 0

                result, promotions, error = self.run_ingest_fixture(provider)
                self.assertEqual((result, promotions, error), (0, 0, ""))

    def test_ingest_rejects_absent_invalid_unrelated_and_unchanged_output(self) -> None:
        sources = self.root / "wiki" / "sources"
        sources.mkdir()
        page = sources / "src-2026-10-03-001.md"
        for name, output in (
            ("absent", None),
            ("blank metadata", self.source_page(summary="")),
            ("wrong PDF", self.source_page(pdf_name="different.pdf")),
            ("draft", self.source_page().replace("status: active", "status: draft")),
            (
                "invalid date",
                self.source_page().replace("created: 2026-10-03", "created: yesterday"),
            ),
            (
                "moved source citation",
                self.source_page().replace("raw/inbox/", "raw/sources/"),
            ),
            ("unchanged", self.source_page()),
        ):
            with self.subTest(case=name):
                page.unlink(missing_ok=True)
                if name == "unchanged":
                    page.write_text(output or "")

                def provider(*_args: object, **_kwargs: object) -> int:
                    if output is not None:
                        page.write_text(output)
                    return 0

                result, promotions, error = self.run_ingest_fixture(provider)
                self.assertEqual((result, promotions), (2, 0))
                self.assertIn("source remains in raw/inbox/", error)

    def test_ingest_provider_failure_never_promotes(self) -> None:
        result, promotions, error = self.run_ingest_fixture(lambda *_a, **_kw: 1)
        self.assertEqual((result, promotions, error), (1, 0, ""))

    def test_ingest_accepts_relative_markdown_pdf_citation(self) -> None:
        sources = self.root / "wiki" / "sources"
        sources.mkdir()
        page = sources / "src-2026-10-03-001.md"

        def provider(*_args: object, **_kwargs: object) -> int:
            page.write_text(
                self.source_page().replace(
                    "[[raw/inbox/example.pdf]]",
                    "[PDF](<../../raw/inbox/example.pdf>)",
                )
            )
            return 0

        self.assertEqual(self.run_ingest_fixture(provider), (0, 0, ""))

    def test_ingest_matches_special_pdf_names_and_large_source_ids(self) -> None:
        from urllib.parse import quote

        sources = self.root / "wiki" / "sources"
        sources.mkdir()
        page = sources / "src-2026-10-03-1000.md"
        for name in ("Example [1] #2.pdf", "Example Project.pdf"):
            with (
                self.subTest(name=name),
                mock.patch.object(self.agent, "ROOT", self.root),
            ):
                content = (
                    self.source_page(pdf_name=name)
                    .replace("src-2026-10-03-001", page.stem)
                    .replace(
                        f"[[raw/inbox/{name}]]",
                        f"[PDF](<../../raw/inbox/{quote(name)}>)",
                    )
                )
                before = self.agent._source_page_snapshot()
                page.write_text(content)
                self.assertTrue(self.agent._verify_ingest_result(self.root / "raw/inbox" / name, before))

    def test_inbox_note_ingest_is_verified_and_prompted_with_its_path(self) -> None:
        sources = self.root / "wiki" / "sources"
        sources.mkdir()
        inbox = self.root / "raw" / "inbox"
        inbox.mkdir(parents=True)
        note = inbox / "article.md"
        note.write_text("note")
        page = sources / "src-2026-10-03-001.md"
        with mock.patch.object(self.agent, "ROOT", self.root):
            self.assertEqual(self.agent._inbox_source("raw/inbox/article.md"), note.resolve())
            prompt = self.agent.build_prompt("ingest", "", str(note.resolve()), "")
            self.assertIn("at exactly this path: \"raw/inbox/article.md\"", prompt)
            for citation, expected in (
                ("[[raw/inbox/article]]", True),
                ("[[raw/inbox/article.md]]", True),
                ("[[raw/sources-text/article]]", False),
            ):
                with self.subTest(citation=citation):
                    page.write_text(
                        self.source_page().replace("[[raw/inbox/example.pdf]]", citation)
                    )
                    self.assertIs(self.agent._verify_ingest_result(note.resolve(), {}), expected)

    def test_ingest_code_examples_do_not_certify_a_pdf_citation(self) -> None:
        sources = self.root / "wiki" / "sources"
        sources.mkdir()
        page = sources / "src-2026-10-03-001.md"
        for citation in (
            "`[PDF](<../../raw/inbox/example.pdf>)`",
            "\n```md\n[PDF](<../../raw/inbox/example.pdf>)\n```",
        ):
            with (
                self.subTest(citation=citation),
                mock.patch.object(self.agent, "ROOT", self.root),
            ):
                page.write_text(
                    self.source_page().replace("[[raw/inbox/example.pdf]]", citation)
                )
                self.assertFalse(
                    self.agent._verify_ingest_result(self.root / "raw/inbox/example.pdf", {})
                )

    def test_ingest_verification_ignores_citation_links_that_cannot_resolve(self) -> None:
        sources = self.root / "wiki" / "sources"
        sources.mkdir()
        page = sources / "src-2026-10-03-001.md"

        def provider(*_args: object, **_kwargs: object) -> int:
            # "%00" decodes to an embedded NUL, which Path.resolve() rejects.
            page.write_text(self.source_page() + "\nSee [broken](%00).\n")
            return 0

        self.assertEqual(self.run_ingest_fixture(provider), (0, 0, ""))

    def test_launch_boundary_failure_returns_status_not_traceback(self) -> None:
        for failure in (
            "Runtime scope belongs to another vault",
            "Native provider executable handoff is missing or mismatched",
        ):
            with (
                self.subTest(failure=failure),
                mock.patch.object(self.agent, "ROOT", self.root),
                mock.patch.object(self.agent, "AGENTS_DIR", self.roles),
                mock.patch.object(
                    self.agent, "build_cli_command", side_effect=ValueError(failure)
                ),
                mock.patch.object(self.agent, "_run_agent_command") as run,
                contextlib.redirect_stderr(io.StringIO()) as error,
            ):
                result = self.agent.invoke_agent(
                    "quality", "claude", "sonnet", "medium", "PROMPT", "", []
                )
            self.assertEqual(result, 2)
            run.assert_not_called()
            self.assertIn(failure, error.getvalue())

    def test_usage_errors_are_reported_before_the_runtime_is_entered(self) -> None:
        for argv in (
            ["quality"],
            ["verify"],
            ["search"],
            ["connect", "--source", "only-one-domain"],
            ["project-run"],
            ["enhance"],
            ["enhance", "--page", "wiki/concepts/x.md", "--iterations", "0"],
        ):
            with (
                self.subTest(argv=argv),
                mock.patch.object(
                    self.agent, "_enter_runtime", side_effect=AssertionError("entered")
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(self.agent.main(argv), 1)
        with mock.patch.object(
            self.agent, "_enter_runtime", return_value=7
        ) as enter:
            self.assertEqual(
                self.agent.main(["quality", "--page", "wiki/concepts/x.md"]), 7
            )
        enter.assert_called_once()

    def test_adapters_ignore_operator_local_model_pins(self) -> None:
        def manifests() -> list[str]:
            return [
                text
                for role in self.generator.load_roles()
                for text in (
                    self.generator.claude_manifest(role),
                    self.generator.codex_manifest(role),
                )
            ]

        with self.generator_paths():
            tracked = manifests()
            (self.root / "tools" / "llm.local.json").write_text(
                json.dumps(
                    {
                        "models": {"claude": "pin-claude", "codex": "pin-codex"},
                        "profiles": {
                            "claude": {"standard": "pin-std", "deep": "pin-deep"},
                            "codex": {"standard": "pin-cstd", "deep": "pin-cdeep"},
                        },
                    }
                )
            )
            with_local_config = manifests()
        self.assertEqual(with_local_config, tracked)
        self.assertFalse(any("pin-" in text for text in with_local_config))

    def test_local_models_are_an_export_only_option(self) -> None:
        (self.root / "tools" / "llm.local.json").write_text(
            json.dumps({"profiles": {"claude": {"standard": "pin-std", "deep": "pin-deep"}}})
        )
        output = self.root / "export"
        with self.generator_paths(), contextlib.redirect_stdout(io.StringIO()):
            with (
                contextlib.redirect_stderr(io.StringIO()) as error,
                self.assertRaises(SystemExit) as refused,
            ):
                self.generator.main(["--local-models"])
            self.assertEqual(refused.exception.code, 2)
            self.assertIn("requires --output-dir", error.getvalue())
            self.assertFalse((self.root / ".claude").exists())
            self.assertEqual(
                self.generator.main(
                    ["--provider", "claude", "--output-dir", str(output), "--local-models"]
                ),
                0,
            )
        exported = "".join(p.read_text() for p in (output / ".claude" / "agents").iterdir())
        self.assertIn('model: "pin-std"', exported)

    def test_export_roundtrip_checks_without_deployed_provider_access(self) -> None:
        output = self.root / "export"
        with self.generator_paths(), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.generator.main(["--output-dir", str(output)]), 0)
            self.assertEqual(
                self.generator.main(["--check", "--output-dir", str(output)]), 0
            )
        for provider in (".claude", ".codex"):
            self.assertEqual(
                len(list((output / provider / "agents").iterdir())), len(AGENT_FILES)
            )
            self.assertFalse((self.root / provider).exists())
        self.assertEqual(list(output.rglob("CLAUDE.md")), [])

    def test_provider_launches_from_nested_project_use_vault_root(self) -> None:
        project = self.root / "projects" / "nested-project"
        project.mkdir(parents=True)
        for provider in ("claude", "codex"):
            with (
                self.subTest(provider=provider),
                contextlib.chdir(project),
                mock.patch.object(self.agent, "ROOT", self.root),
                mock.patch.object(self.agent, "AGENTS_DIR", self.roles),
                mock.patch.object(
                    self.agent,
                    "_run_agent_command",
                    return_value=0,
                ) as run,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    self.agent.invoke_agent(
                        "search",
                        provider,
                        "",
                        "medium",
                        "Find a page",
                        "",
                        [],
                        timeout=42,
                    ),
                    0,
                )
                self.assertEqual(
                    run.call_args.kwargs, {"cwd": self.root, "timeout": 42}
                )
                self.assertEqual(Path.cwd(), project.resolve())

    def test_generated_roles_resolve_from_repository_root_without_host_paths(self) -> None:
        import tomllib

        with self.generator_paths():
            for role in self.generator.load_roles():
                with self.subTest(role=role.name):
                    instruction = self.generator._role_instruction(role)
                    self.assertIn("vault repository root", instruction)
                    self.assertIn("ancestor containing both `AGENTS.md`", instruction)
                    self.assertIn(
                        f"`{role.path.relative_to(self.root).as_posix()}`", instruction
                    )
                    self.assertNotIn(str(self.root), instruction)
                    self.assertIn(instruction, self.generator.claude_manifest(role))
                    codex = tomllib.loads(self.generator.codex_manifest(role))
                    self.assertIn(instruction, codex["developer_instructions"])

    def test_blocked_adapter_directory_reports_blocked_not_drift(self) -> None:
        with (
            self.generator_paths(),
            mock.patch.object(
                Path, "iterdir", side_effect=PermissionError("fixture denial")
            ),
        ):
            with self.assertRaises(self.generator.AdapterAccessError):
                self.generator._check_adapter_set(self.root / ".claude", ".md", set())
        with (
            self.generator_paths(),
            mock.patch.object(
                self.generator,
                "_check_adapter_set",
                side_effect=self.generator.AdapterAccessError("fixture denial"),
            ),
            contextlib.redirect_stderr(io.StringIO()) as error,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                self.generator.main(["--check", "--provider", "claude"]), 2
            )
        self.assertIn("BLOCKED adapter verification", error.getvalue())
        self.assertNotIn("stale or missing", output.getvalue())

    def test_unreadable_adapter_does_not_become_missing_drift(self) -> None:
        with mock.patch.object(
            Path, "read_text", side_effect=PermissionError("fixture denial")
        ):
            with self.assertRaises(self.generator.AdapterAccessError):
                self.generator._sync(self.root / "unreadable.md", "expected", True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(
                self.generator._sync(self.root / "missing.md", "expected", True)
            )
        self.assertIn("stale or missing", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
