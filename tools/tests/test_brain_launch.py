#!/usr/bin/env python3
"""Test local access profiles and fish handoffs against harmless fixture CLIs."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
FISH = shutil.which("fish")
PYTHON = Path(sys.executable)
sys.path.insert(0, str(TOOLS))

import brain_launch as planner  # noqa: E402
from _loader import load_module  # noqa: E402

# JSON the fixture CLI records about its own invocation; read by key and index only.
Captured = dict[str, Any]


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name).resolve()
        role_dir = self.root / ".agents" / "roles"
        role_dir.mkdir(parents=True)
        for source in (TOOLS.parent / ".agents" / "roles").glob("*.md"):
            shutil.copy2(source, role_dir / source.name)

    def plan(
        self,
        args: list[str],
        mode: str = "wiki",
        *,
        cwd: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> planner.Launch:
        mappings = self.root / "tools" / "model-profiles.json"
        if not mappings.exists():
            mappings.parent.mkdir(exist_ok=True)
            shutil.copy2(TOOLS / "model-profiles.json", mappings)
        access = self.root / "tools" / "access-profiles.json"
        if not access.exists():
            shutil.copy2(TOOLS / "access-profiles.json", access)
        return planner.plan_launch(
            mode,
            args,
            root=self.root,
            environ={} if environ is None else environ,
            cwd=cwd,
        )

    def test_deterministic_commands_do_not_require_an_agent_profile(self) -> None:
        cases = [
            (["lint"], "reader"),
            (["lint", "--fix"], "author"),
            (["append-log", "entry"], "author"),
            (["preprocess"], "raw"),
            (["index"], "reader"),
            (["index", "--rebuild"], "author"),
            (["links", "--fix"], "reader"),
            (["links", "--write"], "reader"),
            (["links", "--fix", "--write"], "author"),
            (["inventory", "list"], "reader"),
            (["inventory", "new"], "author"),
            (["archive", "list"], "reader"),
            (["archive", "page", "concepts/demo"], "author"),
            (["archive", "restore", "concepts/demo"], "author"),
        ]
        for action in ("new", "link", "freeze", "unfreeze"):
            cases.append((["project", action, "demo"], "projects"))
        for action in ("list", "show"):
            cases.append((["project", action, "demo"], "reader"))
        for action in ("enable", "disable", "scaffold-all", "complete", "resolve"):
            cases.append((["project", "agenda", action, "demo"], "projects"))
        for action in ("due", "clarifications", "status", "lint", "new-id"):
            cases.append((["project", "agenda", action, "demo"], "reader"))
        for argv, _access in cases:
            with self.subTest(argv=argv):
                plan = self.plan(argv)
                self.assertEqual(plan.profile, "")
                self.assertEqual(plan.command, ["python3", "tools/wiki.py", *argv])

    def test_agent_names_match_real_launcher(self) -> None:
        agent = load_module("fixture_wiki_agent", TOOLS / "agents" / "wiki-agent.py")
        self.assertEqual(planner.AGENT_NAMES, set(agent.AGENT_FILES))
        for name, permissions in agent.AGENT_PERMISSIONS.items():
            args = [name]
            if name == "project-run":
                args += ["--project", "demo"]
            plan = self.plan(args)
            expected = (
                "project-write"
                if name == "project-run"
                else "wiki-write"
                if permissions["write"]
                else "source-read"
                if name == "verify"
                else "cos-read"
                if name == "cos"
                else "wiki-read"
            )
            with self.subTest(name=name):
                self.assertEqual(plan.profile, expected)

    def test_initialization_is_an_explicit_host_command(self) -> None:
        self.assertEqual(
            self.plan(["init"]).command, ["python3", "tools/wiki.py", "init"]
        )

    def test_cli_equal_form_is_normalized_and_explicit_model_kept(self) -> None:
        plan = self.plan(
            ["search", "--cli=codex", "--model=chosen", "--prompt", "two words"]
        )
        self.assertEqual(plan.provider, "codex")
        self.assertEqual(plan.command.count("--cli"), 1)
        self.assertIn("--model=chosen", plan.command)
        self.assertNotIn("--model", plan.command)
        self.assertNotIn("--cli=codex", plan.command)
        self.assertEqual(plan.command[plan.command.index("--prompt") + 1], "two words")
        with self.assertRaises(ValueError):
            self.plan(["search", "--cli"])
        with self.assertRaises(ValueError):
            self.plan(["search", "--cli=other"])

    def test_project_slug_cannot_escape_write_scope(self) -> None:
        for slug in (
            "",
            ".",
            "..",
            "../outside",
            "/tmp",
            "demo/child",
            "demo\\child",
            "demo:child",
            "demo\nchild",
            "demo\rchild",
        ):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                self.plan(["project-run", "--project", slug])
        valid = self.plan(["project-run", "--project=demo-project"])
        self.assertEqual(
            (valid.profile, valid.write_path),
            ("project-write", "projects/demo-project"),
        )

    def test_interactive_access_options_are_separate_from_native_options(self) -> None:
        plan = self.plan(
            [
                "--access-profile=selected-notes",
                "--project",
                "demo",
                "--read-path",
                "wiki/concepts/two words.md",
                "--read-path=wiki/topics/other.md",
                "--resume",
                "literal prompt",
            ],
            "agent",
        )
        self.assertEqual(plan.profile, "selected-notes")
        self.assertEqual(plan.project, "demo")
        self.assertEqual(
            plan.read_paths, ("wiki/concepts/two words.md", "wiki/topics/other.md")
        )
        self.assertEqual(plan.command, ["claude", "--resume", "literal prompt"])
        literal = self.plan(["--", "--access-profile=literal"], "agent")
        self.assertEqual(literal.command, ["claude", "--", "--access-profile=literal"])

    def test_project_work_uses_only_the_current_project_by_default(self) -> None:
        project = self.root / "projects" / "demo"
        project.mkdir(parents=True)
        (project / "project.md").write_text("# Demo\n", encoding="utf-8")
        nested = project / "notes"
        nested.mkdir()
        for mode in ("agent", "shell"):
            with self.subTest(mode=mode):
                plan = self.plan([], mode, cwd=nested)
                self.assertEqual(plan.profile, "project-write")
                self.assertEqual(plan.project, "demo")
        self.assertEqual(self.plan([], "agent", cwd=self.root).profile, "wiki-read")
        self.assertEqual(self.plan([], "shell").profile, "wiki-read")

    def test_root_sessions_cannot_edit_until_the_writer_profile_is_explicit(
        self,
    ) -> None:
        from local_access import resolve_scope

        note = self.root / "wiki/concepts/fixture.md"
        note.parent.mkdir(parents=True)
        note.write_text("Public synthetic note\n")
        source = self.root / "raw/inbox/fixture.md"
        source.parent.mkdir(parents=True)
        source.write_text("Public immutable source\n")
        cases = [("agent", ["--cli=" + cli]) for cli in ("claude", "codex")]
        cases.append(("shell", []))
        for mode, args in cases:
            for editing in (False, True):
                with self.subTest(mode=mode, args=args, editing=editing):
                    arguments = (
                        [*args, "--access-profile=wiki-write"] if editing else args
                    )
                    plan = self.plan(arguments, mode, cwd=self.root)
                    scope = resolve_scope(
                        self.root,
                        plan.profile,
                        capability="wiki-write" if editing else "read",
                    )
                    self.assertTrue(scope.readable(note))
                    self.assertEqual(scope.writable(note), editing)
                    self.assertFalse(scope.writable(source))
                    self.assertEqual(plan.write_path, "wiki" if editing else "")
                    self.assertNotIn("--access-profile=wiki-write", plan.command)

    def test_interactive_project_traversal_is_rejected_before_runtime(self) -> None:
        for mode in ("agent", "shell"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.plan(["--project=../other"], mode)

    def test_execute_interactive_uses_runtime_and_keeps_argv_separate(self) -> None:
        runtime = mock.Mock()
        runtime.launch_interactive.return_value = 7
        plan = self.plan(
            [
                "--cli=codex",
                "--access-profile=wiki-read",
                "--read-path=wiki/topics/demo.md",
                "-m",
                "chosen",
            ],
            "agent",
        )
        with mock.patch.dict(sys.modules, {"local_runtime": runtime}):
            self.assertEqual(planner.execute_launch(plan, root=self.root), 7)
        runtime.launch_interactive.assert_called_once_with(
            self.root,
            "codex",
            ["-m", "chosen"],
            profile="wiki-read",
            project=None,
            read_paths=("wiki/topics/demo.md",),
        )

    def test_execute_deterministic_command_uses_selected_python_and_root(self) -> None:
        plan = self.plan(["project", "list"])
        with mock.patch.object(planner.subprocess, "run") as run:
            run.return_value.returncode = 4
            self.assertEqual(planner.execute_launch(plan, root=self.root), 4)
        run.assert_called_once_with(
            [sys.executable, str(self.root / "tools" / "wiki.py"), "project", "list"],
            cwd=self.root,
            check=False,
        )

    def test_shared_provider_and_native_model_behavior(self) -> None:
        config = self.root / "tools" / "llm.local.json"
        config.parent.mkdir()
        config.write_text('{"cli": "codex"}', encoding="utf-8")
        for mode, args in (("wiki", ["search"]), ("cos", []), ("agent", ["--resume"])):
            with self.subTest(mode=mode):
                self.assertEqual(self.plan(args, mode).provider, "codex")
        for cli in ("claude", "codex"):
            native = self.plan(["--cli=" + cli], "agent")
            self.assertEqual(native.command, [cli])
        config.write_text(
            '{"cli": "codex", "models": {"codex": "saved"}}', encoding="utf-8"
        )
        self.assertEqual(self.plan([], "agent").command, ["codex", "--model", "saved"])
        self.assertEqual(
            self.plan(["--model=explicit"], "agent").command,
            ["codex", "--model=explicit"],
        )
        self.assertEqual(
            self.plan(
                [],
                "agent",
                environ={"VAULTLENS_LLM_CLI": "claude", "VAULTLENS_LLM_MODEL": "env"},
            ).command,
            ["claude", "--model", "env"],
        )

    def test_invalid_config_prevents_agent_plan(self) -> None:
        config = self.root / "tools" / "llm.local.json"
        config.parent.mkdir()
        config.write_text("not json", encoding="utf-8")
        for mode, args in (("wiki", ["search"]), ("cos", []), ("agent", [])):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.plan(args, mode)
        with mock.patch.object(
            Path, "read_text", side_effect=PermissionError("unreadable")
        ):
            with self.assertRaises(ValueError):
                self.plan([], "agent")

    def test_native_codex_model_flags_override_saved_model(self) -> None:
        config = self.root / "tools" / "llm.local.json"
        config.parent.mkdir()
        config.write_text(
            '{"cli": "codex", "models": {"codex": "saved"}}', encoding="utf-8"
        )
        for arguments in (["-m", "explicit"], ["-mexplicit"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    self.plan(arguments, "agent").command, ["codex", *arguments]
                )
        self.assertEqual(
            self.plan(["--", "--model=literal"], "agent").command,
            ["codex", "--model", "saved", "--", "--model=literal"],
        )

    def test_empty_headless_model_freezes_native_default(self) -> None:
        config = self.root / "tools" / "llm.local.json"
        config.parent.mkdir()
        config.write_text(
            '{"cli": "codex", "models": {"codex": "saved"}}', encoding="utf-8"
        )
        for arguments in (["search", "--model", ""], ["search", "--model="]):
            with self.subTest(arguments=arguments):
                plan = self.plan(
                    arguments, environ={"VAULTLENS_LLM_MODEL": "environment"}
                )
                self.assertEqual(
                    plan.command,
                    [
                        "python3",
                        "tools/agents/wiki-agent.py",
                        "search",
                        "--cli",
                        "codex",
                        *arguments[1:],
                        "--effort",
                        "medium",
                    ],
                )
                self.assertNotIn("environment", plan.command)
                self.assertNotIn("saved", plan.command)


@unittest.skipUnless(FISH is not None, "fish must be installed to test shell wrappers")
class FishWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = tempfile.TemporaryDirectory(prefix="brain launch ")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name).resolve()
        role_dir = self.root / ".agents" / "roles"
        role_dir.mkdir(parents=True)
        for source in (TOOLS.parent / ".agents" / "roles").glob("*.md"):
            shutil.copy2(source, role_dir / source.name)
        tools = self.root / "tools"
        tools.mkdir()
        for name in (
            "brain_launch.py",
            "llm_provider.py",
            "agent_profiles.py",
            "model-profiles.json",
        ):
            shutil.copyfile(TOOLS / name, tools / name)
        self.wrappers = tools / "shell"
        self.wrappers.mkdir()
        for source in (TOOLS / "shell").glob("*.fish"):
            shutil.copyfile(source, self.wrappers / source.name)
        self.capture = self.root / "capture.json"
        (tools / "local_runtime.py").write_text(
            "import json, os, pathlib, sys\n"
            "def default_access_profile(agent, root=None):\n"
            "    return {'ingest':'wiki-write', 'enhance':'wiki-write', 'project-run':'project-write', 'verify':'source-read', 'cos':'cos-read'}.get(agent, 'wiki-read')\n"
            "def launch_interactive(root, provider, args, *, profile=None, project=None, read_paths=()):\n"
            "    pathlib.Path(os.environ['FIXTURE_CAPTURE']).write_text(json.dumps({'argv':[provider,*args], 'profile':profile, 'project':project, 'read_paths':read_paths, 'root':str(root)}))\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    pathlib.Path(os.environ['FIXTURE_CAPTURE']).write_text(json.dumps({'argv':sys.argv[1:]}))\n",
            encoding="utf-8",
        )
        entrypoint = (
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['FIXTURE_CAPTURE']).write_text(json.dumps({'argv':sys.argv[1:], 'root':str(pathlib.Path.cwd())}))\n"
        )
        (tools / "wiki.py").write_text(entrypoint, encoding="utf-8")
        agents = tools / "agents"
        agents.mkdir()
        (agents / "wiki-agent.py").write_text(entrypoint, encoding="utf-8")

    def run_wrapper(
        self,
        name: str,
        args: list[str],
        env: dict[str, str] | None = None,
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        script = self.root / "invoke.fish"
        script.write_text(
            "\n".join(
                [
                    *(
                        "source " + shlex.quote(str(path))
                        for path in sorted(self.wrappers.glob("*.fish"))
                    ),
                    "function __brain_project_root",
                    "    printf '%s\\n' " + shlex.quote(str(self.root)),
                    "end",
                    " ".join(shlex.quote(part) for part in [name, *args]),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "BRAIN_PYTHON": str(PYTHON),
            "FIXTURE_CAPTURE": str(self.capture),
            **(env or {}),
        }
        return subprocess.run(
            [str(FISH), "--no-config", str(script)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=cwd or self.root,
        )

    def captured(self) -> Captured:
        return json.loads(self.capture.read_text(encoding="utf-8"))

    def test_shared_wrappers_handoff_provider_argv_and_profiles(self) -> None:
        for name, args in (
            ("brain-wiki", ["search", "--prompt", "two words"]),
            ("brain-wiki", ["links", "--fix", "--write"]),
            ("brain-cos", ["--mode", "brief"]),
            ("brain-agent", ["--resume"]),
        ):
            with self.subTest(name=name, args=args):
                result = self.run_wrapper(name, args, {"VAULTLENS_LLM_CLI": "codex"})
                self.assertEqual(result.returncode, 0, result.stderr)
                captured = self.captured()
                self.assertEqual(captured["root"], str(self.root))
                if name == "brain-agent":
                    self.assertEqual(captured["argv"], ["codex", "--resume"])
                    self.assertEqual(captured["profile"], "wiki-read")
                else:
                    self.assertEqual(
                        captured["argv"][0],
                        "cos" if name == "brain-cos" else args[0],
                    )
                    if name == "brain-cos" or args[0] == "search":
                        self.assertEqual(
                            captured["argv"][captured["argv"].index("--cli") + 1],
                            "codex",
                        )
                if "two words" in args:
                    self.assertEqual(
                        captured["argv"][captured["argv"].index("--prompt") + 1],
                        "two words",
                    )

    def test_initialization_is_direct_without_provider_configuration(self) -> None:
        (self.root / "tools" / "llm.local.json").write_text("broken", encoding="utf-8")
        result = self.run_wrapper("brain-wiki", ["init"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.captured()["argv"], ["init"])

    def test_explicit_cli_and_project_scope_survive_fish(self) -> None:
        result = self.run_wrapper(
            "brain-wiki",
            ["project-run", "--cli=claude", "--project=demo"],
            {"VAULTLENS_LLM_CLI": "codex"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = self.captured()
        self.assertEqual(actual["argv"][actual["argv"].index("--cli") + 1], "claude")
        self.assertIn("--project=demo", actual["argv"])
        self.assertIn("--cli", actual["argv"])
        self.assertNotIn("--cli=claude", actual["argv"])

    def test_named_native_wrappers_are_explicit(self) -> None:
        for cli in ("claude", "codex"):
            with self.subTest(cli=cli):
                result = self.run_wrapper(
                    "brain-" + cli,
                    ["--resume", "two words"],
                    {"VAULTLENS_LLM_CLI": "codex" if cli == "claude" else "claude"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.captured()["argv"], [cli, "--resume", "two words"]
                )
                self.assertEqual(self.captured()["profile"], "wiki-read")

    def test_interactive_wrappers_require_explicit_root_wiki_editing(self) -> None:
        cases = [
            ("brain-agent", {"VAULTLENS_LLM_CLI": cli}) for cli in ("claude", "codex")
        ] + [("brain-claude", {}), ("brain-codex", {}), ("brain-shell", {})]
        for name, environment in cases:
            for editing in (False, True):
                with self.subTest(name=name, env=environment, editing=editing):
                    arguments = ["--access-profile", "wiki-write"] if editing else []
                    result = self.run_wrapper(name, arguments, environment)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    actual = self.captured()
                    if name == "brain-shell":
                        profile = actual["argv"][actual["argv"].index("--profile") + 1]
                        self.assertEqual(actual["argv"][-3:], ["--", "/bin/zsh", "-f"])
                    else:
                        profile = actual["profile"]
                        self.assertEqual(actual["project"], None)
                        cli = environment.get(
                            "VAULTLENS_LLM_CLI", name.removeprefix("brain-")
                        )
                        self.assertEqual(actual["argv"], [cli])
                    self.assertEqual(profile, "wiki-write" if editing else "wiki-read")

    def test_interactive_wrappers_infer_exact_nested_project_scope(self) -> None:
        project = self.root / "projects/demo"
        nested = project / "notes/deeper"
        nested.mkdir(parents=True)
        (project / "project.md").write_text("# Public synthetic project\n")
        for name in ("brain-claude", "brain-codex", "brain-shell"):
            with self.subTest(name=name):
                result = self.run_wrapper(name, [], cwd=nested)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = self.captured()
                if name == "brain-shell":
                    argv = actual["argv"]
                    self.assertEqual(argv[argv.index("--profile") + 1], "project-write")
                    self.assertEqual(argv[argv.index("--project") + 1], "demo")
                else:
                    self.assertEqual(actual["profile"], "project-write")
                    self.assertEqual(actual["project"], "demo")

    def test_native_model_flags_and_literal_data_survive_fish(self) -> None:
        (self.root / "tools" / "llm.local.json").write_text(
            '{"cli": "codex", "models": {"codex": "saved"}}', encoding="utf-8"
        )
        for arguments, expected in (
            (["-m", "explicit"], ["codex", "-m", "explicit"]),
            (["-mexplicit"], ["codex", "-mexplicit"]),
            (
                ["--", "--model=literal"],
                ["codex", "--model", "saved", "--", "--model=literal"],
            ),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_wrapper("brain-agent", arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.captured()["argv"], expected)

    def test_empty_model_argument_survives_fish_handoff(self) -> None:
        (self.root / "tools" / "llm.local.json").write_text(
            '{"cli": "codex", "models": {"codex": "saved"}}', encoding="utf-8"
        )
        result = self.run_wrapper(
            "brain-wiki",
            ["search", "--model", ""],
            {"VAULTLENS_LLM_MODEL": "environment"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.captured()["argv"],
            [
                "search",
                "--cli",
                "codex",
                "--model",
                "",
                "--effort",
                "medium",
            ],
        )

    def test_bad_config_and_traversal_never_reach_agent(self) -> None:
        config = self.root / "tools" / "llm.local.json"
        for data, name, args in (
            ("{broken", "brain-agent", []),
            ('{"cli":"other"}', "brain-cos", []),
            ("{}", "brain-wiki", ["project-run", "--project=../outside"]),
        ):
            with self.subTest(data=data, name=name):
                config.write_text(data, encoding="utf-8")
                result = self.run_wrapper(name, args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def test_provider_switch_is_local_and_used_by_all_shared_wrappers(self) -> None:
        result = self.run_wrapper("brain-provider", ["codex", "--model", "saved-model"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.capture.exists())
        for name, args in (
            ("brain-agent", []),
            ("brain-wiki", ["search"]),
            ("brain-cos", []),
        ):
            with self.subTest(name=name):
                result = self.run_wrapper(name, args)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = self.captured()
                self.assertIn("codex", actual["argv"])
                model_index = actual["argv"].index("--model")
                self.assertEqual(actual["argv"][model_index + 1], "saved-model")

    def test_interactive_policy_flags_survive_fish_without_reaching_provider(
        self,
    ) -> None:
        result = self.run_wrapper(
            "brain-codex",
            [
                "--access-profile=selected-notes",
                "--project=demo",
                "--read-path",
                "wiki/two words.md",
                "--resume",
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = self.captured()
        self.assertEqual(actual["argv"], ["codex", "--resume"])
        self.assertEqual(actual["profile"], "selected-notes")
        self.assertEqual(actual["project"], "demo")
        self.assertEqual(actual["read_paths"], ["wiki/two words.md"])

    def test_shell_uses_runtime_exec_without_any_provider(self) -> None:
        result = self.run_wrapper(
            "brain-shell",
            ["--access-profile=wiki-read", "--read-path=wiki/topics/demo.md"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.captured()["argv"],
            [
                "exec",
                "--root",
                str(self.root),
                "--profile",
                "wiki-read",
                "--read-path",
                "wiki/topics/demo.md",
                "--",
                "/bin/zsh",
                "-f",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
