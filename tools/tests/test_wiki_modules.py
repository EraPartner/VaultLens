#!/usr/bin/env python3
"""Focused regression tests for ingest, archive, and inventory modules."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wiki  # noqa: E402
import wiki_archive  # noqa: E402
import wiki_ingest  # noqa: E402
import wiki_inventory  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def write_page(root: Path, rel: str, *, status: str = "active") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {path.stem}\n"
        "type: concept\n"
        f"status: {status}\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "summary: Fixture.\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    return path


def test_ingest() -> None:
    print("wiki_ingest:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf = root / "paper.pdf"
        pdf.write_bytes(b"pdf")
        text_dir = root / "text"

        def extract(command, **kwargs):
            check("PDF subprocess has a timeout", kwargs.get("timeout", 0) > 0)
            Path(command[-1]).write_text("extracted words", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(wiki_ingest, "RAW_SOURCES_TEXT_DIR", text_dir),
            patch.object(wiki_ingest.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(wiki_ingest.subprocess, "run", side_effect=extract),
        ):
            path, status = wiki_ingest.extract_pdf_to_markdown(pdf)
        check(
            "plain PDF extraction succeeds",
            status is wiki_ingest.ExtractStatus.EXTRACTED,
        )
        check("extracted text is written", "extracted words" in path.read_text())
        check(
            "raw extraction file is cleaned", not path.with_suffix(".raw.txt").exists()
        )

        permission_error = "Copying of text from this document is not allowed"
        calls = 0

        def decrypt(command, **kwargs):
            nonlocal calls
            calls += 1
            check(
                f"decryption call {calls} has a timeout", kwargs.get("timeout", 0) > 0
            )
            if calls == 1:
                return subprocess.CompletedProcess(command, 1, "", permission_error)
            if command[0] == "qpdf":
                Path(command[-1]).write_bytes(b"decrypted")
            else:
                Path(command[-1]).write_text("decrypted words", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(wiki_ingest, "RAW_SOURCES_TEXT_DIR", text_dir),
            patch.object(wiki_ingest.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(wiki_ingest.subprocess, "run", side_effect=decrypt),
        ):
            path, status = wiki_ingest.extract_pdf_to_markdown(pdf, force=True)
        check(
            "copy-protected PDF uses qpdf fallback",
            status is wiki_ingest.ExtractStatus.DECRYPTED,
        )
        check("qpdf fallback performs three bounded calls", calls == 3, str(calls))
        check(
            "decrypted temporary file is cleaned",
            not path.with_suffix(".decrypted.pdf").exists(),
        )

        with (
            patch.object(wiki_ingest, "RAW_SOURCES_TEXT_DIR", text_dir),
            patch.object(wiki_ingest.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(
                wiki_ingest.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["pdftotext"], 1),
            ),
        ):
            try:
                wiki_ingest.extract_pdf_to_markdown(pdf, force=True)
            except RuntimeError as exc:
                timed_out = "timed out" in str(exc)
            else:
                timed_out = False
        check("PDF timeout becomes a clear runtime error", timed_out)


def test_archive_reconciliation() -> None:
    print("wiki_archive:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        write_page(root, "concepts/both.md", status="archived")
        write_page(root, "concepts/disk-only.md", status="archived")
        registry = root / "system" / "archive-registry.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            json.dumps(
                {
                    "archived": {
                        "concepts/both": {
                            "archived_on": "2026-01-01",
                            "reason": "done",
                        },
                        "concepts/registry-only": {
                            "archived_on": "2026-01-02",
                            "reason": "stale",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        output = io.StringIO()
        with (
            patch.object(wiki, "WIKI_DIR", root),
            patch.object(wiki_archive, "WIKI_DIR", root),
            patch.object(wiki_archive, "REGISTRY_PATH", registry),
            contextlib.redirect_stdout(output),
        ):
            rc = wiki_archive.list_archived(as_json=True)
        rows = {row["page"]: row for row in json.loads(output.getvalue())}
        check("archive reconciliation returns success", rc == 0)
        check(
            "archive reconciliation reports the full union", len(rows) == 3, str(rows)
        )
        check(
            "archive reconciliation flags both drift directions",
            rows["concepts/disk-only"]
            == {
                "page": "concepts/disk-only",
                "archived_on": "",
                "reason": "",
                "in_registry": False,
                "status_archived": True,
            }
            and rows["concepts/registry-only"]["in_registry"]
            and not rows["concepts/registry-only"]["status_archived"],
            str(rows),
        )


def test_inventory() -> None:
    print("wiki_inventory:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        inventory = root / "inventory"
        outside = root / "concepts" / "secret.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("SECRET", encoding="utf-8")
        with (
            patch.object(wiki, "WIKI_DIR", root),
            patch.object(wiki_inventory, "INVENTORY_DIR", inventory),
        ):
            created = wiki_inventory.inventory_new(
                "question", "safe", "Safe", "proposed", "p2", "A fixture"
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                shown = wiki_inventory.inventory_show("question/safe", as_json=True)
            payload = json.loads(output.getvalue())
            escaped = io.StringIO()
            with contextlib.redirect_stdout(escaped):
                traversal = wiki_inventory.inventory_show(
                    "../concepts/secret", as_json=False
                )
        check("inventory creates and shows a valid record", created == shown == 0)
        check("inventory show returns the requested record", payload["title"] == "Safe")
        check(
            "inventory rejects traversal without exposing content",
            traversal == 1 and "SECRET" not in escaped.getvalue(),
            escaped.getvalue(),
        )


def main() -> int:
    test_ingest()
    test_archive_reconciliation()
    test_inventory()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
