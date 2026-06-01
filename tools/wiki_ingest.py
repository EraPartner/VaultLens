#!/usr/bin/env python3
"""Source ingest preprocessing: turn raw/sources/*.pdf into readable markdown.

Agents cannot open binary PDFs, so before a source can be ingested its text is
pre-extracted into a markdown sibling under `raw/sources-text/`. This module owns
that pipeline (pdftotext, qpdf fallback for copy-protected PDFs) plus the
inbox->sources promotion step. Everything here is reachable via
`python3 tools/wiki.py preprocess`.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
from enum import Enum
from pathlib import Path

from wiki import ROOT

RAW_SOURCES_DIR = ROOT / "raw" / "sources"
RAW_SOURCES_TEXT_DIR = ROOT / "raw" / "sources-text"
RAW_INBOX_DIR = ROOT / "raw" / "inbox"


class ExtractStatus(Enum):
    EXTRACTED = "extracted"
    SKIPPED = "skipped"
    DECRYPTED = "decrypted"


def text_path_for_pdf(pdf_path: Path) -> Path:
    """Return the markdown sibling path for a given PDF in raw/sources/."""
    return RAW_SOURCES_TEXT_DIR / f"{pdf_path.stem}.md"


def _pdf_needs_extract(pdf_path: Path, text_path: Path) -> bool:
    if not text_path.exists():
        return True
    return pdf_path.stat().st_mtime > text_path.stat().st_mtime


def extract_pdf_to_markdown(
    pdf_path: Path, force: bool = False
) -> tuple[Path, ExtractStatus]:
    """Extract a PDF into a markdown sibling using `pdftotext -layout`.

    Returns (markdown_path, status). Status is SKIPPED when the sibling
    exists and is newer than the PDF (and force=False), EXTRACTED on a
    plain run, or DECRYPTED when qpdf was needed first.
    Raises FileNotFoundError, RuntimeError on failure.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if shutil.which("pdftotext") is None:
        raise RuntimeError(
            "pdftotext is not installed. Install poppler (`brew install poppler`) "
            "or xpdf-tools and retry."
        )

    text_path = text_path_for_pdf(pdf_path)
    if not force and not _pdf_needs_extract(pdf_path, text_path):
        return text_path, ExtractStatus.SKIPPED

    text_path.parent.mkdir(parents=True, exist_ok=True)

    raw_txt = text_path.with_suffix(".raw.txt")
    decrypted_pdf: Path | None = None
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), str(raw_txt)],
            capture_output=True,
            text=True,
        )
        PERMISSION_ERROR = "Copying of text from this document is not allowed"
        if result.returncode != 0 and PERMISSION_ERROR in result.stderr:
            # PDF has copy-protection; strip restrictions with qpdf and retry.
            if shutil.which("qpdf") is None:
                raise RuntimeError(
                    f"Permission Error: {PERMISSION_ERROR}. "
                    "Install qpdf (`brew install qpdf`) to bypass restriction."
                )
            decrypted_pdf = text_path.with_suffix(".decrypted.pdf")
            qpdf_result = subprocess.run(
                ["qpdf", "--decrypt", str(pdf_path), str(decrypted_pdf)],
                capture_output=True,
                text=True,
            )
            if qpdf_result.returncode != 0:
                raise RuntimeError(
                    f"qpdf decrypt failed for {pdf_path.name}: {qpdf_result.stderr.strip()}"
                )
            result = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", str(decrypted_pdf), str(raw_txt)],
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"pdftotext failed for {pdf_path.name}: {result.stderr.strip()}"
            )

        body = raw_txt.read_text(encoding="utf-8", errors="replace")
    finally:
        raw_txt.unlink(missing_ok=True)
        if decrypted_pdf is not None:
            decrypted_pdf.unlink(missing_ok=True)

    today = dt.datetime.now().strftime("%Y-%m-%d")
    status = (
        ExtractStatus.DECRYPTED if decrypted_pdf is not None else ExtractStatus.EXTRACTED
    )
    extractor = (
        "qpdf --decrypt | pdftotext -layout"
        if status is ExtractStatus.DECRYPTED
        else "pdftotext -layout"
    )
    # Record the PDF's actual location so the sibling stays truthful whether the
    # PDF sits in raw/sources/ or is still awaiting promotion in raw/inbox/.
    try:
        source_ref = pdf_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        source_ref = pdf_path.name
    header = (
        "---\n"
        f'source_pdf: "{source_ref}"\n'
        f"extracted: {today}\n"
        f"extractor: {extractor}\n"
        "---\n\n"
        f"# {pdf_path.stem}\n\n"
        "> Pre-extracted plain text from the source PDF. Treat as ground-truth\n"
        "> reference content. Layout artifacts (page numbers, headers, line\n"
        "> breaks inside paragraphs) may be present.\n\n"
    )

    text_path.write_text(header + body, encoding="utf-8")
    return text_path, status


