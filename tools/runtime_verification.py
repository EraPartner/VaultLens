"""Fail-closed receipts for a complete real operating system isolation probe.

Only the probe records verification. A receipt binds its passed checks to public
runtime artifacts, host dependencies, reviewed launcher sources and policy.
This module never loads authentication, notes or provider configuration.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Generator, Iterable, Iterator
from pathlib import Path
from typing import cast

from local_access import FORBIDDEN_DIRS, SECRET_NAMES, JsonObject
from runtime_probe_checks import expected_checks as canonical_probe_checks

RECEIPT_VERSION = 1
MAX_RECEIPT_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_TREE_ENTRIES = 20_000
MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
PUBLIC_SOURCES = (
    "tools/local_access.py",
    "tools/local_runtime.py",
    "tools/runtime_maintenance.py",
    "tools/process_control.py",
    "tools/macos_processes.py",
    "tools/runtime_verification.py",
    "tools/runtime_probe_checks.py",
    "tools/run_reports.py",
    "tools/provider_commands.py",
    "tools/scoped_search.py",
    "tools/agents/wiki-agent.py",
    "tools/runtime/probe.py",
    "tools/runtime/check_boundary.py",
    "tools/runtime/macos-process-guard.mjs",
    "tools/access-profiles.json",
)
RUNTIME_PATH = os.pathsep.join(
    dict.fromkeys(
        (
            str(Path(sys.executable).resolve().parent),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        )
    )
)


def _mapping(value: object) -> JsonObject | None:
    """Return a parsed JSON object, or None for any other value."""
    if not isinstance(value, dict):
        return None
    # JSON object keys are strings; isinstance only narrows to dict[Unknown, Unknown].
    return cast(JsonObject, value)


def _items(value: object) -> list[object] | None:
    """Return a parsed JSON array's items, or None for any other value."""
    if not isinstance(value, list):
        return None
    # isinstance narrows to list[Unknown]; callers validate each item.
    return cast(list[object], value)


def _reviewed_version(version: object) -> str:
    """Runtime guard for callers outside the type checker's reach."""
    if not isinstance(version, str) or not version:
        raise ValueError("Verification requires the reviewed runtime version")
    return version


def _root(root: Path) -> Path:
    try:
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Runtime verification needs one existing vault root")
        return root
    except (OSError, RuntimeError) as exc:
        raise ValueError("Runtime verification vault root is unavailable") from exc


@contextlib.contextmanager
def _directory(path: Path) -> Generator[int, None, None]:
    """Open each component without following directory aliases."""
    if not path.is_absolute():
        raise ValueError("Runtime verification paths must be absolute")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _file(
    path: Path, maximum: int, *, private: bool = False, parent_fd: int | None = None
) -> Generator[int, None, None]:
    parent_context = (
        _directory(path.parent)
        if parent_fd is None
        else contextlib.nullcontext(parent_fd)
    )
    with parent_context as parent:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
            or private
            and (
                stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid()
            )
        ):
            raise ValueError(
                "Verification requires bounded regular single-link files and private receipts"
            )
        yield descriptor
        after = os.fstat(descriptor)
        if (
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_nlink,
            metadata.st_mode,
            metadata.st_uid,
        ) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
            after.st_mode,
            after.st_uid,
        ):
            raise ValueError("Runtime verification file changed while being read")
    finally:
        os.close(descriptor)


def _blocks(descriptor: int, maximum: int) -> Iterator[bytes]:
    total = 0
    while block := os.read(descriptor, min(64 * 1024, maximum + 1 - total)):
        total += len(block)
        if total > maximum:
            raise ValueError("Runtime verification file exceeds its size limit")
        yield block


def _bytes(
    path: Path, maximum: int, *, private: bool = False, parent_fd: int | None = None
) -> bytes:
    with _file(path, maximum, private=private, parent_fd=parent_fd) as descriptor:
        return b"".join(_blocks(descriptor, maximum))


def _hash(path: Path, maximum: int) -> str:
    digest = hashlib.sha256()
    with _file(path, maximum) as descriptor:
        for block in _blocks(descriptor, maximum):
            digest.update(block)
    return digest.hexdigest()


def _private_name(path: Path) -> bool:
    return any(part in FORBIDDEN_DIRS - {"node_modules"} for part in path.parts) or any(
        fnmatch.fnmatch(path.name, pattern) for pattern in SECRET_NAMES
    )


