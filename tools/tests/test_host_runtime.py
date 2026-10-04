"""Reject unsupported host Python before commands can change local state."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]


class HostRuntimeTests(unittest.TestCase):
    def test_old_python_fails_cleanly_before_host_commands(self):
        for relative, arguments in (
            ("llm_provider.py", ["select", "claude"]),
            ("brain_launch.py", ["agent"]),
            ("agents/wiki-agent.py", ["search", "--debug"]),
            ("agents/generate-adapters.py", ["--check", "--provider", "codex"]),
            ("schedule/dispatch.py", ["status"]),
            ("wiki.py", ["init"]),
        ):
            with (
                self.subTest(command=relative),
                tempfile.TemporaryDirectory() as directory,
            ):
                # Simulate the unsupported interpreter at startup without relying
                # on macOS system Python or executing any command implementation.
                program = (
                    "import runpy, sys; "
                    "sys.version_info = (3, 9, 6); "
                    f"sys.path.insert(0, {str(TOOLS)!r}); "
                    f"sys.argv = {[relative, *arguments]!r}; "
                    f"runpy.run_path({str(TOOLS / relative)!r}, run_name='__main__')"
                )
                result = subprocess.run(
                    [sys.executable, "-c", program],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Python 3.11 or newer", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
