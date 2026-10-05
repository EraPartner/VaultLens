#!/usr/bin/env python3
"""Tests for the qmd review-inbox consent boundary."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "configure-qmd.py"
SPEC = importlib.util.spec_from_file_location("configure_qmd", SCRIPT)
assert SPEC and SPEC.loader
configure_qmd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure_qmd)


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"PASS  {name}")


SETUP = SCRIPT.with_name("setup-qmd.sh")
SYSTEM_PATH = "/usr/bin:/bin"


def write_stub(directory: Path, name: str, body: str) -> None:
    stub = directory / name
    stub.write_text("#!/bin/sh\n" + body)
    stub.chmod(0o755)


def run_setup(stubs: Path, home: Path) -> subprocess.CompletedProcess[str]:
    environment = {"PATH": f"{stubs}:{SYSTEM_PATH}", "HOME": str(home)}
    return subprocess.run(
        ["/bin/bash", str(SETUP)],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def setup_script_checks() -> None:
    if not Path("/bin/bash").exists() or shutil.which("qmd", path=SYSTEM_PATH):
        print("SKIP  setup-qmd.sh checks (no /bin/bash, or a system qmd)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stubs = root / "stubs"
        stubs.mkdir()
        calls = root / "calls"
        for installer in ("bun", "npm"):
            write_stub(stubs, installer, f'echo "{installer} $@" >> "{calls}"\n')
        result = run_setup(stubs, root)
        check("missing qmd is not installed from a non-interactive run", result.returncode == 1 and not calls.exists())
        check("missing qmd names the install command", "qmd is not installed" in result.stdout)

        write_stub(stubs, "python3", f'exec "{sys.executable}" "$@"\n')
        write_stub(
            stubs,
            "qmd",
            'case "$1 $2" in\n'
            '  "collection add") echo "STUB_QMD_ERROR" >&2; exit 1 ;;\n'
            "esac\nexit 0\n",
        )
        config = root / ".config" / "qmd"
        config.mkdir(parents=True)
        (config / "index.yml").write_text("collections:\n  raw:\n    path: /raw\n")
        result = run_setup(stubs, root)
        check("qmd collection errors stay visible", "STUB_QMD_ERROR" in result.stderr)
        check("setup continues after a collection that already exists", result.returncode == 0)
        check("setup applies the review-inbox boundary", "review-inbox/**" in (config / "index.yml").read_text())


def main() -> int:
    base = """collections:
  wiki:
    path: /vault/wiki
    pattern: "**/*.md"
  raw:
    path: /vault/raw
    pattern: "**/*.md"
models:
  embed: example
"""
    updated, changed = configure_qmd.ensure_review_inbox_ignored(base)
    check("adds ignore rule to raw collection", changed and "review-inbox/**" in updated)
    check("does not attach rule to wiki", updated.index("review-inbox/**") > updated.index("  raw:"))

    repeated, changed_again = configure_qmd.ensure_review_inbox_ignored(updated)
    check("is idempotent", not changed_again and repeated == updated)

    existing = base.replace(
        "models:", '    ignore:\n      - "drafts/**"\nmodels:'
    )
    extended, changed_existing = configure_qmd.ensure_review_inbox_ignored(existing)
    check(
        "preserves existing ignore rules",
        changed_existing
        and '      - "drafts/**"' in extended
        and '      - "review-inbox/**"' in extended,
    )

    for label, inline, expected in (
        ("inline list", '    ignore: ["drafts/**"]\n', '    ignore: ["drafts/**", "review-inbox/**"]'),
        ("empty inline list", "    ignore: []\n", '    ignore: ["review-inbox/**"]'),
    ):
        flow = base.replace("models:", inline + "models:")
        fixed, changed_flow = configure_qmd.ensure_review_inbox_ignored(flow)
        check(f"{label} is extended in place", changed_flow and expected in fixed)
        check(f"{label} adds no duplicate ignore key", fixed.count("ignore:") == 1)
        again, changed_flow_again = configure_qmd.ensure_review_inbox_ignored(fixed)
        check(f"{label} update is idempotent", not changed_flow_again and again == fixed)

    same_indent = base.replace("models:", '    ignore:\n    - "drafts/**"\nmodels:')
    fixed, changed_same = configure_qmd.ensure_review_inbox_ignored(same_indent)
    check(
        "block list indented like its key keeps one consistent indent",
        changed_same
        and fixed.count("ignore:") == 1
        and '    - "drafts/**"\n    - "review-inbox/**"' in fixed,
    )

    try:
        configure_qmd.ensure_review_inbox_ignored(
            base.replace("models:", "    ignore: drafts/**\nmodels:")
        )
    except ValueError:
        check("unsupported scalar ignore form is refused, not duplicated", True)
    else:
        check("unsupported scalar ignore form is refused, not duplicated", False)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "index.yml"
        path.write_text(base, encoding="utf-8")
        check("atomic file update changes config", configure_qmd.update_config(path))
        check("second file update is a no-op", not configure_qmd.update_config(path))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "index.yml"
        path.write_text(base, encoding="utf-8")
        with mock.patch.object(configure_qmd.os, "replace", side_effect=OSError("denied")):
            try:
                configure_qmd.update_config(path)
            except OSError:
                pass
            else:
                check("failed replace is reported", False)
        check("failed replace leaves the config untouched", path.read_text(encoding="utf-8") == base)
        check("failed replace leaks no temporary file", [p.name for p in Path(tmp).iterdir()] == ["index.yml"])

    setup_script_checks()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