def _runtime_tree(root: Path) -> JsonObject:
    tree = root / "tools/runtime-node"
    digest = hashlib.sha256()
    entries = 0
    total = 0

    def visit(directory: Path, depth: int = 0) -> None:
        nonlocal entries, total
        if depth > 64:
            raise ValueError("Runtime artifact tree exceeds its depth limit")
        with _directory(directory) as descriptor:
            for name in sorted(os.listdir(descriptor)):
                path = directory / name
                relative = path.relative_to(tree)
                if _private_name(relative):
                    raise ValueError("Runtime artifact tree contains nonpublic state")
                entries += 1
                if entries > MAX_TREE_ENTRIES:
                    raise ValueError("Runtime artifact tree exceeds its entry limit")
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                record: JsonObject = {
                    "path": relative.as_posix(),
                    "mode": stat.S_IMODE(info.st_mode),
                }
                if stat.S_ISLNK(info.st_mode):
                    target = path.resolve(strict=True)
                    if not target.is_relative_to(tree) or _private_name(
                        target.relative_to(tree)
                    ):
                        raise ValueError(
                            "Runtime artifact links must stay within the public installation tree"
                        )
                    if not target.is_file() and not target.is_dir():
                        raise ValueError(
                            "Runtime artifact link has no regular file or directory target"
                        )
                    record.update(
                        type="link", target=target.relative_to(tree).as_posix()
                    )
                elif stat.S_ISDIR(info.st_mode):
                    record["type"] = "directory"
                    visit(path, depth + 1)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > MAX_TREE_BYTES:
                        raise ValueError(
                            "Runtime artifact tree exceeds its content limit"
                        )
                    record.update(
                        type="file",
                        size=info.st_size,
                        sha256=_hash(path, MAX_ARTIFACT_BYTES),
                    )
                else:
                    raise ValueError(
                        "Runtime artifact tree contains a nonregular object"
                    )
                digest.update(
                    json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
                    + b"\n"
                )

    visit(tree)
    if entries == 0:
        raise ValueError("Runtime artifact tree is empty")
    return {"sha256": digest.hexdigest(), "entries": entries, "bytes": total}


def _executable(path: Path, version_args: tuple[str, ...]) -> JsonObject:
    path = path.resolve(strict=True)
    if _private_name(path) or not os.access(path, os.X_OK):
        raise ValueError("Runtime dependency must be an executable public file")
    digest = _hash(path, MAX_EXECUTABLE_BYTES)
    result = subprocess.run(
        [str(path), *version_args],
        capture_output=True,
        text=True,
        timeout=5,
        env={"PATH": RUNTIME_PATH, "LANG": "C", "LC_ALL": "C"},
        cwd="/",
    )
    output = (result.stdout or result.stderr).strip()
    if result.returncode or not output or len(output) > 4096:
        raise ValueError("Runtime dependency version could not be verified")
    return {"path": str(path), "sha256": digest, "version": output}


def _find(name: str) -> Path:
    executable = shutil.which(name, path=RUNTIME_PATH)
    if not executable:
        raise ValueError(f"Runtime verification dependency is missing: {name}")
    return Path(executable)


def _fingerprint_runtime(root: Path, version: str) -> JsonObject:
    """Compute current public evidence; tests mock this hook in isolated roots."""
    package = (
        root
        / "tools/runtime-node/node_modules/@anthropic-ai/sandbox-runtime/package.json"
    )
    try:
        installed: object = json.loads(_bytes(package, MAX_SOURCE_BYTES))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Installed runtime package metadata is invalid") from exc
    package_metadata = _mapping(installed)
    if package_metadata is None or package_metadata.get("version") != version:
        raise ValueError("Installed runtime version differs from the reviewed version")
    sources: dict[str, str | None] = {
        relative: _hash(root / relative, MAX_SOURCE_BYTES)
        for relative in PUBLIC_SOURCES
    }
    local = root / "tools/access.local.json"
    try:
        sources["tools/access.local.json"] = _hash(local, MAX_SOURCE_BYTES)
    except FileNotFoundError:
        sources["tools/access.local.json"] = None
    helpers: dict[str, JsonObject] = {}
    if platform.system() == "Linux":
        for name, arguments in (
            ("bwrap", ("--version",)),
            ("socat", ("-V",)),
            ("rg", ("--version",)),
        ):
            helpers[name] = _executable(_find(name), arguments)
    python = Path(sys.executable).resolve(strict=True)
    return {
        "runtime_version": version,
        "runtime_artifacts": _runtime_tree(root),
        "node": _executable(_find("node"), ("--version",)),
        "python": {
            "path": str(python),
            "sha256": _hash(python, MAX_EXECUTABLE_BYTES),
            "version": platform.python_version(),
        },
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
        },
        "linux_helpers": helpers,
        "sources": sources,
    }


