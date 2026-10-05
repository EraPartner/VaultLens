#!/usr/bin/env python3
"""Focused regression tests for ingest, archive, and inventory modules."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
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

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
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

        def extract(
            command: list[str], *, timeout: float = 0, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            check("PDF subprocess has a timeout", timeout > 0)
            Path(command[-1]).write_text("extracted words", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(wiki_ingest, "RAW_SOURCES_TEXT_DIR", text_dir),
            patch.object(shutil, "which", return_value="/usr/bin/tool"),
            patch.object(subprocess, "run", side_effect=extract),
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
        # A list so the count survives the closure without a narrowed-literal comparison.
        calls = [0]

        def decrypt(
            command: list[str], *, timeout: float = 0, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls[0] += 1
            check(f"decryption call {calls[0]} has a timeout", timeout > 0)
            if calls[0] == 1:
                return subprocess.CompletedProcess(command, 1, "", permission_error)
            if command[0] == "qpdf":
                Path(command[-1]).write_bytes(b"decrypted")
            else:
                Path(command[-1]).write_text("decrypted words", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(wiki_ingest, "RAW_SOURCES_TEXT_DIR", text_dir),
            patch.object(shutil, "which", return_value="/usr/bin/tool"),
            patch.object(subprocess, "run", side_effect=decrypt),
        ):
            path, status = wiki_ingest.extract_pdf_to_markdown(pdf, force=True)
        check(
            "copy-protected PDF uses qpdf fallback",
            status is wiki_ingest.ExtractStatus.DECRYPTED,
        )
        check("qpdf fallback performs three bounded calls", calls[0] == 3, str(calls[0]))
        check(
            "decrypted temporary file is cleaned",
            not path.with_suffix(".decrypted.pdf").exists(),
        )

        with (
            patch.object(wiki_ingest, "RAW_SOURCES_TEXT_DIR", text_dir),
            patch.object(shutil, "which", return_value="/usr/bin/tool"),
            patch.object(
                subprocess,
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


def test_archive_registry_safety() -> None:
    print("wiki_archive registry safety:")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        page = write_page(root, "concepts/live.md", status="active")
        registry = root / "system" / "archive-registry.json"
        registry.parent.mkdir(parents=True)
        corrupt = '{"archived": {"concepts/kept": {"archived_on": "2026-01-01", "reason": "x"}'
        registry.write_text(corrupt, encoding="utf-8")
        with (
            patch.object(wiki, "WIKI_DIR", root),
            patch.object(wiki_archive, "WIKI_DIR", root),
            patch.object(wiki_archive, "REGISTRY_PATH", registry),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            archived = wiki_archive.archive_page("concepts/live", "why")
            listed = wiki_archive.list_archived(as_json=False)
        check("archive refuses while the registry is corrupt", archived == 1, out.getvalue())
        check("corrupt registry is not overwritten", registry.read_text(encoding="utf-8") == corrupt)
        check("page status untouched when archive is refused", "status: active" in page.read_text())
        check("list reports the corrupt registry", listed == 1)

        registry.write_text(json.dumps({"archived": {}}), encoding="utf-8")
        with (
            patch.object(wiki, "WIKI_DIR", root),
            patch.object(wiki_archive, "WIKI_DIR", root),
            patch.object(wiki_archive, "REGISTRY_PATH", registry),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            wiki_archive.archive_page("concepts/live", "why")
        data = json.loads(registry.read_text(encoding="utf-8"))
        check("archive writes a valid registry", "concepts/live" in data["archived"], str(data))
        check("no temp files left beside the registry", [p.name for p in registry.parent.iterdir()] == [registry.name])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        draft = write_page(root, "concepts/draft.md", status="draft")
        registry = root / "system" / "archive-registry.json"
        with (
            patch.object(wiki, "WIKI_DIR", root),
            patch.object(wiki_archive, "WIKI_DIR", root),
            patch.object(wiki_archive, "REGISTRY_PATH", registry),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rc = wiki_archive.restore_page("concepts/draft")
        check("restore of a non-archived page succeeds", rc == 0)
        check("restore leaves a non-archived page's status alone", "status: draft" in draft.read_text(), draft.read_text())


def test_projects_todo_script_is_atomic() -> None:
    print("rebuild-projects-todo.sh failure handling:")
    script_src = Path(__file__).resolve().parents[1] / "scripts" / "rebuild-projects-todo.sh"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tools" / "scripts").mkdir(parents=True)
        (root / "projects").mkdir()
        script = root / "tools" / "scripts" / "rebuild-projects-todo.sh"
        shutil.copy(script_src, script)
        # A wiki.py that fails, as when python or the project CLI breaks.
        (root / "tools" / "wiki.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        live = root / "projects" / "TODO.md"
        widget = root / "projects" / "TODO-widget.md"
        live.write_text("PREVIOUS LIVE\n", encoding="utf-8")
        widget.write_text("PREVIOUS WIDGET\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=60, env=dict(os.environ)
        )
        check("script exits non-zero when the project CLI fails", result.returncode != 0, result.stderr)
        check("live view keeps its previous content", live.read_text() == "PREVIOUS LIVE\n", live.read_text())
        check("widget view keeps its previous content", widget.read_text() == "PREVIOUS WIDGET\n", widget.read_text())
        check(
            "no temp files left behind",
            sorted(p.name for p in (root / "projects").iterdir()) == ["TODO-widget.md", "TODO.md"],
        )

        # A working project CLI still produces both views.
        (root / "tools" / "wiki.py").write_text("print('alpha')\n", encoding="utf-8")
        (root / "projects" / "alpha").mkdir()
        (root / "projects" / "alpha" / "TODO.md").write_text(
            "- [ ] Ship it 📅 2026-12-01\n", encoding="utf-8"
        )
        ok = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
        check("script succeeds when the CLI works", ok.returncode == 0, ok.stderr)
        check("live view embeds the project", "![[projects/alpha/TODO]]" in live.read_text())
        check("widget view lists the dated task", "Ship it" in widget.read_text())


def main() -> int:
    test_ingest()
    test_archive_reconciliation()
    test_inventory()
    test_archive_registry_safety()
    test_projects_todo_script_is_atomic()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
