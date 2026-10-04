#!/usr/bin/env python3
"""Install the reviewed sandbox pin and renew stale host verification.

New registry releases are reported, never approved by this updater. No provider,
note content, authentication or scheduled job is loaded.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import local_runtime
from process_control import check_process_records
from runtime_maintenance import runtime_lock
from runtime_verification import (
    invalidate_verified_runtime,
    require_verified_runtime,
)

ROOT = Path(__file__).resolve().parents[2]
VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?\Z")


def latest_release():
    # Public metadata only. Do not import ambient proxy credentials or npmrc.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(
        "https://registry.npmjs.org/@anthropic-ai%2Fsandbox-runtime/latest",
        timeout=10,
    ) as response:
        payload = response.read(64 * 1024 + 1)
    if len(payload) > 64 * 1024:
        raise ValueError("Registry metadata exceeds size limit")
    version = json.loads(payload)["version"]
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("Registry returned an invalid release version")
    return version


def validate_pins(root):
    version = local_runtime.SRT_VERSION
    if (
        not VERSION.fullmatch(version)
        or f"'@anthropic-ai/sandbox-runtime@{version}'"
        not in (root / "tools/runtime/install.sh").read_text()
        or f"const VERSION = '{version}';"
        not in (root / "tools/runtime/macos-process-guard.mjs").read_text()
    ):
        raise ValueError("Reviewed runtime, installer and macOS guard pins disagree")
    return version


def needs_install(root):
    try:
        local_runtime.runtime_executable(root)
    except ValueError:
        return True
    return False


def maintain(root, *, check=False):
    version = validate_pins(root)
    if check:
        local_runtime.runtime_executable(root)
        require_verified_runtime(root, version)
        print(f"{root.name}: reviewed SRT {version}; current verification matches")
        return
    with runtime_lock(root, exclusive=True):
        check_process_records(root)
        if (root / "tools/runtime-state/cancellation-unconfirmed.json").exists():
            raise ValueError("Unconfirmed cleanup must be resolved before maintenance")
        install = needs_install(root)
        if not install:
            try:
                require_verified_runtime(root, version)
            except ValueError:
                pass
            else:
                print(f"{root.name}: reviewed SRT {version}; no changes needed")
                return
        # Revoke before changing files. Failed install/probe never re-enables runs.
        invalidate_verified_runtime(root)
        if install:
            print(f"{root.name}: installing reviewed SRT {version}", flush=True)
            subprocess.run(
                ["/bin/sh", str(root / "tools/runtime/install.sh")],
                cwd=root,
                check=True,
            )
            local_runtime.runtime_executable(root)
        print(f"{root.name}: rerunning complete synthetic isolation probe", flush=True)
        result = subprocess.run(
            [
                sys.executable,
                str(root / "tools/runtime/probe.py"),
                "--root",
                str(root),
                "--json",
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        reports = root / "tools/runtime-state/maintenance"
        if reports.is_symlink():
            raise ValueError("Maintenance reports cannot follow symbolic links")
        reports.mkdir(mode=0o700, exist_ok=True)
        for name, content in (
            ("last-probe.json", result.stdout),
            ("last-probe.stderr", result.stderr),
        ):
            # Only synthetic probe diagnostics; never native provider output.
            path = reports / name
            if path.is_symlink() or path.exists() and path.stat().st_nlink != 1:
                raise ValueError("Maintenance report cannot be aliased")
            path.write_text(content)
            path.chmod(0o600)
        if result.returncode:
            raise ValueError(
                f"Isolation probe failed; agent launches remain blocked. See {reports}"
            )
        require_verified_runtime(root, version)
        print(f"{root.name}: full isolation verified; reports saved in {reports}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--check", action="store_true", help="Read-only local verification"
    )
    parser.add_argument(
        "--skip-release-check",
        action="store_true",
        help="Do not query the public registry",
    )
    args = parser.parse_args(argv)
    root = args.root.expanduser().absolute()
    try:
        maintain(root, check=args.check)
        if not args.skip_release_check:
            try:
                latest = latest_release()
            except (OSError, ValueError, KeyError) as exc:
                print(
                    f"Release check unavailable: {type(exc).__name__}", file=sys.stderr
                )
                return 1
            if latest != local_runtime.SRT_VERSION:
                print(
                    f"Registry release {latest} differs from reviewed {local_runtime.SRT_VERSION}. "
                    "Compatibility review is required; the reviewed pin was kept."
                )
            else:
                print(f"Reviewed SRT {latest} matches the registry release")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Sandbox maintenance failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
