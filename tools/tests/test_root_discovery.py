"""Exercise the tracked fish root helper against isolated checkout fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "shell" / "__brain_project_root.fish"
FISH = shutil.which("fish")


@unittest.skipUnless(FISH, "fish required for root discovery tests")
class RootDiscoveryTests(unittest.TestCase):
    fixture: tempfile.TemporaryDirectory[str]
    root: Path
    fallback: Path
    unrelated: Path

    def setUp(self) -> None:
        self.fixture = tempfile.TemporaryDirectory(prefix="brain root ")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name).resolve()
        self.fallback = self.checkout("configured fallback")
        self.unrelated = self.root / "unrelated"
        self.unrelated.mkdir()

    def checkout(
        self,
        name: str,
        *,
        missing: str | None = None,
    ) -> Path:
        root = self.root / name
        root.mkdir(parents=True)
        for marker in ("AGENTS.md", "tools/wiki.py", "tools/agents/wiki-agent.py"):
            path = root / marker
            path.parent.mkdir(parents=True, exist_ok=True)
            if marker != missing:
                # Root discovery only validates these files; it never executes them.
                path.write_text("raise RuntimeError('must not execute')\n")
        return root

    def resolve(
        self, cwd: Path, *, fallback: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert FISH is not None  # class is skipped without fish
        return subprocess.run(
            [
                FISH,
                "--no-config",
                "-c",
                f"source {shlex.quote(str(HELPER))}; __brain_project_root",
            ],
            cwd=cwd,
            env={"PATH": os.defpath, "BRAIN_HOME": str(fallback or self.fallback)},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def assert_root(self, cwd: Path, expected: Path) -> None:
        result = self.resolve(cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{expected}\n")
        self.assertEqual(result.stderr, "")

    def test_complete_vault_checkouts_override_fallback(self) -> None:
        for name in ("Brain", "VaultLens", "another vault"):
            with self.subTest(name=name):
                project = self.checkout(name)
                nested = project / "projects" / "two words" / "notes"
                nested.mkdir(parents=True)
                self.assert_root(nested, project)

    def test_each_marker_is_required(self) -> None:
        for marker in ("AGENTS.md", "tools/wiki.py", "tools/agents/wiki-agent.py"):
            with self.subTest(marker=marker):
                project = self.checkout(
                    f"missing {marker.replace('/', '-')}", missing=marker
                )
                self.assert_root(project, self.fallback)

    def test_marker_directories_are_rejected(self) -> None:
        for marker in ("AGENTS.md", "tools/wiki.py", "tools/agents/wiki-agent.py"):
            with self.subTest(marker=marker):
                project = self.checkout(
                    f"directory {marker.replace('/', '-')}", missing=marker
                )
                (project / marker).mkdir()
                self.assert_root(project, self.fallback)

    def test_nearest_complete_vault_wins(self) -> None:
        outer = self.checkout("outer vault")
        inner = self.checkout("outer vault/nested vault")
        self.assert_root(inner, inner)
        partial = self.checkout("outer vault/partial vault", missing="tools/wiki.py")
        self.assert_root(partial, outer)

    def test_explicit_fallback_requires_all_markers(self) -> None:
        for marker in ("AGENTS.md", "tools/wiki.py", "tools/agents/wiki-agent.py"):
            with self.subTest(marker=marker):
                fallback = self.checkout(
                    f"fallback {marker.replace('/', '-')}", missing=marker
                )
                result = self.resolve(self.unrelated, fallback=fallback)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("vault markers missing", result.stderr)
                self.assertIn("set BRAIN_HOME", result.stderr)

    def test_vault_and_fallback_do_not_require_container_files(self) -> None:
        self.assertFalse((self.fallback / ".devcontainer").exists())
        self.assert_root(self.fallback, self.fallback)
        self.assert_root(self.unrelated, self.fallback)


if __name__ == "__main__":
    unittest.main()
