"""Local git gate regressions; run against a scratch repository only."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT = REPO_ROOT / ".githooks" / "pre-commit"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


class PreCommitTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]
    repo: Path

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / ".gitignore").write_text("private/\n", encoding="utf-8")
        (self.repo / "note.txt").write_text("public\n", encoding="utf-8")
        git(self.repo, "add", ".gitignore", "note.txt")
        self.assertEqual(git(self.repo, "commit", "-q", "-m", "init").returncode, 0)

    def run_hook(self) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(SKIP_TESTS="1", SKIP_WIKI_LINT="1", SKIP_WIKI_INDEX="1")
        env.pop("ALLOW_IGNORED_FILES", None)
        return subprocess.run(
            ["bash", str(PRE_COMMIT)],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_rename_into_ignored_path_is_blocked(self) -> None:
        # A rename is not an add, so a filter of ACM let this through unchecked.
        (self.repo / "private").mkdir()
        moved = git(self.repo, "mv", "note.txt", "private/note.txt")
        self.assertEqual(moved.returncode, 0, moved.stderr)
        status = git(self.repo, "diff", "--cached", "--name-status").stdout
        self.assertTrue(status.startswith("R"), status)
        result = self.run_hook()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("private/note.txt", result.stderr)

    def test_clean_change_passes(self) -> None:
        (self.repo / "note.txt").write_text("still public\n", encoding="utf-8")
        git(self.repo, "add", "note.txt")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
