#!/usr/bin/env python3
"""Fixture tests for role cost policy; never invoke either model CLI."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import agent_profiles as profiles  # noqa: E402
import brain_launch  # noqa: E402
import llm_provider  # noqa: E402


class RoleProfileTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.roles = self.root / ".agents" / "roles"
        self.roles.mkdir(parents=True)
        for source in (TOOLS.parent / ".agents" / "roles").glob("*.md"):
            shutil.copy2(source, self.roles / source.name)
        (self.root / "tools").mkdir()
        shutil.copy2(
            TOOLS / "model-profiles.json", self.root / "tools" / "model-profiles.json"
        )
        shutil.copy2(TOOLS / "access-profiles.json", self.root / "tools")
        self.config = self.root / "tools" / "llm.local.json"

    def resolve(self, agent="search", **kwargs):
        kwargs.setdefault("environ", {})
        return profiles.resolve_role_settings(agent, root=self.root, **kwargs)

    def save(self, data):
        self.config.write_text(json.dumps(data))

    def test_bundled_roles_preserve_model_and_effort_policy(self):
        for agent in profiles.AGENT_FILES:
            role = profiles.load_role(self.roles / profiles.AGENT_FILES[agent])
            for cli in ("claude", "codex"):
                provider, effort = self.resolve(agent, cli=cli)
                self.assertEqual(effort, role.reasoning_effort)
                self.assertEqual(
                    provider.model,
                    {"standard": "gpt-6-luna", "deep": "gpt-6.1-sol"}[
                        role.model_profile
                    ]
                    if cli == "codex"
                    else {"standard": "sonnet", "deep": "opus"}[role.model_profile],
                )
        self.assertEqual(self.resolve("search")[1], "medium")
        self.assertEqual(self.resolve("enhance")[1], "xhigh")

    def test_provider_profile_mapping_changes_only_its_provider(self):
        self.save({"profiles": {"codex": {"standard": "economy", "deep": "reasoner"}}})
        self.assertEqual(self.resolve(cli="codex")[0].model, "economy")
        self.assertEqual(self.resolve("enhance", cli="codex")[0].model, "reasoner")
        self.assertEqual(self.resolve()[0].model, "sonnet")
        self.save({"profiles": {"claude": {"deep": "custom-deep"}}})
        self.assertEqual(self.resolve("enhance")[0].model, "custom-deep")
        self.assertEqual(self.resolve()[0].model, "sonnet")

    def test_overrides_and_explicit_empty_preserve_precedence(self):
        self.save(
            {
                "models": {"claude": "saved"},
                "profiles": {"claude": {"standard": "profile"}},
            }
        )
        self.assertEqual(self.resolve()[0].model, "saved")
        env = {"VAULTLENS_LLM_MODEL": "environment"}
        self.assertEqual(self.resolve(environ=env)[0].model, "environment")
        self.assertEqual(
            self.resolve(model="explicit", environ=env)[0].model, "explicit"
        )
        self.assertEqual(self.resolve(model="", environ=env)[0].model, "")
        self.assertEqual(self.resolve(environ={"VAULTLENS_LLM_MODEL": ""})[0].model, "")
        self.assertEqual(self.resolve(effort="low")[1], "low")
        self.save({"profiles": {"claude": {"standard": ""}}})
        self.assertEqual(self.resolve()[0].model, "")

    def test_model_names_round_trip_as_opaque_values(self):
        model = 'custom "model" \\ name'
        self.save({"profiles": {"codex": {"standard": model}}})
        self.assertEqual(self.resolve(cli="codex")[0].model, model)

    def test_invalid_metadata_and_config_fail_closed(self):
        source = self.roles / profiles.AGENT_FILES["search"]
        original = source.read_text()
        for old, new in (
            ("model_profile: standard", "model_profile: invalid"),
            ("reasoning_effort: medium", "reasoning_effort: invalid"),
        ):
            source.write_text(original.replace(old, new))
            with self.assertRaises(ValueError):
                self.resolve()
        source.write_text(original)
        for invalid in (
            {"profiles": []},
            {"profiles": {"other": {}}},
            {"profiles": {"claude": {"standard": None}}},
            {"profiles": {"codex": {"unknown": "x"}}},
        ):
            self.save(invalid)
            with self.assertRaises(ValueError):
                self.resolve(model="explicit")
        self.config.unlink()
        (self.root / "tools" / "model-profiles.json").write_text("{broken")
        with self.assertRaises(ValueError):
            self.resolve()

    def test_selecting_provider_preserves_role_mappings(self):
        mappings = {"codex": {"standard": "economy"}}
        self.save({"profiles": mappings})
        llm_provider.select_provider("codex", path=self.config)
        self.assertEqual(llm_provider.load_config(self.config)["profiles"], mappings)
        self.assertEqual(self.resolve()[0].model, "economy")

    def test_host_freezes_resolved_role_settings_and_keeps_overrides(self):
        for agent, model, effort in (
            ("search", "sonnet", "medium"),
            ("enhance", "opus", "xhigh"),
        ):
            command = brain_launch.plan_launch(
                "wiki", [agent], root=self.root, environ={}
            ).command
            self.assertEqual(command[command.index("--model") + 1], model)
            self.assertEqual(command[command.index("--effort") + 1], effort)
        command = brain_launch.plan_launch(
            "wiki", ["search", "--model=", "--effort=low"], root=self.root, environ={}
        ).command
        self.assertIn("--model=", command)
        self.assertIn("--effort=low", command)
        self.assertNotIn("--model", command)
        self.assertNotIn("--effort", command)

    def test_headless_entrypoint_applies_role_defaults_and_flags(self):
        spec = importlib.util.spec_from_file_location(
            "profile_wiki_agent", TOOLS / "agents" / "wiki-agent.py"
        )
        agent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agent)
        with (
            mock.patch.object(agent, "ROOT", self.root),
            mock.patch.object(agent, "_enter_runtime", return_value=None),
            mock.patch.object(agent, "_install_signal_handlers"),
            mock.patch.object(agent, "run_agent", return_value=0) as run,
            mock.patch.dict("os.environ", {}, clear=True),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(agent.main(["search", "--source", "question"]), 0)
            args = run.call_args.args[0]
            self.assertEqual(
                (args.cli, args.model, args.effort), ("claude", "sonnet", "medium")
            )
            self.assertEqual(
                agent.main(
                    ["search", "--source", "question", "--model", "", "--effort", "low"]
                ),
                0,
            )
            args = run.call_args.args[0]
            self.assertEqual((args.model, args.effort), ("", "low"))

    def test_adapters_share_mappings_and_escape_names(self):
        spec = importlib.util.spec_from_file_location(
            "profile_generator", TOOLS / "agents" / "generate-adapters.py"
        )
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        generator.ROOT = self.root
        name = 'custom "model" \\ name'
        self.save(
            {
                "profiles": {
                    "claude": {"standard": "custom-standard"},
                    "codex": {"standard": name, "deep": "custom-deep"},
                }
            }
        )
        search = profiles.load_role(self.roles / profiles.AGENT_FILES["search"])
        enhance = profiles.load_role(self.roles / profiles.AGENT_FILES["enhance"])
        # A global env model is scoped to a launch; it must not contaminate both providers' adapters.
        with mock.patch.dict("os.environ", {"VAULTLENS_LLM_MODEL": "wrong-provider"}):
            claude = generator.claude_manifest(search)
            codex = generator.codex_manifest(search)
        import tomllib

        self.assertIn('model: "custom-standard"', claude)
        self.assertIn("effort: medium", claude)
        self.assertEqual(tomllib.loads(codex)["model"], name)
        self.assertEqual(tomllib.loads(codex)["model_reasoning_effort"], "medium")
        self.assertEqual(
            tomllib.loads(generator.codex_manifest(enhance))["model"], "custom-deep"
        )
        self.assertEqual(
            tomllib.loads(generator.codex_manifest(enhance))["model_reasoning_effort"],
            "xhigh",
        )
        self.save({"profiles": {"claude": {"standard": ""}, "codex": {"standard": ""}}})
        self.assertIn('model: "inherit"', generator.claude_manifest(search))
        self.assertNotIn("model", tomllib.loads(generator.codex_manifest(search)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
