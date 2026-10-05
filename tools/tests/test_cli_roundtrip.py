#!/usr/bin/env python3
"""End-to-end wiki CLI round trips: what one command writes, the next must read back."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import wiki_init  # noqa: E402

PAGE = (
    "---\ntitle: {title}\ntype: concept\nstatus: {status}\ncreated: 2026-10-01\n"
    "updated: 2026-10-01\nsummary: Fixture.\n---\n\n{body}\n"
)


class CliRoundTripTests(unittest.TestCase):
    root: Path

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(
            TOOLS_DIR,
            self.root / "tools",
            ignore=shutil.ignore_patterns("__pycache__", "tests"),
        )
        wiki_init.initialize_vault(self.root, today=dt.date(2026, 10, 1))
        (self.root / "projects").mkdir(exist_ok=True)

    def wiki(self, *args: str, path_prefix: str = "") -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(self.root / "home")}
        env.pop("BRAIN_PYTHON", None)
        if path_prefix:
            env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            [sys.executable, str(self.root / "tools" / "wiki.py"), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )

    def page(self, rel: str, body: str, *, title: str = "Fixture", status: str = "active") -> Path:
        path = self.root / "wiki" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PAGE.format(title=title, status=status, body=body), encoding="utf-8")
        return path

    def test_project_link_keeps_refs_with_spaces_whole(self) -> None:
        self.assertEqual(self.wiki("project", "new", "demo").returncode, 0)
        self.assertEqual(self.wiki("project", "link", "demo", "concepts/my page").returncode, 0)
        self.assertEqual(self.wiki("project", "link", "demo", "home").returncode, 0)
        shown = self.wiki("project", "show", "demo").stdout
        self.assertIn("[[concepts/my page]]", shown)
        self.assertNotIn("[[page]]", shown)

    def test_project_views_rebuild_with_the_running_interpreter(self) -> None:
        # A broken `python3` earlier on PATH must not half-create the project.
        shim = self.root / "shim"
        shim.mkdir()
        (shim / "python3").write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        (shim / "python3").chmod(0o755)
        result = self.wiki("project", "new", "shimtest", path_prefix=str(shim))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shimtest", (self.root / "projects" / "TODO.md").read_text(encoding="utf-8"))

    def test_project_new_rejects_slugs_the_runtime_cannot_launch(self) -> None:
        for slug in ("my.project", "_scratch"):
            with self.subTest(slug=slug):
                result = self.wiki("project", "new", slug)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertFalse((self.root / "projects" / slug).exists())

    def test_agenda_commands_on_unknown_project_fail_cleanly(self) -> None:
        for args in (("complete", "nope", "T1"), ("resolve", "nope", "T1"), ("new-id", "nope"), ("ack", "nope")):
            with self.subTest(args=args):
                result = self.wiki("project", "agenda", *args)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("No AGENDA.md", result.stdout)

    def test_links_fix_is_idempotent_for_parentheses_and_spaces(self) -> None:
        self.page("concepts/foo (bar).md", "Body.", title="Foo Bar")
        self.page("concepts/my page.md", "Body.", title="My Page")
        linker = self.page(
            "concepts/linker.md",
            "See [[concepts/foo (bar)]] and [[concepts/my page]].\n\n"
            "Legacy: [[concepts/foo (bar)]] ([Foo Bar](foo (bar).md)).",
        )
        self.assertEqual(self.wiki("links", "--fix", "--write").returncode, 0)
        first = linker.read_text(encoding="utf-8")
        self.assertIn("([Foo Bar](<foo (bar).md>))", first)
        self.assertEqual(first.count("([Foo Bar]"), 2, first)
        self.assertIn("([My Page](<my page.md>))", first)
        self.wiki("links", "--fix", "--write")
        self.assertEqual(linker.read_text(encoding="utf-8"), first)

    def test_append_log_keeps_each_same_day_note(self) -> None:
        for summary in ("first run", "second run"):
            result = self.wiki(
                "append-log", "--operation", "lint", "--title", "Weekly lint", "--summary", summary
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        notes = sorted((self.root / "wiki" / "log").glob("*-lint-weekly-lint*.md"))
        self.assertEqual(len(notes), 2, [n.name for n in notes])
        bodies = "".join(n.read_text(encoding="utf-8") for n in notes)
        self.assertIn("first run", bodies)
        self.assertIn("second run", bodies)

    def test_inventory_show_accepts_the_path_new_prints(self) -> None:
        created = self.wiki("inventory", "new", "question", "how-x-works")
        self.assertEqual(created.returncode, 0, created.stdout)
        self.assertEqual(self.wiki("inventory", "show", "inventory/question/how-x-works.md").returncode, 0)

    def test_coverage_skips_archived_pages(self) -> None:
        self.page("concepts/retired.md", "Short.", status="archived")
        self.page("concepts/live.md", "Short.")
        out = self.wiki("coverage").stdout
        self.assertIn("concepts/live.md", out)
        self.assertNotIn("concepts/retired.md", out)

    def test_lint_limit_hides_sections_with_no_rows_left(self) -> None:
        for name in ("a", "b", "c"):
            self.page(f"concepts/{name}.md", f"Links to [[concepts/missing-{name}]].")
        out = self.wiki("lint", "--limit", "1").stdout
        headers = [line for line in out.splitlines() if line.endswith(":") and not line.startswith("-")]
        lines = out.splitlines()
        for header in headers:
            index = lines.index(header)
            following = lines[index + 1] if index + 1 < len(lines) else ""
            if header in ("Errors:", "Warnings:"):
                continue
            self.assertTrue(following.startswith("- "), f"empty section {header!r} in:\n{out}")


if __name__ == "__main__":
    unittest.main()
