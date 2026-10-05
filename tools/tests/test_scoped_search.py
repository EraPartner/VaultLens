#!/usr/bin/env python3
"""Behavioral privacy checks for the explicit lexical search boundary."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_access import RunScope  # noqa: E402
import scoped_search  # noqa: E402
from scoped_search import (  # noqa: E402
    DEFAULT_SNIPPET_CHARS,
    MAX_DOCUMENT_BYTES,
    MAX_GET_CHARS,
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    ScopedSearch,
    load_scope,
    serve_mcp,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scoped_search.py"


class ScopedSearchTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]
    root: Path
    note: Path
    outside: Path
    scope: RunScope
    search: ScopedSearch
    manifest: Path

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "vault"
        self.root.mkdir()
        self.note = self.write(
            "wiki/concepts/current.md",
            "# Current note\nApproved alpha corpus marker.\n",
        )
        self.write("wiki/private/secret.md", "Denied-private corpus marker.")
        self.write("raw/review-inbox/consent.md", "Denied-review corpus marker.")
        self.write("raw/sources/approved.txt", "Approved-source corpus marker.")
        self.write("projects/one/notes/selected.md", "Approved-project corpus marker.")
        self.write("projects/sibling/notes/other.md", "Denied-sibling corpus marker.")
        self.write("wiki/concepts/credentials.txt", "Denied-credentials corpus marker.")
        self.write("wiki/concepts/.env.md", "Denied-env corpus marker.")
        self.write("wiki/concepts/.git/metadata.md", "Denied-git corpus marker.")
        self.write("wiki/concepts/AGENTS.md", "Denied-instructions corpus marker.")
        self.outside = Path(self.temporary.name).resolve() / "outside"
        self.outside.mkdir()
        (self.outside / "global-index.md").write_text(
            "Denied-index corpus marker.", encoding="utf-8"
        )
        self.scope = RunScope(
            self.root,
            "selected",
            (self.root / "wiki", self.root / "raw", self.root / "projects/one"),
            (),
            (self.root / "wiki/private",),
            (),
            self.root / "wiki/reports/runtime",
        )
        self.search = ScopedSearch(self.scope)
        self.manifest = Path(self.temporary.name).resolve() / "scope.json"
        self.manifest.write_text(json.dumps(self.scope.manifest()), encoding="utf-8")

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_search_results_contain_only_approved_current_documents(self) -> None:
        payload = self.search.search({"query": "corpus marker", "limit": 50})
        serialized = json.dumps(payload)
        self.assertIn("Approved alpha", serialized)
        self.assertIn("Approved-source", serialized)
        self.assertIn("Approved-project", serialized)
        self.assertNotIn("Denied-", serialized)
        self.assertEqual(
            {result["file"] for result in payload["results"]},
            {
                "wiki/concepts/current.md",
                "raw/sources/approved.txt",
                "projects/one/notes/selected.md",
            },
        )
        self.assertEqual(payload["mode"], "lexical")

    def test_get_blocks_denied_review_sibling_secret_and_traversal_paths(self) -> None:
        excluded = [
            "wiki/private/secret.md",
            "raw/review-inbox/consent.md",
            "projects/sibling/notes/other.md",
            "wiki/concepts/credentials.txt",
            "wiki/concepts/.env.md",
            "wiki/concepts/.git/metadata.md",
            "wiki/concepts/AGENTS.md",
            "../outside/global-index.md",
            str(self.outside / "global-index.md"),
            "wiki/../private/secret.md",
        ]
        for path in excluded:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.search.get({"path": path})
        selected = self.search.get({"docid": "qmd://scope/wiki/concepts/current.md"})
        self.assertEqual(selected["text"], self.note.read_text(encoding="utf-8"))

    def test_file_and_directory_symlinks_cannot_disclose_excluded_content(self) -> None:
        file_link = self.root / "wiki/concepts/linked.md"
        file_link.symlink_to(self.outside / "global-index.md")
        directory_link = self.root / "wiki/concepts/linked-directory"
        directory_link.symlink_to(self.outside, target_is_directory=True)
        private_link = self.root / "wiki/concepts/private-alias.md"
        private_link.symlink_to(self.root / "wiki/private/secret.md")
        self.assertEqual(self.search.search({"query": "Denied-index"})["results"], [])
        for path in (file_link, directory_link / "global-index.md", private_link):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.search.get({"path": str(path)})

    def test_descriptor_walk_rejects_symlink_swapped_after_permission_check(
        self,
    ) -> None:
        original_readable = self.scope.readable
        swapped = False

        def readable(path: Path) -> bool:
            nonlocal swapped
            approved = original_readable(path)
            if path == self.note and not swapped:
                self.note.unlink()
                self.note.symlink_to(self.outside / "global-index.md")
                swapped = True
            return approved

        with (
            patch.object(RunScope, "readable", side_effect=readable),
            self.assertRaises(ValueError),
        ):
            self.search.get({"path": str(self.note)})

    def test_current_edits_are_visible_without_cached_index(self) -> None:
        self.assertEqual(len(self.search.search({"query": "alpha"})["results"]), 1)
        self.note.write_text("# Changed\nFresh beta corpus marker.", encoding="utf-8")
        self.assertEqual(self.search.search({"query": "alpha"})["results"], [])
        self.assertEqual(len(self.search.search({"query": "beta"})["results"]), 1)
        self.assertIn(
            "Fresh beta", self.search.get({"file": "wiki/concepts/current.md"})["text"]
        )
        status = self.search.status()
        self.assertFalse(status["persistent_index"])
        self.assertFalse(status["embeddings"])

    def test_batched_query_and_multi_get_enforce_limits_and_selection(self) -> None:
        payload = self.search.call(
            "query",
            {
                "searches": [
                    {"type": "lex", "query": "alpha"},
                    {"query": "Approved-source"},
                ],
                "limit": 2,
            },
        )
        self.assertEqual(len(payload["results"]), 2)
        documents = self.search.call(
            "multi_get",
            {"paths": ["wiki/concepts/current.md", "raw/sources/approved.txt"]},
        )
        self.assertEqual(len(documents["documents"]), 2)
        with self.assertRaises(ValueError):
            self.search.multi_get(
                {"paths": ["wiki/concepts/current.md", "raw/review-inbox/consent.md"]}
            )
        for args in (
            {"query": "alpha", "limit": MAX_RESULTS + 1},
            {"query": ""},
            {"query": "x" * 4097},
            {"query": "alpha", "limit": True},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.search.search(args)

    def test_oversized_and_nonregular_documents_are_never_read(self) -> None:
        self.write(
            "wiki/concepts/large.md", "Denied-oversize " + "x" * MAX_DOCUMENT_BYTES
        )
        fifo = self.root / "wiki/concepts/pipe.md"
        os.mkfifo(fifo)
        payload = self.search.search({"query": "Denied-oversize"})
        self.assertEqual(payload["results"], [])
        self.assertGreaterEqual(payload["skipped_documents"], 1)
        for path in ("wiki/concepts/large.md", "wiki/concepts/pipe.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.search.get({"path": path})
        self.note.write_text("x" * (MAX_GET_CHARS + 100), encoding="utf-8")
        result = self.search.get({"path": "wiki/concepts/current.md"})
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["text"]), MAX_GET_CHARS)

    def test_snippets_are_bounded_and_adjustable(self) -> None:
        self.note.write_text(
            "# Current note\n" + "filler " * 100 + "alpha " + "tail " * 500,
            encoding="utf-8",
        )
        result = self.search.search({"query": "alpha"})["results"][0]
        self.assertEqual(len(result["snippet"]), DEFAULT_SNIPPET_CHARS)
        self.assertIn("alpha", result["snippet"])
        wide = self.search.search({"query": "alpha", "snippet_chars": MAX_SNIPPET_CHARS})
        self.assertEqual(len(wide["results"][0]["snippet"]), MAX_SNIPPET_CHARS)
        for value in (0, MAX_SNIPPET_CHARS + 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.search.search({"query": "alpha", "snippet_chars": value})

    def test_document_cap_names_unsearched_directories(self) -> None:
        payload = self.search.search({"query": "marker"})
        self.assertNotIn("unsearched_documents", payload)
        total = len(self.scope.document_paths())
        with patch.object(scoped_search, "MAX_DOCUMENTS", 1):
            payload = self.search.search({"query": "marker"})
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["unsearched_documents"], total - 1)
        self.assertIn("wiki/concepts", payload["unsearched_directories"])

    def test_manifest_is_explicit_and_rejects_external_selections(self) -> None:
        with patch.dict(
            os.environ,
            {"VAULTLENS_RUNTIME_MANIFEST": str(self.outside / "missing.json")},
        ):
            self.assertEqual(
                load_scope(self.manifest).read_paths, self.scope.read_paths
            )
        data = self.scope.manifest()
        data["read"].append(str(self.outside))
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_scope(self.manifest)
        with self.assertRaises(ValueError):
            load_scope(Path("relative.json"))

    def test_mcp_initialization_tools_notifications_and_calls(self) -> None:
        requests: list[dict[str, object]] = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "query", "arguments": {"query": "alpha"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "get",
                    "arguments": {"path": "raw/review-inbox/consent.md"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            },
        ]
        stream = io.StringIO()
        serve_mcp(
            self.search,
            io.StringIO("".join(json.dumps(request) + "\n" for request in requests)),
            stream,
        )
        responses = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4, 5])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "qmd")
        self.assertEqual(
            {tool["name"] for tool in responses[1]["result"]["tools"]},
            {"query", "search", "get", "multi_get", "status"},
        )
        self.assertEqual(
            responses[2]["result"]["structuredContent"]["results"][0]["file"],
            "wiki/concepts/current.md",
        )
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertNotIn("Denied-review", stream.getvalue())
        self.assertFalse(responses[4]["result"]["structuredContent"]["embeddings"])

    def test_subprocess_cli_and_stdio_use_only_the_explicit_manifest(self) -> None:
        for operation, flags in (
            ("search", ["--json"]),
            ("query", ["--format", "json"]),
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--manifest",
                    str(self.manifest),
                    operation,
                    "corpus marker",
                    *flags,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Denied-", result.stdout)
            self.assertEqual(len(json.loads(result.stdout)["results"]), 3)
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "status", "arguments": {}},
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--manifest", str(self.manifest), "mcp"],
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], 7)
        self.assertFalse(
            json.loads(result.stdout)["result"]["structuredContent"]["persistent_index"]
        )


if __name__ == "__main__":
    unittest.main()
