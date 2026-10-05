"""Scheduler plist and sudoers templating regressions; fixtures only."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
SCHEDULE = TOOLS / "schedule"
sys.path.insert(0, str(SCHEDULE))
from render_plist import (  # noqa: E402
    default_substitutions,
    render_plist,
    render_sudoers,
    validate_plist,
)

PLIST = SCHEDULE / "com.brain.schedule.plist"
SUDOERS = SCHEDULE / "brain-schedule.sudoers"


def clean_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("VAULTLENS_LLM_") or key == "VAULTLENS_SCHEDULE_ENHANCE":
            env.pop(key)
    return env


class TemplateTests(unittest.TestCase):
    def test_tracked_templates_hold_no_host_specific_values(self) -> None:
        for template in (PLIST, SUDOERS):
            text = template.read_text(encoding="utf-8")
            with self.subTest(template=template.name):
                self.assertNotIn("/Users/", text)
                self.assertNotRegex(text, r"(?m)^computer\b")

    def test_plist_renders_for_another_checkout_and_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fresh clone ") as temporary:
            base = Path(temporary).resolve()
            root, home = base / "VaultLens checkout", base / "home" / "alice"
            config = base / "llm.local.json"
            source = plistlib.loads(PLIST.read_bytes())
            result = render_plist(
                source,
                {},
                config_path=config,
                python_executable=sys.executable,
                substitutions={
                    "@BRAIN_ROOT@": str(root),
                    "@BRAIN_HOME@": str(home),
                    "@BRAIN_USER@": "alice",
                },
            )
        self.assertEqual(
            result["ProgramArguments"],
            [sys.executable, f"{root}/tools/schedule/dispatch.py", "run"],
        )
        self.assertEqual(result["StandardOutPath"], f"{home}/.brain/logs/launchd.out.log")
        self.assertEqual(result["StandardErrorPath"], f"{home}/.brain/logs/launchd.err.log")
        self.assertNotIn("@BRAIN_", plistlib.dumps(result).decode())
        # The source template is not modified by rendering.
        self.assertIn("@BRAIN_ROOT@", str(source["ProgramArguments"]))

    def test_unresolved_placeholder_is_refused(self) -> None:
        source = plistlib.loads(PLIST.read_bytes())
        with self.assertRaisesRegex(ValueError, "Unresolved template placeholder"):
            render_plist(
                source,
                {},
                config_path=Path("/missing/llm.local.json"),
                substitutions={"@BRAIN_ROOT@": "/vault"},
            )

    def test_default_substitutions_name_this_checkout(self) -> None:
        values = default_substitutions()
        self.assertEqual(values["@BRAIN_ROOT@"], str(TOOLS.parent))
        self.assertEqual(values["@BRAIN_HOME@"], str(Path.home()))
        self.assertTrue(values["@BRAIN_USER@"])

    def test_rendered_plist_validates_against_its_own_checkout(self) -> None:
        source = plistlib.loads(PLIST.read_bytes())
        result = render_plist(
            source,
            {},
            python_executable=sys.executable,
            substitutions=default_substitutions(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "llm.local.json"
            config.write_text('{"cli":"claude"}')
            self.assertEqual(validate_plist(result, config_path=config), "claude")
        dispatcher = SCHEDULE / "dispatch.py"
        self.assertEqual(
            result["ProgramArguments"], [sys.executable, str(dispatcher), "run"]
        )
        self.assertTrue(dispatcher.is_file())

    def test_command_line_renders_for_the_calling_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            output = Path(temporary) / "out.plist"
            env = clean_environment()
            env["HOME"] = str(home)
            run = subprocess.run(
                [
                    sys.executable,
                    str(SCHEDULE / "render_plist.py"),
                    str(PLIST),
                    str(output),
                    "--python-executable",
                    sys.executable,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            result = plistlib.loads(output.read_bytes())
        self.assertEqual(result["StandardOutPath"], f"{home}/.brain/logs/launchd.out.log")
        self.assertEqual(
            result["ProgramArguments"][1],
            str(TOOLS.parent / "tools/schedule/dispatch.py"),
        )


class SudoersTests(unittest.TestCase):
    def test_rule_is_granted_to_the_named_account_only(self) -> None:
        text = render_sudoers(SUDOERS.read_text(encoding="utf-8"), "alice")
        rules = [
            line
            for line in text.splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(
            rules,
            [
                "alice ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 1, "
                "/usr/bin/pmset -a disablesleep 0, /usr/bin/pmset sleepnow"
            ],
        )
        self.assertNotIn("@BRAIN_", text)

    def test_hostile_or_privileged_names_are_refused(self) -> None:
        template = SUDOERS.read_text(encoding="utf-8")
        for user in ("", "root", "a b", "x\nALL ALL=(ALL) NOPASSWD: ALL", "A", "x,y", "1abc"):
            with self.subTest(user=user), self.assertRaises(ValueError):
                render_sudoers(template, user)

    def test_installer_renders_sudoers_and_checks_usage_before_python(self) -> None:
        installer = str(SCHEDULE / "install.sh")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "brain-schedule.sudoers"
            env = clean_environment()
            env["BRAIN_PYTHON"] = sys.executable
            env["LOGNAME"] = env["USER"] = "alice"  # getpass.getuser() reads these
            run = subprocess.run(
                ["bash", installer, "--render-sudoers", str(output)],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn("@BRAIN_", rendered)
            self.assertIn("\nalice ALL=(root) NOPASSWD:", rendered)
        # A bad mode must report usage even when the interpreter is also wrong.
        env["BRAIN_PYTHON"] = "/nonexistent/python3"
        bad = subprocess.run(
            ["bash", installer, "--bogus"], capture_output=True, text=True, env=env
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("usage:", bad.stderr)
        self.assertNotIn("BRAIN_PYTHON", bad.stderr)


if __name__ == "__main__":
    unittest.main()