def fingerprint_runtime(root: Path, version: str) -> JsonObject:
    """Capture public evidence before the real probe, without creating a receipt."""
    _reviewed_version(version)
    root = _root(root)
    try:
        # Tests mock the hook, so keep the runtime shape check below.
        captured: object = _fingerprint_runtime(root, version)
        fingerprint = _mapping(captured)
        if not fingerprint:
            raise ValueError("Runtime verification fingerprint is empty")
        return fingerprint
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeError,
        TypeError,
        RuntimeError,
    ) as exc:
        raise ValueError("Runtime verification fingerprint is unavailable") from exc


def _duplicate_free(pairs: list[tuple[str, object]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Verification receipt contains duplicate fields")
        result[key] = value
    return result


def _checks(report: object, expected_checks: Iterable[str]) -> list[str]:
    if isinstance(expected_checks, (str, bytes)):
        raise ValueError(
            "Verification requires an explicit collection of expected checks"
        )
    try:
        requested: list[object] = list(expected_checks)
    except TypeError as exc:
        raise ValueError(
            "Verification requires an explicit collection of expected checks"
        ) from exc
    expected = [name for name in requested if isinstance(name, str)]
    if (
        not requested
        or len(expected) != len(requested)
        or any(not name or len(name) > 256 for name in expected)
        or len(set(expected)) != len(expected)
    ):
        raise ValueError("Verification requires nonempty unique expected check names")
    if set(expected) != set(canonical_probe_checks()):
        raise ValueError(
            "Verification requires the complete current canonical probe check set"
        )
    probe_report = _mapping(report)
    if (
        probe_report is None
        or probe_report.get("os_isolation_verified") is not True
    ):
        raise ValueError(
            "Only a complete verified operating system probe may record a receipt"
        )
    items = _items(probe_report.get("checks"))
    if not items:
        raise ValueError("Verification report has no checks")
    delivered: list[str] = []
    for item in items:
        entry = _mapping(item)
        check = entry.get("check") if entry is not None else None
        if entry is None or entry.get("status") != "passed" or not isinstance(check, str):
            raise ValueError("Every required operating system probe check must pass")
        delivered.append(check)
    if len(delivered) != len(set(delivered)) or set(delivered) != set(expected):
        raise ValueError(
            "Verification report does not contain exactly the expected unique checks"
        )
    return sorted(expected)


@contextlib.contextmanager
def _state(root: Path, *, create: bool = False) -> Generator[int, None, None]:
    with _directory(root / "tools") as tools:
        if create:
            try:
                os.mkdir("runtime-state", mode=0o700, dir_fd=tools)
            except FileExistsError:
                pass
        descriptor = os.open(
            "runtime-state", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=tools
        )
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid():
            raise ValueError(
                "Runtime verification state must belong to the current operator"
            )
        if create:
            os.fchmod(descriptor, 0o700)
        elif stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Runtime verification state must be private")
        try:
            os.stat(
                "cancellation-unconfirmed.json",
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise ValueError(
                "Runtime verification is blocked by unconfirmed process cancellation"
            )
        yield descriptor
    finally:
        os.close(descriptor)


def _receipt_info(descriptor: int) -> tuple[int, int, int, int] | None:
    try:
        info = os.stat("verification.json", dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
    ):
        raise ValueError("Runtime verification receipt cannot be aliased or hardlinked")
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _write_receipt(descriptor: int, payload: bytes) -> None:
    before = _receipt_info(descriptor)
    name = f".verification-{uuid.uuid4().hex}.tmp"
    staged = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=descriptor,
    )
    try:
        os.fchmod(staged, 0o600)
        with os.fdopen(staged, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(staged)
        if _receipt_info(descriptor) != before:
            raise ValueError("Runtime verification receipt changed during the probe")
        os.replace(
            name, "verification.json", src_dir_fd=descriptor, dst_dir_fd=descriptor
        )
        os.fsync(descriptor)
    finally:
        os.close(staged)
        try:
            os.unlink(name, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def record_verified_probe(
    root: Path, version: str, report: JsonObject, expected_checks: Iterable[str]
) -> Path:
    """Record only complete real-probe evidence; no bootstrap or bypass exists."""
    checks = _checks(report, expected_checks)
    _reviewed_version(version)
    before = _mapping(report.get("runtime_fingerprint"))
    if not before:
        raise ValueError(
            "Verification requires the runtime fingerprint captured before the probe"
        )
    root = _root(root)
    try:
        with _state(root, create=True) as descriptor:
            fingerprint = fingerprint_runtime(root, version)
            if fingerprint != before:
                raise ValueError(
                    "Runtime changed during the probe; rerun the complete operating system probe"
                )
            receipt = {
                "version": RECEIPT_VERSION,
                "root": str(root),
                "runtime_version": version,
                "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "checks": checks,
                "fingerprint": fingerprint,
            }
            payload = (
                json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n"
            ).encode()
            if len(payload) > MAX_RECEIPT_BYTES:
                raise ValueError("Runtime verification receipt exceeds its size limit")
            _write_receipt(descriptor, payload)
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeError,
        TypeError,
        RuntimeError,
    ) as exc:
        raise ValueError("Runtime probe verification could not be recorded") from exc
    return root / "tools/runtime-state/verification.json"


def invalidate_verified_runtime(root: Path) -> None:
    """Revoke prior evidence only for an explicit real-probe workflow.

    Missing state or a missing receipt is harmless. Alias and hardlink refusals
    preserve unrelated targets and leave malformed state for operator review.
    """
    root = _root(root)
    try:
        with _directory(root / "tools/runtime-state") as descriptor:
            before = _receipt_info(descriptor)
            if before is None:
                return
            state = os.fstat(descriptor)
            if state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) & 0o022:
                raise ValueError(
                    "Runtime verification state is not privately controlled by this operator"
                )
            receipt = os.open(
                "verification.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(receipt)
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ) != before or _receipt_info(descriptor) != before:
                    raise ValueError(
                        "Runtime verification receipt changed during revocation"
                    )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or opened.st_uid != os.getuid()
                ):
                    raise ValueError(
                        "Runtime verification receipt cannot be aliased or hardlinked"
                    )
                os.unlink("verification.json", dir_fd=descriptor)
                os.fsync(descriptor)
            finally:
                os.close(receipt)
    except FileNotFoundError:
        return
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "Prior runtime verification could not be safely revoked"
        ) from exc


