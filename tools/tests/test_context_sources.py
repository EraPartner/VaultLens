"""Inbox preview safety: no links, no hard-link aliases, bounded reads."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import context_sources  # noqa: E402


class InboxPreviewTests(unittest.TestCase):
    root: Path
    inbox: Path

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="vaultlens-preview-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.inbox = self.root / "raw/inbox"
        self.inbox.mkdir(parents=True)
        (self.root / "raw/review-inbox").mkdir()

    def test_regular_inbox_file_is_previewed(self) -> None:
        note = self.inbox / "public.md"
        note.write_text("Public synthetic fixture.\n", encoding="utf-8")
        self.assertEqual(
            context_sources.read_inbox_preview(self.root, note),
            "Public synthetic fixture.\n",
        )

    def test_hard_and_symbolic_links_into_the_consent_queue_are_refused(self) -> None:
        private = self.root / "raw/review-inbox/consent.md"
        private.write_text("CONSENT_REQUIRED_SENTINEL\n", encoding="utf-8")
        hard = self.inbox / "hard.md"
        os.link(private, hard)
        soft = self.inbox / "soft.md"
        soft.symlink_to(private)
        for alias in (hard, soft):
            with self.subTest(alias=alias.name):
                self.assertIsNone(context_sources.read_inbox_preview(self.root, alias))

    def test_preview_read_is_bounded(self) -> None:
        note = self.inbox / "large.md"
        note.write_text("x" * (context_sources.MAX_PREVIEW_CHARACTERS + 10), encoding="utf-8")
        preview = context_sources.read_inbox_preview(self.root, note)
        self.assertIsNotNone(preview)
        self.assertEqual(len(preview or ""), context_sources.MAX_PREVIEW_CHARACTERS)

    def test_paths_outside_the_inbox_are_refused(self) -> None:
        private = self.root / "raw/review-inbox/consent.md"
        private.write_text("CONSENT_REQUIRED_SENTINEL\n", encoding="utf-8")
        self.assertIsNone(context_sources.read_inbox_preview(self.root, private))


if __name__ == "__main__":
    unittest.main()
