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
from scoped_search import (  # noqa: E402
    DEFAULT_SNIPPET_CHARS,
    MAX_BRIDGE_MESSAGE_BYTES,
    MAX_DOCUMENT_BYTES,
    MAX_GET_CHARS,
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    QmdBridge,
    ScopedSearch,
    bridge_query,
    load_bridge,
    load_scope,
    serve_mcp,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scoped_search.py"


class VaultCase(unittest.TestCase):
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


class ScopedSearchTests(VaultCase):
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
        with self.assertRaisesRegex(ValueError, "raw/review-inbox/consent.md"):
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

    def test_rare_terms_outrank_common_words_in_natural_language_queries(
        self,
    ) -> None:
        self.write(
            "wiki/concepts/scheduler.md",
            "# Scheduler retries\nThe dispatcher backs off retries after a quota error.\n",
        )
        for index in range(5):
            self.write(
                f"wiki/concepts/filler{index}.md",
                f"# Filler {index}\nHow does the operator handle this? What it does.\n",
            )
        payload = self.search.search({"query": "how does the scheduler handle retries"})
        self.assertEqual(payload["results"][0]["file"], "wiki/concepts/scheduler.md")
        self.assertTrue(payload["results"][0]["snippet"].startswith("# Scheduler"))

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

    def test_search_covers_every_document_in_a_large_vault(self) -> None:
        # The old 4,096-document cap silently skipped later paths.
        for index in range(4200):
            self.write(f"wiki/bulk/{index:05d}.md", f"bulk note {index}")
        self.write("wiki/zz-last/final.md", "Approved last-path corpus marker.")
        payload = self.search.search({"query": "last-path"})
        self.assertEqual(
            [result["file"] for result in payload["results"]],
            ["wiki/zz-last/final.md"],
        )
        self.assertEqual(payload["skipped_documents"], 0)
        self.assertGreater(self.search.status()["documents"], 4200)

    def test_cached_approval_still_refuses_later_link_swaps(self) -> None:
        self.assertEqual(len(self.search.search({"query": "alpha"})["results"]), 1)
        self.note.unlink()
        self.note.symlink_to(self.outside / "global-index.md")
        self.assertEqual(self.search.search({"query": "Denied-index"})["results"], [])
        self.note.unlink()
        secret = self.root / "wiki/private/secret.md"
        os.link(secret, self.note)
        self.assertEqual(
            self.search.search({"query": "Denied-private"})["results"], []
        )
        self.write("wiki/concepts/new.md", "Approved fresh-file corpus marker.")
        self.assertEqual(
            len(self.search.search({"query": "fresh-file"})["results"]), 1
        )

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


class QmdBridgeTests(VaultCase):
    """The host bridge passes back approved paths only, and never qmd's text."""

    host: Path
    run_dir: Path
    bridge: QmdBridge

    def setUp(self) -> None:
        super().setUp()
        temporary = Path(self.temporary.name).resolve()
        self.host = temporary / "host"
        self.host.mkdir()
        fake = self.host / "qmd"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "here = Path(__file__).parent\n"
            "(here / 'call.json').write_text(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n"
            "sys.stdout.write((here / 'output.json').read_text())\n"
            "status = here / 'status'\n"
            "sys.exit(int(status.read_text()) if status.exists() else 0)\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        self.run_dir = temporary / "run"
        self.run_dir.mkdir()
        self.bridge = QmdBridge(self.scope, QmdBridge.prepare(self.run_dir), fake, timeout=10)
        alias = temporary / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        (self.root / "wiki/concepts/linked.md").symlink_to(
            self.outside / "global-index.md"
        )
        self.qmd_output(
            [
                {"file": str(self.root / "wiki/private/secret.md"), "score": 0.99},
                {"file": str(self.root / "raw/review-inbox/consent.md"), "score": 0.98},
                {"file": str(self.root / "projects/sibling/notes/other.md"), "score": 0.97},
                {"file": str(self.root / "wiki/concepts/AGENTS.md"), "score": 0.96},
                {"file": str(self.root / "wiki/concepts/credentials.txt"), "score": 0.95},
                {"file": str(self.root / "wiki/concepts/linked.md"), "score": 0.94},
                {"file": str(self.outside / "global-index.md"), "score": 0.93},
                {"file": str(self.root / "wiki/concepts/../private/secret.md"), "score": 0.92},
                {"file": "wiki/concepts/current.md", "score": 0.91},
                {"file": "qmd://wiki/concepts/current.md", "score": 0.9},
                "not an object",
                {
                    "file": str(alias / "wiki/concepts/current.md"),
                    "score": 0.8,
                    "snippet": "QMD-SNIPPET-LEAK",
                    "title": "QMD-TITLE-LEAK",
                },
                {"file": str(self.root / "wiki/concepts/current.md"), "score": 0.7},
                {"file": str(self.root / "raw/sources/approved.txt"), "score": 0.6},
            ]
        )

    def qmd_output(self, items: list[object]) -> None:
        (self.host / "output.json").write_text(json.dumps(items), encoding="utf-8")

    def test_host_returns_only_approved_paths_without_qmd_text(self) -> None:
        results = self.bridge.query("alpha", 10)
        self.assertEqual(
            results,
            [
                {"path": "wiki/concepts/current.md", "score": 0.8},
                {"path": "raw/sources/approved.txt", "score": 0.6},
            ],
        )
        call = json.loads((self.host / "call.json").read_text())
        # Run from "/" so no vault-local qmd configuration applies.
        self.assertEqual(call["cwd"], "/")
        self.assertEqual(call["argv"][:4], ["query", "--format", "json", "--full-path"])

    def test_query_text_is_never_read_as_an_option(self) -> None:
        self.bridge.query("--index /elsewhere", 1)
        argv = json.loads((self.host / "call.json").read_text())["argv"]
        self.assertEqual(argv[-2:], ["--", "--index /elsewhere"])

    def test_round_trip_rereads_files_inside_the_run(self) -> None:
        self.bridge.start()
        self.addCleanup(self.bridge.stop)
        search = ScopedSearch(self.scope, self.bridge.directory)
        payload = search.call("query", {"query": "alpha", "limit": 5})
        serialized = json.dumps(payload)
        self.assertEqual(payload["mode"], "qmd")
        self.assertNotIn("fallback", payload)
        self.assertEqual(
            [result["file"] for result in payload["results"]],
            ["wiki/concepts/current.md", "raw/sources/approved.txt"],
        )
        self.assertIn("Approved alpha corpus marker", serialized)
        self.assertNotIn("LEAK", serialized)
        self.assertNotIn("Denied-", serialized)
        self.assertEqual(search.status()["query_mode"], "qmd")

    def test_malformed_oversized_and_linked_requests_get_errors(self) -> None:
        requests = self.bridge.directory / "requests"
        responses = self.bridge.directory / "responses"
        names = [f"{index:032x}.json" for index in range(6)]
        (requests / names[0]).write_text("not json")
        (requests / names[1]).write_bytes(b" " * (MAX_BRIDGE_MESSAGE_BYTES + 1))
        (requests / names[2]).symlink_to(self.root / "wiki/concepts/current.md")
        (requests / names[3]).mkdir()
        (requests / names[4]).write_text(json.dumps({"query": "alpha", "limit": 0}))
        (requests / names[5]).write_text(json.dumps({"query": ""}))
        (requests / "ignored.json").write_text(json.dumps({"query": "alpha"}))
        self.assertEqual(self.bridge.handle_pending(limit=10), 6)
        for name in names:
            with self.subTest(name=name):
                answer = json.loads((responses / name).read_text())
                self.assertEqual(answer["status"], "error")
        # A request that cannot be removed is answered once, not on every pass.
        self.assertEqual(self.bridge.handle_pending(), 0)
        self.assertFalse((responses / "ignored.json").exists())
        self.assertFalse((self.host / "call.json").exists())

    def test_qmd_failure_and_timeout_fall_back_to_lexical(self) -> None:
        (self.host / "status").write_text("3")
        self.assertEqual(
            self.bridge.answer(json.dumps({"query": "alpha"}).encode())["status"],
            "error",
        )
        with self.assertRaisesRegex(ValueError, "timed out"):
            bridge_query(self.bridge.directory, "alpha", 1, timeout=0.1)
        search = ScopedSearch(self.scope, self.bridge.directory)
        with patch(
            "scoped_search.bridge_query",
            side_effect=ValueError("qmd bridge timed out"),
        ):
            payload = search.query({"query": "alpha"})
        self.assertEqual(payload["mode"], "lexical")
        self.assertEqual(payload["fallback"], "qmd bridge timed out")
        self.assertEqual(payload["results"][0]["file"], "wiki/concepts/current.md")
        plain = ScopedSearch(self.scope).query({"query": "alpha"})
        self.assertEqual(plain["fallback"], "qmd is not enabled for this run")
        self.assertEqual(ScopedSearch(self.scope).status()["query_mode"], "lexical")

    def test_manifest_bridge_must_be_the_runs_own_folder(self) -> None:
        manifest = self.scope.manifest()
        path = self.run_dir / "scope.json"
        path.write_text(json.dumps(manifest))
        self.assertIsNone(load_bridge(path))
        manifest["qmd_bridge"] = str(self.bridge.directory)
        path.write_text(json.dumps(manifest))
        self.assertEqual(load_bridge(path), self.bridge.directory)
        elsewhere = self.host / "qmd-bridge"
        elsewhere.mkdir()
        alias = self.run_dir / "alias"
        alias.symlink_to(self.bridge.directory, target_is_directory=True)
        for value in (str(elsewhere), str(self.run_dir / "requests"), "qmd-bridge", 3):
            with self.subTest(value=value):
                manifest["qmd_bridge"] = value
                path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    load_bridge(path)


if __name__ == "__main__":
    unittest.main()