def promote_inbox_pdf(pdf_path: Path) -> Path | None:
    """Move a freshly ingested PDF out of raw/inbox/ into raw/sources/.

    raw/sources/ is the canonical home for ingested source PDFs; raw/inbox/ is
    only a staging area for files awaiting ingest. Call this after a successful
    ingest so the source no longer shows up in inbox triage.

    Returns the new path on a move, or None when nothing was moved (the PDF is
    not under raw/inbox/, or a different file already occupies the destination).
    Re-points the extracted sibling's `source_pdf:` header to the new location.
    Raises FileNotFoundError if the PDF does not exist.
    """
    pdf_path = pdf_path.resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Only promote files that actually live in raw/inbox/. Anything already in
    # raw/sources/ (or elsewhere) is left untouched — "if not already there".
    try:
        pdf_path.relative_to(RAW_INBOX_DIR.resolve())
    except ValueError:
        return None

    dest = RAW_SOURCES_DIR / pdf_path.name
    if dest.exists():
        # Don't clobber a different source that already claims this name.
        print(
            f"Warning: {dest.relative_to(ROOT)} already exists; "
            f"leaving {pdf_path.name} in raw/inbox/ to avoid overwriting it."
        )
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pdf_path), str(dest))

    # Keep the extracted sibling's provenance header pointing at the new home.
    text_path = text_path_for_pdf(dest)
    if text_path.exists():
        contents = text_path.read_text(encoding="utf-8", errors="replace")
        new_ref = dest.relative_to(ROOT).as_posix()
        updated = re.sub(
            r'^(source_pdf:\s*").*?(")\s*$',
            rf"\g<1>{new_ref}\g<2>",
            contents,
            count=1,
            flags=re.MULTILINE,
        )
        if updated != contents:
            text_path.write_text(updated, encoding="utf-8")

    return dest


def preprocess_pdfs(pdf: str | None, force: bool) -> int:
    """CLI entry: preprocess one PDF or all PDFs under raw/sources/."""
    if pdf:
        pdf_path = Path(pdf)
        if not pdf_path.is_absolute():
            pdf_path = (ROOT / pdf_path).resolve()
        targets = [pdf_path]
    else:
        if not RAW_SOURCES_DIR.exists():
            print(f"No raw/sources directory at {RAW_SOURCES_DIR}")
            return 1
        targets = sorted(RAW_SOURCES_DIR.glob("*.pdf"))

    if not targets:
        print("No PDFs found to preprocess.")
        return 0

    counts = {status: 0 for status in ExtractStatus}
    failed = 0
    for target in targets:
        try:
            text_path, status = extract_pdf_to_markdown(target, force=force)
            counts[status] += 1
            print(f"  {status.value:<9} {target.name} -> {text_path.relative_to(ROOT)}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL      {target.name}: {exc}")

    print(
        f"\nPreprocess complete. extracted={counts[ExtractStatus.EXTRACTED]} "
        f"decrypted={counts[ExtractStatus.DECRYPTED]} "
        f"skipped={counts[ExtractStatus.SKIPPED]} failed={failed}"
    )
    return 0 if failed == 0 else 2