def require_verified_runtime(root: Path, version: str) -> None:
    """Refuse missing, stale, malformed or quarantined evidence before launch."""
    _reviewed_version(version)
    root = _root(root)
    try:
        with _state(root) as descriptor:
            parsed: object = json.loads(
                _bytes(
                    root / "tools/runtime-state/verification.json",
                    MAX_RECEIPT_BYTES,
                    private=True,
                    parent_fd=descriptor,
                ),
                object_pairs_hook=_duplicate_free,
            )
            data = _mapping(parsed)
            if (
                data is None
                or set(data)
                != {
                    "version",
                    "root",
                    "runtime_version",
                    "verified_at",
                    "checks",
                    "fingerprint",
                }
                or type(data["version"]) is not int
                or data["version"] != RECEIPT_VERSION
                or data["root"] != str(root)
                or data["runtime_version"] != version
                or not isinstance(data["verified_at"], str)
                or dt.datetime.fromisoformat(data["verified_at"]).utcoffset() is None
                or not isinstance(data["fingerprint"], dict)
                or not data["fingerprint"]
            ):
                raise ValueError(
                    "Runtime verification receipt is malformed or mismatched"
                )
            _checks(
                {
                    "os_isolation_verified": True,
                    "checks": [
                        {"check": name, "status": "passed"} for name in data["checks"]
                    ],
                },
                canonical_probe_checks(),
            )
            if data["fingerprint"] != fingerprint_runtime(root, version):
                raise ValueError(
                    "Runtime verification is stale; rerun the complete operating system probe"
                )
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeError,
        TypeError,
        KeyError,
        RuntimeError,
    ) as exc:
        raise ValueError(
            "Runtime verification is missing, unreadable or invalid; run the complete operating system probe"
        ) from exc
