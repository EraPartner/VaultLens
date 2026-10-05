"""Fixture-only validation of scheduler rendering and reversible host repairs."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _loader import load_module

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS / "schedule"))
from render_plist import render_plist, validate_plist  # noqa: E402

repairs = load_module(
    "host_repairs", TOOLS / "scripts/repair-provider-host.py", register=True
)


class HostRepairTests(unittest.TestCase):
    def installer_fixture(self, root: Path) -> tuple[Path, dict[str, str]]:
        schedule = root / "vault/tools/schedule"
        schedule.mkdir(parents=True)
        for name in ("install.sh", "render_plist.py"):
            shutil.copyfile(TOOLS / "schedule" / name, schedule / name)
        shutil.copyfile(TOOLS / "llm_provider.py", schedule.parent / "llm_provider.py")
        (schedule / "dispatch.py").write_text("# fixture; never execute\n")
        (schedule / "com.brain.schedule.plist").write_bytes(
            plistlib.dumps(
                {
                    "ProgramArguments": [
                        "/missing/python3",
                        str(schedule / "dispatch.py"),
                        "run",
                    ],
                    "RunAtLoad": True,
                }
            )
        )
        binary = root / "bin"
        binary.mkdir()
        launchctl = binary / "launchctl"
        launchctl.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n')
        launchctl.chmod(0o755)
        env = dict(os.environ)
        for key in tuple(env):
            if key.startswith("VAULTLENS_LLM_") or key == "VAULTLENS_SCHEDULE_ENHANCE":
                env.pop(key)
        env.update(
            {
                "HOME": str(root / "home"),
                "PATH": str(binary) + os.pathsep + env["PATH"],
                "BRAIN_PYTHON": sys.executable,
                "CALL_LOG": str(root / "calls"),
                "VAULTLENS_LLM_CLI": "claude",
            }
        )
        return schedule / "install.sh", env

    def test_installer_bootstraps_once_and_preserves_python_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer, env = self.installer_fixture(root)
            for mode, expected in (
                ("--install", ["bootout", "enable", "bootstrap"]),
                ("--prepare-disabled", ["bootout", "disable"]),
                ("--enable-prepared", ["bootout", "enable", "bootstrap"]),
            ):
                with self.subTest(mode=mode):
                    (root / "calls").unlink(missing_ok=True)
                    result = subprocess.run(
                        ["bash", str(installer), mode],
                        env=env,
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = (root / "calls").read_text().splitlines()
                    self.assertEqual([call.split()[0] for call in calls], expected)
                    installed = (
                        Path(env["HOME"])
                        / "Library/LaunchAgents/com.brain.schedule.plist"
                    )
                    self.assertEqual(
                        plistlib.loads(installed.read_bytes())["ProgramArguments"][0],
                        sys.executable,
                    )

    def test_installer_render_is_read_only_and_rejects_old_python_early(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer, env = self.installer_fixture(root)
            output = root / "rendered.plist"
            result = subprocess.run(
                ["bash", str(installer), "--render", str(output)],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                plistlib.loads(output.read_bytes())["ProgramArguments"][0],
                sys.executable,
            )
            self.assertFalse((root / "calls").exists())
            self.assertFalse(Path(env["HOME"]).exists())
            old_python = root / "bin/old-python"
            old_python.write_text(
                '#!/bin/bash\necho "Detected Python 3.9.6" >&2\nexit 1\n'
            )
            old_python.chmod(0o755)
            env["BRAIN_PYTHON"] = str(old_python)
            result = subprocess.run(
                ["bash", str(installer)],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Python 3.11 or newer is required", result.stderr)
            self.assertIn("Detected Python 3.9.6", result.stderr)
            self.assertFalse((root / "calls").exists())
            self.assertFalse(Path(env["HOME"]).exists())

    def test_renderer_python_override_preserves_source_and_dispatcher(self) -> None:
        arguments = [
            "/missing/python3",
            "/vault/tools/schedule/dispatch.py",
            "run",
        ]
        source: dict[str, object] = {"ProgramArguments": arguments}
        result = render_plist(
            source,
            {},
            config_path=Path("/missing/config.json"),
            python_executable=sys.executable,
        )
        self.assertEqual(
            result["ProgramArguments"],
            [sys.executable, *arguments[1:]],
        )
        self.assertEqual(arguments[0], "/missing/python3")
        for executable in ("python3", "/missing/python3"):
            with (
                self.subTest(executable=executable),
                self.assertRaisesRegex(ValueError, "absolute executable file"),
            ):
                render_plist(source, {}, python_executable=executable)

    def test_plist_uses_target_vault_configuration_and_remains_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout_config = root / "checkout/tools/llm.local.json"
            target_config = root / "deployed vault/tools/llm.local.json"
            for config, cli in ((checkout_config, "claude"), (target_config, "codex")):
                config.parent.mkdir(parents=True)
                config.write_text('{"cli":"' + cli + '"}')
            dispatcher = target_config.parent / "schedule/dispatch.py"
            dispatcher.parent.mkdir()
            dispatcher.write_text("# fixture; never execute\n")
            source: dict[str, object] = {
                "ProgramArguments": ["/fixture/python3", str(dispatcher), "run"],
                "Disabled": True,
            }
            before = {
                path: path.read_bytes() for path in root.rglob("*") if path.is_file()
            }
            with patch.dict(os.environ, {"VAULTLENS_LLM_CLI": "claude"}):
                rendered = render_plist(source, {})
                self.assertEqual(validate_plist(rendered), "codex")
            self.assertTrue(rendered["Disabled"])
            self.assertNotIn("EnvironmentVariables", source)
            self.assertEqual(
                validate_plist(rendered, config_path=checkout_config), "claude"
            )
            overridden = render_plist(source, {"VAULTLENS_LLM_CLI": "claude"})
            self.assertEqual(validate_plist(overridden), "claude")
            target_config.write_text('{"cli":"claude"}')
            self.assertEqual(validate_plist(rendered), "claude")
            target_config.write_text("invalid JSON")
            with self.assertRaisesRegex(
                ValueError, "deployed vault/tools/llm.local.json"
            ):
                validate_plist(rendered)
            target_config.write_bytes(before[target_config])
            self.assertEqual(
                before,
                {path: path.read_bytes() for path in root.rglob("*") if path.is_file()},
            )

    def test_plist_rejects_missing_relative_or_ambiguous_dispatcher(self) -> None:
        for arguments in (
            None,
            "python dispatch.py",
            [],
            ["/fixture/python3"],
            ["/fixture/python3", 123],
            ["/fixture/python3", "tools/schedule/dispatch.py", "run"],
            ["/fixture/python3", "/vault/other/dispatch.py", "run"],
            ["/fixture/python3", "-c", "/vault/tools/schedule/dispatch.py"],
            [
                "/fixture/python3",
                "/vault/tools/schedule/dispatch.py",
                "/other/tools/schedule/dispatch.py",
            ],
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(ValueError, "ProgramArguments"),
            ):
                validate_plist({"ProgramArguments": arguments})

    def test_plist_configuration_follows_resolved_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target/tools/schedule/dispatch.py"
            target.parent.mkdir(parents=True)
            target.write_text("# fixture\n")
            (target.parent.parent / "llm.local.json").write_text('{"cli":"codex"}')
            link = root / "checkout/tools/schedule/dispatch.py"
            link.parent.mkdir(parents=True)
            link.symlink_to(target)
            (link.parent.parent / "llm.local.json").write_text('{"cli":"claude"}')
            self.assertEqual(
                validate_plist({"ProgramArguments": ["/fixture/python3", str(link)]}),
                "codex",
            )

    def test_plist_follows_shared_provider_and_keeps_enablement_and_enhancement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "llm.local.json"
            config.write_text('{"cli":"claude"}')
            source_environment = {
                "PATH": "/fixture/bin",
                "VAULTLENS_LLM_CLI": "codex",
                "VAULTLENS_LLM_MODEL": "codex-model",
                "VAULTLENS_SCHEDULE_ENHANCE": "1",
            }
            source: dict[str, object] = {
                "Disabled": True,
                "EnvironmentVariables": source_environment,
            }
            result = render_plist(source, {}, config_path=config)
            self.assertEqual(validate_plist(result, config_path=config), "claude")
            self.assertTrue(result["Disabled"])
            self.assertEqual(
                result["EnvironmentVariables"],
                {
                    "PATH": "/fixture/bin",
                    "VAULTLENS_SCHEDULE_ENHANCE": "1",
                },
            )
            self.assertEqual(source_environment["VAULTLENS_LLM_CLI"], "codex")
            config.write_text('{"cli":"codex"}')
            self.assertEqual(validate_plist(result, config_path=config), "codex")

    def test_explicit_scheduler_override_and_bad_prepared_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "missing.json"
            result = render_plist(
                {},
                {
                    "VAULTLENS_LLM_CLI": "codex",
                    "VAULTLENS_LLM_MODEL": "custom",
                },
                config_path=config,
            )
            self.assertEqual(validate_plist(result, config_path=config), "codex")
            for env in (
                {"VAULTLENS_LLM_CLI": "unknown"},
                {"VAULTLENS_LLM_CLI": ""},
                {"VAULTLENS_LLM_HEALTH_HOST": ""},
                {"VAULTLENS_SCHEDULE_ENHANCE": "maybe"},
            ):
                with self.subTest(env=env), self.assertRaises(ValueError):
                    validate_plist({"EnvironmentVariables": env}, config_path=config)

    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        vault, source, functions = root / "vault", root / "source", root / "functions"
        (source / "tools/shell").mkdir(parents=True)
        (source / "tools/shell/brain-wiki.fish").write_text(
            "function brain-wiki\nend\n"
        )
        (source / "tools/shell/__brain_project_root.fish").write_text(
            "function __brain_project_root\nend\n"
        )
        for directory in (
            vault,
            vault / "wiki",
            vault / "projects",
            vault / "projects/example",
        ):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "AGENTS.md").write_text("Shared instructions\n")
        (vault / "projects/example/project.md").write_text("Fixture\n")
        (vault / ".gitignore").write_text("unrelated-ignore\n")
        plist = root / "schedule.plist"
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Disabled": True,
                    "EnvironmentVariables": {
                        "VAULTLENS_LLM_CLI": "codex",
                        "VAULTLENS_SCHEDULE_ENHANCE": "1",
                    },
                }
            )
        )
        return vault, source, functions, plist

    def test_plan_apply_backups_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault, source, functions, plist = self.fixture(root)
            before = (vault / ".gitignore").read_bytes()
            changes = repairs.plan_repairs(vault, source, functions, plist)
            self.assertEqual((vault / ".gitignore").read_bytes(), before)
            self.assertFalse(functions.exists())
            backup = root / "backups"
            repairs.apply_repairs(changes, backup)
            self.assertFalse((vault / ".devcontainer").exists())
            ignored = (vault / ".gitignore").read_text()
            self.assertIn("tools/access.local.json", ignored)
            self.assertIn("tools/runtime-state/", ignored)
            self.assertFalse((vault / "projects/example/CLAUDE.md").exists())
            installed = plistlib.loads(plist.read_bytes())
            self.assertTrue(installed["Disabled"])
            self.assertEqual(
                installed["EnvironmentVariables"], {"VAULTLENS_SCHEDULE_ENHANCE": "1"}
            )
            self.assertTrue((backup / "manifest.txt").is_file())
            self.assertEqual(
                (functions / "__brain_project_root.fish").read_bytes(),
                (source / "tools/shell/__brain_project_root.fish").read_bytes(),
            )
            self.assertEqual(repairs.plan_repairs(vault, source, functions, plist), [])

    def test_legacy_vaultlens_aliases_are_left_untouched(self) -> None:
        # The container-era alias migration was removed; old functions stay as they are.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault, source, functions, plist = self.fixture(root)
            functions.mkdir(parents=True, exist_ok=True)
            legacy = functions / "vaultlens-claude.fish"
            legacy.write_text("function vaultlens-claude\n    echo old\nend\n")
            paths = [proposed.path for proposed in repairs.plan_repairs(vault, source, functions, plist)]
            self.assertNotIn(legacy, paths)
            self.assertFalse((functions / "__vaultlens_launch.fish").exists())
            self.assertFalse(hasattr(repairs, "legacy_alias_fallback"))

    def test_scheduler_symlink_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault, source, functions, plist = self.fixture(root)
            link = root / "installed.plist"
            link.symlink_to(plist)
            changes = repairs.plan_repairs(vault, source, functions, link)
            self.assertTrue(any(item.path == plist.resolve() for item in changes))
            self.assertFalse(any(item.path == link for item in changes))
            repairs.apply_repairs(changes, root / "backups")
            self.assertTrue(link.is_symlink())

    def test_stale_plan_and_symlinks_are_refused_before_other_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            first.write_bytes(b"old")
            second.write_bytes(b"old")
            changes = [repairs.change(first, b"new"), repairs.change(second, b"new")]
            second.write_bytes(b"concurrent edit")
            with self.assertRaisesRegex(ValueError, "changed since preview"):
                repairs.apply_repairs(changes, root / "backups")
            self.assertEqual(first.read_bytes(), b"old")
            self.assertFalse((root / "backups").exists())
            link = root / "link"
            link.symlink_to(first)
            with self.assertRaisesRegex(ValueError, "symlink"):
                repairs.change(link, b"new")

    def test_partial_write_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            first.write_bytes(b"old")
            changes = [repairs.change(first, b"new"), repairs.change(second, b"new")]
            original = repairs._atomic_write

            def fail_second(path: Path, payload: bytes, mode: int = 0o644) -> None:
                if path == second:
                    raise OSError("fixture failure")
                original(path, payload, mode)

            with patch.object(repairs, "_atomic_write", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "fixture failure"):
                    repairs.apply_repairs(changes, root / "backups")
            self.assertEqual(first.read_bytes(), b"old")
            self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
