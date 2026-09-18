#!/usr/bin/env python3
"""Regression tests for the fixed VaultLens scaffold."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

import wiki  # noqa: E402
import wiki_init  # noqa: E402


class InitTests(unittest.TestCase):
    def test_init_creates_scaffold_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = wiki_init.initialize_vault(
                root, today=dt.date(2026, 9, 18)
            )

            for relative in wiki_init.REQUIRED_DIRECTORIES:
                self.assertTrue((root / relative).is_dir(), relative)
            self.assertIn(root / "wiki/log.md", created)
            self.assertIn("created: 2026-09-18", (root / "wiki/log.md").read_text())
            self.assertTrue((root / "wiki/index.md").is_file())

            marker = "local content\n"
            (root / "wiki/log.md").write_text(marker, encoding="utf-8")
            self.assertEqual(wiki_init.initialize_vault(root), [])
            self.assertEqual((root / "wiki/log.md").read_text(), marker)

    def test_required_empty_directories_are_tracked(self) -> None:
        for relative in wiki_init.REQUIRED_DIRECTORIES:
            placeholder = REPO_ROOT / relative / ".gitkeep"
            self.assertTrue(placeholder.is_file(), str(placeholder))
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "-q", str(placeholder)],
                cwd=REPO_ROOT,
                check=False,
            )
            self.assertEqual(result.returncode, 1, str(placeholder))

    def test_private_payloads_remain_ignored(self) -> None:
        payloads = [
            "raw/sources/private.pdf",
            "raw/sources-text/private.md",
            "raw/assets/private.png",
            "raw/inbox/private.pdf",
            "raw/review-inbox/private.url",
            "wiki/log/private.json",
        ]
        for relative in payloads:
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "-q", relative],
                cwd=REPO_ROOT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, relative)

    def test_operator_profile_is_an_optional_link_target(self) -> None:
        self.assertIn("entities/user-background", wiki.SPECIAL_LINK_TARGETS)
        home = (REPO_ROOT / "wiki/home.md").read_text(encoding="utf-8")
        self.assertIn("[[entities/user-background|Operator Profile]]", home)


if __name__ == "__main__":
    unittest.main()
