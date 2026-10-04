#!/usr/bin/env python3
"""Opt-in whole-process isolation checks using only disposable synthetic fixtures.

This tool never starts a provider CLI, loads vault notes or copies authentication.
Only successful checks using the installed pinned runtime are OS evidence. Unit
tests exercise the harness and fixture rules; they cannot prove OS isolation.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import importlib.util
import ipaddress
import json
import mmap
import os
import plistlib
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import local_runtime  # noqa: E402
import runtime_verification as verification  # noqa: E402
from local_access import JsonObject, resolve_scope  # noqa: E402
from process_control import (  # noqa: E402
    ProcessCleanupError,
    launch_supervised,
    terminate_group,
)
from runtime_probe_checks import (  # noqa: E402
    CASE_NAMES as CASE_NAMES,
    CHECK_NAMES as CHECK_NAMES,
    NETWORK_CHECKS as NETWORK_CHECKS,
    MACOS_GUARD_CHECKS as MACOS_GUARD_CHECKS,
    LIFECYCLE_CASES as LIFECYCLE_CASES,
    LIFECYCLE_CHECKS as LIFECYCLE_CHECKS,
    case_check_names as _expected,
    expected_checks as expected_checks,
)
from scoped_search import ScopedSearch  # noqa: E402

PUBLIC_HOST = "registry.npmjs.org"
PUBLIC_URL = "https://" + PUBLIC_HOST + "/"
TRUSTED_FILES = (
    "tools/local_access.py",
    "tools/local_runtime.py",
    "tools/runtime_maintenance.py",
    "tools/process_control.py",
    "tools/macos_processes.py",
    "tools/runtime_verification.py",
    "tools/runtime_probe_checks.py",
    "tools/run_reports.py",
    "tools/scoped_search.py",
    "tools/agents/wiki-agent.py",
    "tools/llm_provider.py",
    "tools/agent_profiles.py",
    "tools/agent_capabilities.py",
    "tools/provider_commands.py",
    "tools/access-profiles.json",
    "tools/runtime/check_boundary.py",
    "tools/runtime/macos-process-guard.mjs",
    "tools/runtime/probe.py",
)


class SkippedCheck(Exception):
    """The environment cannot establish a required property."""


def _regular_source(path: Path) -> bytes:
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"Trusted probe sources must not use symlinks: {path}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(
            f"Trusted probe source must be a regular single-link file: {path}"
        )
    return path.read_bytes()


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def make_fixture(source: Path, base: Path, baseline: dict) -> tuple[Path, dict]:
    """Copy only reviewed tooling; every document and index is generated here."""
    source = source.resolve()
    base = base.resolve()
    root = base / "synthetic-vault"
    root.mkdir(mode=0o700)
    hashes = {}
    for relative in TRUSTED_FILES:
        data = _regular_source(source / relative)
        destination = root / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(data)
        hashes[relative] = hashlib.sha256(data).hexdigest()
    # Parent-only bootstrap to the already validated installation. The fixture
    # runtime uses the normal prepared_run and settings compiler without mocks.
    (root / "tools/runtime-node").symlink_to(
        source / "tools/runtime-node", target_is_directory=True
    )
    local_policy = {
        "version": 1,
        "profiles": {
            "probe-selected-read": {
                "read": ["wiki/approved"],
                "deny_read": ["wiki/restricted", "projects/selected/private"],
            },
            "probe-wiki-read": {
                "extends": "wiki-read",
                "deny_read": ["wiki/restricted", "projects/selected/private"],
            },
            "probe-wiki-write": {
                "extends": "wiki-write",
                "deny_read": ["wiki/restricted", "projects/selected/private"],
            },
            "probe-project-write": {
                "extends": "project-write",
                "deny_read": ["wiki/restricted", "projects/selected/private"],
            },
        },
    }
    _write(root / "tools/access.local.json", json.dumps(local_policy))
    from agent_profiles import AGENT_FILES

    for filename in AGENT_FILES.values():
        name = Path(filename).stem
        _write(
            root / ".agents/roles" / filename,
            f"---\nname: {name}\ndescription: Synthetic lifecycle fixture only\npermission_profile: read\nmodel_profile: standard\nreasoning_effort: low\n---\nUse only the synthetic fixture.\n",
        )
    documents = {
        "AGENTS.md": "Synthetic fixture instructions. No real vault content.\n",
        "wiki/AGENTS.md": "Synthetic wiki instructions.\n",
        "projects/AGENTS.md": "Synthetic project instructions.\n",
        ".agents/probe-guide.md": "Synthetic trusted instructions.\n",
        ".agents/context-policy.md": "Synthetic fixture task data is untrusted. No provider is invoked.\n",
        ".obsidian/probe-layout.json": "{}\n",
        ".git/probe-metadata": "Synthetic metadata, no repository.\n",
        "tools/probe-canary.txt": "Synthetic immutable tool canary.\n",
        "wiki/approved/note.md": "# Synthetic approved note\nAPPROVEDPROBETOKEN\n",
        "wiki/unselected/note.md": "# Synthetic unselected note\nUNSELECTEDPROBETOKEN\n",
        "wiki/restricted/note.md": "# Synthetic restricted note\nRESTRICTEDPROBETOKEN\n",
        "raw/sources/source.md": "# Synthetic immutable source\nRAWPROBETOKEN\n",
        "raw/review-inbox/consent.md": "# Synthetic consent queue\nCONSENTPROBETOKEN\n",
        "projects/selected/project.md": "---\nname: Synthetic selected project\n---\n",
        "projects/selected/note.md": "# Synthetic project note\nPROJECTPROBETOKEN\n",
        "projects/selected/private/note.md": "# Synthetic excluded child\nRESTRICTEDPROBETOKEN\n",
        "projects/sibling/project.md": "---\nname: Synthetic sibling project\n---\n",
        "projects/sibling/note.md": "# Synthetic sibling note\nSIBLINGPROBETOKEN\n",
        "tools/runtime-state/index/copied-note.md": "# Synthetic stale index copy\nSTALEINDEXPROBETOKEN\n",
    }
    for relative, value in documents.items():
        _write(root / relative, value)
    outside = base / "excluded.md"
    _write(outside, "# Synthetic external note\nEXTERNALPROBETOKEN\n")
    aliases = (root / "wiki/approved", root / "projects/selected")
    for directory in aliases:
        (directory / "external-symlink.md").symlink_to(outside)
    hardlinks = True
    hardlink_reason = ""
    for directory in aliases:
        try:
            os.link(outside, directory / "external-hardlink.md")
        except OSError as exc:
            hardlinks = False
            hardlink_reason = f"Host filesystem could not create a synthetic hardlink: errno {exc.errno}"
    metadata = {
        "version": 1,
        "synthetic": True,
        "hardlinks": hardlinks,
        "hardlink_reason": hardlink_reason,
        "network_baseline": baseline,
        "trusted_source_sha256": hashes,
    }
    if sys.platform == "darwin":
        metadata.update(_macos_host_metadata())
    _write(root / "tools/runtime/probe-fixture.json", json.dumps(metadata))
    return root, metadata


def _macos_host_metadata() -> dict:
    import ctypes

    from macos_processes import AuditKernel

    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    bootstrap = ctypes.c_uint.in_dll(system, "bootstrap_port").value
    self_port = ctypes.c_uint.in_dll(system, "mach_task_self_").value
    for name in ("com.apple.securityd.xpc", "com.apple.SecurityServer"):
        port = ctypes.c_uint()
        if system.bootstrap_look_up(bootstrap, name.encode(), ctypes.byref(port)) != 0:
            raise ValueError(
                "Required public credential-service baseline is unavailable"
            )
        system.mach_port_deallocate(self_port, port.value)
    return {
        "host_audit_session": AuditKernel().token(os.getpid())[6],
        "escape_job_label": "com.vaultlens.probe.escape." + uuid.uuid4().hex,
    }


def _macos_ipc_library():
    import ctypes

    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
    library.shm_open.restype = ctypes.c_int
    library.shm_unlink.argtypes = [ctypes.c_char_p]
    library.sem_open.restype = ctypes.c_void_p
    library.sem_close.argtypes = [ctypes.c_void_p]
    library.sem_unlink.argtypes = [ctypes.c_char_p]
    return library


def _macos_ipc_create() -> dict:
    """Create private synthetic IPC objects before the sandbox session exists."""
    import ctypes

    library = _macos_ipc_library()
    suffix = uuid.uuid4().hex[:16]
    data = {"host_shm": "/vl-shm-" + suffix, "host_sem": "/vl-sem-" + suffix}
    descriptor = library.shm_open(
        data["host_shm"].encode(), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
    )
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Synthetic shared-memory baseline failed")
    try:
        os.ftruncate(descriptor, 4096)
        with mmap.mmap(descriptor, 4096) as mapping:
            canary = b"SYNTHETIC_HOST_IPC_CANARY"
            mapping[: len(canary)] = canary
        semaphore = library.sem_open(
            data["host_sem"].encode(), os.O_CREAT | os.O_EXCL, 0o600, 0
        )
        if semaphore in (None, ctypes.c_void_p(-1).value):
            raise OSError(ctypes.get_errno(), "Synthetic semaphore baseline failed")
        library.sem_close(semaphore)
    except BaseException:
        _macos_ipc_cleanup(data)
        raise
    finally:
        os.close(descriptor)
    return data


def _macos_ipc_cleanup(data: dict) -> None:
    library = _macos_ipc_library()
    library.shm_unlink(data["host_shm"].encode())
    library.sem_unlink(data["host_sem"].encode())


def _macos_job_absent(root: Path) -> dict:
    metadata = json.loads((root / "tools/runtime/probe-fixture.json").read_text())
    service = f"gui/{os.getuid()}/{metadata['escape_job_label']}"
    result = _subprocess(["/bin/launchctl", "print", service])
    absent = result.returncode != 0 and "Could not find service" in result.stderr
    if not absent:
        _subprocess(["/bin/launchctl", "bootout", service])
        confirmation = _subprocess(["/bin/launchctl", "print", service])
        if confirmation.returncode == 0:
            raise ProcessCleanupError("Synthetic escape job cleanup is unconfirmed")
    return {
        "check": "selected-read.macos.job-absent",
        "status": "passed" if absent else "failed",
        "detail": "The trusted host checked the unique synthetic escape job after the workload",
    }


def _denied(operation) -> None:
    try:
        operation()
    except PermissionError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            return
        raise
    except OSError as exc:
        if exc.errno == errno.EROFS:
            return
        raise
    raise AssertionError("Operation succeeded despite the required OS denial")


def _deny_read(path: Path) -> None:
    _denied(path.read_bytes)


def _append(path: Path) -> None:
    with path.open("ab") as stream:
        stream.write(b"UNEXPECTED_PROBE_WRITE\n")


def _deny_write(path: Path) -> None:
    _denied(lambda: _append(path))


def _subprocess(
    command: list[str],
    *,
    stdin: str | None = None,
    env: dict | None = None,
    timeout: float = 5,
) -> subprocess.CompletedProcess:
    """Children inherit this sandbox and process group; their leader is bounded."""
    return subprocess.run(
        command, input=stdin, capture_output=True, text=True, env=env, timeout=timeout
    )


def _shell(path: Path, *, write: bool, allowed: bool) -> None:
    operation = 'printf PROBE >> "$1"' if write else '/bin/cat "$1" >/dev/null'
    result = _subprocess(
        ["/bin/sh", "-c", operation, "probe", str(path)],
        env={**os.environ, "LC_ALL": "C"},
    )
    if allowed:
        assert result.returncode == 0, "Allowed spawned-shell operation failed"
        return
    assert result.returncode != 0, "Spawned shell bypassed the required OS denial"
    diagnostic = result.stderr.casefold()
    assert any(
        value in diagnostic
        for value in (
            "permission denied",
            "operation not permitted",
            "read-only file system",
        )
    ), "Shell failed without evidence of an OS permission denial"


def _allowed_read(path: Path, token: str) -> None:
    assert token in path.read_text(), "Allowed synthetic note was unavailable"


def _scratch(path: Path) -> None:
    path.write_text("Synthetic scratch write\n")
    assert path.read_text() == "Synthetic scratch write\n", (
        "Scratch write/read did not round-trip"
    )


def _profile_write(scope, path: Path) -> None:
    if scope.writable(path):
        _scratch(path)
    else:
        _deny_write(path)


def _profile_note_write(scope, path: Path) -> None:
    if scope.writable(path):
        with path.open("ab") as stream:
            stream.write(b"ALLOWED_SYNTHETIC_WRITE\n")
    else:
        _deny_write(path)


def _profile_read(scope, path: Path, token: str) -> None:
    if scope.readable(path):
        _allowed_read(path, token)
    else:
        _deny_read(path)


def _new_link_escape(
    directory: Path, outside: Path, *, hardlink: bool, metadata: dict
) -> None:
    if hardlink and not metadata["hardlinks"]:
        raise SkippedCheck(metadata["hardlink_reason"])
    alias = directory / ("new-hardlink.md" if hardlink else "new-symlink.md")
    try:
        if hardlink:
            os.link(outside, alias)
        else:
            alias.symlink_to(outside)
    except PermissionError as exc:
        assert exc.errno in {errno.EACCES, errno.EPERM}, (
            "Link creation failed without a permission denial"
        )
        return
    _deny_read(alias)
    _deny_write(alias)


def _old_hardlink(operation, metadata: dict) -> None:
    if not metadata["hardlinks"]:
        raise SkippedCheck(metadata["hardlink_reason"])
    operation()


def _search_results(payload: dict, token: str, *, present: bool) -> None:
    found = any(token in item.get("snippet", "") for item in payload.get("results", []))
    assert found == present, "Scoped search returned the wrong synthetic corpus"


def _search_cli(root: Path) -> None:
    for token, present in (
        ("APPROVEDPROBETOKEN", True),
        ("RESTRICTEDPROBETOKEN", False),
        ("STALEINDEXPROBETOKEN", False),
        ("EXTERNALPROBETOKEN", False),
    ):
        result = _subprocess(["qmd", "search", token, "--json"])
        assert result.returncode == 0, "Scoped qmd CLI could not run"
        _search_results(json.loads(result.stdout), token, present=present)
    status = _subprocess(["qmd", "status", "--json"])
    assert status.returncode == 0, "Scoped qmd status could not run"
    information = json.loads(status.stdout)
    assert (
        information.get("persistent_index") is False
        and information.get("embeddings") is False
    ), "Search reused an index or embedding model"
    denied = _subprocess(
        ["qmd", "get", str(root / "wiki/restricted/note.md"), "--json"]
    )
    assert denied.returncode == 2 and "RESTRICTEDPROBETOKEN" not in denied.stdout, (
        "Scoped qmd get disclosed an excluded document"
    )


def _search_mcp(root: Path) -> None:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    ]
    for identifier, token in enumerate(
        (
            "APPROVEDPROBETOKEN",
            "RESTRICTEDPROBETOKEN",
            "STALEINDEXPROBETOKEN",
            "EXTERNALPROBETOKEN",
        ),
        start=1,
    ):
        requests.append(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": token}},
            }
        )
    for identifier, path in enumerate(
        (
            "wiki/restricted/note.md",
            "raw/review-inbox/consent.md",
            "wiki/approved/external-symlink.md",
            "wiki/approved/external-hardlink.md",
            "../excluded.md",
        ),
        start=5,
    ):
        requests.append(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": "get", "arguments": {"path": path}},
            }
        )
    # Even explicit legacy index environment cannot widen the scoped server.
    env = {
        **os.environ,
        "QMD_INDEX_PATH": str(root / "tools/runtime-state/index/copied-note.md"),
    }
    result = _subprocess(
        [
            sys.executable,
            str(root / "tools/scoped_search.py"),
            "--manifest",
            os.environ["VAULTLENS_RUNTIME_MANIFEST"],
            "mcp",
        ],
        stdin="".join(json.dumps(request) + "\n" for request in requests),
        env=env,
    )
    assert result.returncode == 0, "Scoped MCP server could not run inside the boundary"
    responses = {
        value["id"]: value
        for value in (json.loads(line) for line in result.stdout.splitlines())
    }
    assert set(responses) == set(range(10)), (
        "Scoped MCP did not complete its bounded request batch"
    )
    for identifier, token in enumerate(
        (
            "APPROVEDPROBETOKEN",
            "RESTRICTEDPROBETOKEN",
            "STALEINDEXPROBETOKEN",
            "EXTERNALPROBETOKEN",
        ),
        start=1,
    ):
        response = responses[identifier]["result"]
        assert response.get("isError") is False, (
            "Scoped MCP search returned a tool error"
        )
        _search_results(response["structuredContent"], token, present=identifier == 1)
    for identifier in range(5, 10):
        assert responses[identifier]["result"].get("isError") is True, (
            "Scoped MCP get admitted an excluded or aliased document"
        )


def network_error_kind(exc: BaseException) -> str:
    """Distinguish explicit boundary denials from DNS/transport failures."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, PermissionError) and reason.errno in {
        errno.EACCES,
        errno.EPERM,
    }:
        return "denied"
    description = str(reason).casefold()
    if "tunnel connection failed: 403" in description or any(
        value in description
        for value in (
            "blocked by sandbox",
            "domain is not allowed",
            "domain not allowed",
            "network policy denied",
        )
    ):
        return "denied"
    return "unavailable"


def _public_request() -> dict:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        # Keep the public fixture request on this literal endpoint.
        # Use the runtime's explicit proxy variables, never ambient macOS
        # proxy settings. The host baseline has a minimal, proxy-free env.
        proxies = {
            scheme: os.environ[scheme.upper() + "_PROXY"]
            for scheme in ("http", "https")
            if scheme.upper() + "_PROXY" in os.environ
        }
        for scheme, proxy in proxies.items():
            parsed = urllib.parse.urlsplit(proxy)
            if parsed.hostname == "localhost":
                # The runtime already grants its loopback proxy port. A
                # numeric address avoids a separate local resolver dependency.
                authority = parsed.netloc.rpartition("@")[0]
                endpoint = "127.0.0.1:" + str(parsed.port)
                if authority:
                    endpoint = authority + "@" + endpoint
                proxies[scheme] = parsed._replace(netloc=endpoint).geturl()
        with urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies), NoRedirect
        ).open(PUBLIC_URL, timeout=3) as response:
            response.read(1)
            status = response.status
        addresses = socket.getaddrinfo(PUBLIC_HOST, 443, type=socket.SOCK_STREAM)
        public_ip = next(
            (
                item[4][0]
                for item in addresses
                if ipaddress.ip_address(item[4][0]).is_global
            ),
            None,
        )
        return {"status": "passed", "http_status": status, "public_ip": public_ip}
    except (OSError, urllib.error.URLError) as exc:
        return {
            "status": network_error_kind(exc),
            "detail": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def public_baseline() -> dict:
    command = [
        "/usr/bin/curl",
        "--disable",
        "--noproxy",
        "*",
        "--head",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "3",
        "--max-time",
        "4",
        "--output",
        os.devnull,
        "--write-out",
        '{"http_status":%{http_code},"public_ip":"%{remote_ip}"}',
        PUBLIC_URL,
    ]
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            stdout, _stderr = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            return {
                "status": "skipped",
                "detail": "Public connectivity baseline exceeded five seconds",
            }
        if child.returncode:
            return {
                "status": "skipped",
                "detail": "Public connectivity baseline subprocess failed",
            }
        result = json.loads(stdout)
        address = result.get("public_ip")
        if not isinstance(address, str) or not ipaddress.ip_address(address).is_global:
            return {
                "status": "skipped",
                "detail": "Public baseline did not establish a public socket address",
            }
        result["status"] = "passed"
        return result
    finally:
        terminate_group(child, grace=0.1)


def _require_baseline(metadata: dict) -> dict:
    baseline = metadata["network_baseline"]
    if baseline.get("status") != "passed":
        raise SkippedCheck(
            baseline.get("detail", "Public connectivity baseline is unavailable")
        )
    return baseline


def _network_http(metadata: dict, *, shell: bool = False) -> None:
    _require_baseline(metadata)
    try:
        if shell:
            command = [
                "/bin/sh",
                "-c",
                'exec "$1" "$2" --network-baseline',
                "probe",
                str(Path(sys.executable).resolve()),
                str(Path(__file__).resolve()),
            ]
        else:
            command = [
                str(Path(sys.executable).resolve()),
                str(Path(__file__).resolve()),
                "--network-baseline",
            ]
        result = _subprocess(command, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise SkippedCheck(
            "Public HTTP attempt exceeded five seconds without explicit denial evidence"
        ) from exc
    assert result.returncode == 0, "Synthetic public request helper failed"
    response = json.loads(result.stdout)
    if response.get("status") == "passed":
        raise AssertionError("Unapproved public HTTPS access succeeded")
    if response.get("status") != "denied":
        raise SkippedCheck(
            "Network failed without explicit boundary denial evidence: "
            + response.get("detail", "transport failure")
        )


def _network_socket(metadata: dict) -> None:
    baseline = _require_baseline(metadata)
    public_ip = baseline.get("public_ip")
    if not public_ip or not ipaddress.ip_address(public_ip).is_global:
        raise SkippedCheck("Public baseline did not establish a public socket address")
    family = socket.AF_INET6 if ":" in public_ip else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as stream:
            stream.settimeout(3)
            stream.connect((public_ip, 443))
    except OSError as exc:
        if network_error_kind(exc) == "denied":
            return
        raise SkippedCheck(
            f"Direct socket failed without explicit boundary denial evidence: errno {exc.errno}"
        ) from exc
    raise AssertionError("Unapproved direct public socket access succeeded")


def _clean_provider_environment(run: Path) -> None:
    excluded = (
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "SSH_AUTH_SOCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    )
    assert not any(key in os.environ for key in excluded), (
        "Probe inherited provider state or ambient credentials"
    )
    assert Path(os.environ["HOME"]) == run / "home", (
        "Probe did not use a disposable home"
    )
    assert not (run / "provider").exists(), (
        "Probe created provider authentication state"
    )


def _macos_guard_checks(metadata: dict) -> list[tuple[str, object]]:
    """Attempt finite escapes using public kernel metadata and empty jobs only."""
    import ctypes

    class Mask(ctypes.Structure):
        _fields_ = [("success", ctypes.c_uint), ("failure", ctypes.c_uint)]

    class Terminal(ctypes.Structure):
        _fields_ = [
            ("port", ctypes.c_int),
            ("kind", ctypes.c_uint),
            ("address", ctypes.c_uint * 4),
        ]

    class AuditInfo(ctypes.Structure):
        _fields_ = [
            ("user", ctypes.c_uint),
            ("mask", Mask),
            ("terminal", Terminal),
            ("session", ctypes.c_uint),
            ("flags", ctypes.c_uint64),
        ]

    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    information = AuditInfo()

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    def session():
        require(
            system.getaudit_addr(ctypes.byref(information), ctypes.sizeof(information))
            == 0,
            "Cannot read synthetic process audit session",
        )
        return information.session

    before = session()
    require(before != metadata["host_audit_session"], "Workload inherited host session")

    def reassign():
        session()
        information.session = 0xFFFFFFFF
        ctypes.set_errno(0)
        result = system.setaudit_addr(
            ctypes.byref(information), ctypes.sizeof(information)
        )
        require(
            result == -1 and ctypes.get_errno() == errno.EPERM,
            "Audit session assignment did not return the required permission denial",
        )

    def foreign_port():
        port = ctypes.c_uint()
        ctypes.set_errno(0)
        result = system.audit_session_port(
            metadata["host_audit_session"], ctypes.byref(port)
        )
        require(
            result == -1 and ctypes.get_errno() == errno.EPERM,
            "Foreign audit session port did not return the required permission denial",
        )

    def operation_denied(operation):
        require(
            system.sandbox_check(os.getpid(), operation.encode(), 0) > 0,
            "Required sandbox operation was permitted: " + operation,
        )

    def service_denied(name):
        bootstrap = ctypes.c_uint.in_dll(system, "bootstrap_port").value
        destination = ctypes.c_uint()
        result = system.bootstrap_look_up(
            bootstrap, name.encode(), ctypes.byref(destination)
        )
        require(result == 1100, "Existing credential service lookup was not denied")

    def job_denied():
        target = Path(os.environ["TMPDIR"]) / "synthetic-escape.plist"
        target.write_bytes(
            plistlib.dumps(
                {
                    "Label": metadata["escape_job_label"],
                    "ProgramArguments": ["/bin/sleep", "2"],
                    "SessionCreate": True,
                    "RunAtLoad": True,
                    "KeepAlive": False,
                }
            )
        )
        result = _subprocess(
            ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)],
            timeout=4,
        )
        require(result.returncode != 0, "Synthetic external job bootstrap succeeded")

    def shared_memory(write):
        library = _macos_ipc_library()
        descriptor = library.shm_open(
            metadata["host_shm"].encode(), os.O_RDWR if write else os.O_RDONLY, 0
        )
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), "Synthetic host shared-memory access")
        try:
            with mmap.mmap(
                descriptor,
                4096,
                access=mmap.ACCESS_WRITE if write else mmap.ACCESS_READ,
            ) as mapping:
                if write:
                    mapping[0] = ord("X")
                else:
                    mapping[:24]
        finally:
            os.close(descriptor)

    def semaphore_open():
        library = _macos_ipc_library()
        pointer = library.sem_open(metadata["host_sem"].encode(), 0)
        if pointer in (None, ctypes.c_void_p(-1).value):
            raise OSError(ctypes.get_errno(), "Synthetic host semaphore access")
        library.sem_close(pointer)

    return [
        ("macos.audit-reassignment", reassign),
        ("macos.foreign-session-port", foreign_port),
        (
            "macos.security-session-create",
            lambda: require(
                security.SessionCreate(0, 0) != 0, "Security session creation succeeded"
            ),
        ),
        ("macos.system-audit-denied", lambda: operation_denied("system-audit")),
        ("macos.job-creation-denied", lambda: operation_denied("job-creation")),
        (
            "macos.securityd-lookup-denied",
            lambda: service_denied("com.apple.securityd.xpc"),
        ),
        (
            "macos.securityserver-lookup-denied",
            lambda: service_denied("com.apple.SecurityServer"),
        ),
        ("macos.job-bootstrap-denied", job_denied),
        (
            "macos.session-unchanged",
            lambda: require(session() == before, "Audit session changed"),
        ),
        (
            "macos.node-preload-removed",
            lambda: require(
                "NODE_OPTIONS" not in os.environ, "Supervisor preload reached workload"
            ),
        ),
        (
            "macos.host-shared-memory-read",
            lambda: _denied(lambda: shared_memory(False)),
        ),
        (
            "macos.host-shared-memory-write",
            lambda: _denied(lambda: shared_memory(True)),
        ),
        ("macos.host-semaphore-open", lambda: _denied(semaphore_open)),
    ]


def child_checks(scope, metadata: dict, case: str) -> list[tuple[str, object]]:
    root = scope.root
    run = Path(os.environ["VAULTLENS_RUNTIME_MANIFEST"]).parent
    outside = root.parent / "excluded.md"
    approved = root / "wiki/approved/note.md"
    restricted = root / "wiki/restricted/note.md"
    future = root / "wiki/restricted" / ("future-" + case + ".md")
    raw = root / "raw/sources/source.md"
    other = root / "wiki/unselected/note.md"
    private = root / "projects/selected/private/note.md"
    private_future = root / "projects/selected/private" / ("future-" + case + ".md")
    sibling = root / "projects/sibling/note.md"
    aliases = root / (
        "projects/selected" if case == "project-write" else "wiki/approved"
    )
    symlink = aliases / "external-symlink.md"
    hardlink = aliases / "external-hardlink.md"
    search = ScopedSearch(scope)
    checks = [
        ("active-boundary", local_runtime.verify_active_boundary),
        ("clean-provider-environment", lambda: _clean_provider_environment(run)),
        ("notes.python-read", lambda: _allowed_read(approved, "APPROVEDPROBETOKEN")),
        ("notes.shell-read", lambda: _shell(approved, write=False, allowed=True)),
        ("notes.profile-write", lambda: _profile_note_write(scope, approved)),
        (
            "notes.shell-profile-write",
            lambda: _shell(approved, write=True, allowed=scope.writable(approved)),
        ),
        (
            "selection.other-note-read",
            lambda: _profile_read(scope, other, "UNSELECTEDPROBETOKEN"),
        ),
        (
            "selection.other-note-shell-read",
            lambda: _shell(other, write=False, allowed=scope.readable(other)),
        ),
        ("scratch.python-write", lambda: _scratch(run / "scratch/python-write.txt")),
        (
            "scratch.shell-write",
            lambda: _shell(run / "scratch/shell-write.txt", write=True, allowed=True),
        ),
        ("excluded.python-read", lambda: _deny_read(outside)),
        ("excluded.shell-read", lambda: _shell(outside, write=False, allowed=False)),
        ("excluded.python-write", lambda: _deny_write(outside)),
        ("excluded.shell-write", lambda: _shell(outside, write=True, allowed=False)),
        ("restricted.python-read", lambda: _deny_read(restricted)),
        (
            "restricted.shell-read",
            lambda: _shell(restricted, write=False, allowed=False),
        ),
        ("restricted.python-write", lambda: _deny_write(restricted)),
        (
            "restricted.shell-write",
            lambda: _shell(restricted, write=True, allowed=False),
        ),
        ("denied.future-child-read", lambda: _deny_read(future)),
        ("denied.future-child-write", lambda: _deny_write(future)),
        (
            "consent.python-read",
            lambda: _deny_read(root / "raw/review-inbox/consent.md"),
        ),
        (
            "stale-index.python-read",
            lambda: _deny_read(root / "tools/runtime-state/index/copied-note.md"),
        ),
        (
            "raw.profile-read",
            lambda: (
                _allowed_read(raw, "RAWPROBETOKEN")
                if scope.readable(raw)
                else _deny_read(raw)
            ),
        ),
        ("raw.python-write", lambda: _deny_write(raw)),
        ("raw.shell-write", lambda: _shell(raw, write=True, allowed=False)),
        ("tools.python-write", lambda: _deny_write(root / "tools/probe-canary.txt")),
        (
            "tools.shell-write",
            lambda: _shell(root / "tools/probe-canary.txt", write=True, allowed=False),
        ),
        ("instructions.python-write", lambda: _deny_write(root / "AGENTS.md")),
        (
            "instructions.shell-write",
            lambda: _shell(root / "wiki/AGENTS.md", write=True, allowed=False),
        ),
        (
            "obsidian.python-write",
            lambda: _deny_write(root / ".obsidian/probe-layout.json"),
        ),
        ("git.python-write", lambda: _deny_write(root / ".git/probe-metadata")),
        (
            "wiki.profile-write",
            lambda: _profile_write(scope, root / "wiki/approved/new-note.md"),
        ),
        (
            "project.profile-write",
            lambda: _profile_write(scope, root / "projects/selected/new-note.md"),
        ),
        ("sibling.python-read", lambda: _deny_read(sibling)),
        ("sibling.python-write", lambda: _deny_write(sibling)),
        ("sibling.shell-write", lambda: _shell(sibling, write=True, allowed=False)),
        ("project.denied-child-read", lambda: _deny_read(private)),
        ("project.denied-child-write", lambda: _deny_write(private)),
        ("project.future-denied-child-read", lambda: _deny_read(private_future)),
        ("project.future-denied-child-write", lambda: _deny_write(private_future)),
        ("symlink.python-read", lambda: _deny_read(symlink)),
        ("symlink.shell-read", lambda: _shell(symlink, write=False, allowed=False)),
        ("symlink.python-write", lambda: _deny_write(symlink)),
        ("symlink.shell-write", lambda: _shell(symlink, write=True, allowed=False)),
        (
            "hardlink.python-read",
            lambda: _old_hardlink(lambda: _deny_read(hardlink), metadata),
        ),
        (
            "hardlink.shell-read",
            lambda: _old_hardlink(
                lambda: _shell(hardlink, write=False, allowed=False), metadata
            ),
        ),
        (
            "hardlink.python-write",
            lambda: _old_hardlink(lambda: _deny_write(hardlink), metadata),
        ),
        (
            "hardlink.shell-write",
            lambda: _old_hardlink(
                lambda: _shell(hardlink, write=True, allowed=False), metadata
            ),
        ),
        (
            "symlink.new-escape",
            lambda: _new_link_escape(
                aliases, outside, hardlink=False, metadata=metadata
            ),
        ),
        (
            "hardlink.new-escape",
            lambda: _new_link_escape(
                aliases, outside, hardlink=True, metadata=metadata
            ),
        ),
        (
            "search.approved",
            lambda: _search_results(
                search.search({"query": "APPROVEDPROBETOKEN"}),
                "APPROVEDPROBETOKEN",
                present=True,
            ),
        ),
        (
            "search.restricted",
            lambda: _search_results(
                search.search({"query": "RESTRICTEDPROBETOKEN"}),
                "RESTRICTEDPROBETOKEN",
                present=False,
            ),
        ),
        (
            "search.stale-index",
            lambda: _search_results(
                search.search({"query": "STALEINDEXPROBETOKEN"}),
                "STALEINDEXPROBETOKEN",
                present=False,
            ),
        ),
        ("search.cli", lambda: _search_cli(root)),
        (
            "search.selection",
            lambda: _search_results(
                search.search({"query": "UNSELECTEDPROBETOKEN"}),
                "UNSELECTEDPROBETOKEN",
                present=scope.readable(other),
            ),
        ),
        ("search.mcp", lambda: _search_mcp(root)),
    ]
    if case == "selected-read":
        checks.extend(
            [
                ("network.http", lambda: _network_http(metadata)),
                ("network.direct-socket", lambda: _network_socket(metadata)),
                ("network.shell-http", lambda: _network_http(metadata, shell=True)),
            ]
        )
        if sys.platform == "darwin":
            checks.extend(_macos_guard_checks(metadata))
    return checks


def run_child(case: str) -> int:
    scope = local_runtime.verify_active_boundary()
    metadata = json.loads((scope.root / "tools/runtime/probe-fixture.json").read_text())
    if (
        metadata.get("synthetic") is not True
        or metadata.get("version") != 1
        or scope.name != "probe-" + case
    ):
        raise ValueError(
            "Probe child requires the trusted disposable synthetic fixture"
        )
    output = Path(os.environ["TMPDIR"]) / "probe-results.json"
    checks = child_checks(scope, metadata, case)
    results = []
    for name, operation in checks:
        try:
            operation()
            item = {"check": case + "." + name, "status": "passed"}
        except SkippedCheck as exc:
            item = {"check": case + "." + name, "status": "skipped", "detail": str(exc)}
        except Exception as exc:
            item = {
                "check": case + "." + name,
                "status": "failed",
                "detail": f"{type(exc).__name__}: {str(exc)[:320]}",
            }
        results.append(item)
        output.write_text(json.dumps(results))
    return 1 if any(item["status"] == "failed" for item in results) else 0


def _synthetic_scope():
    scope = local_runtime.verify_active_boundary()
    metadata = json.loads((scope.root / "tools/runtime/probe-fixture.json").read_text())
    if metadata.get("synthetic") is not True or metadata.get("version") != 1:
        raise ValueError("Lifecycle probe requires the trusted synthetic fixture")
    return scope, Path(os.environ["VAULTLENS_RUNTIME_MANIFEST"]).parent


def lifecycle_heartbeat(token: str) -> int:
    _scope, run = _synthetic_scope()
    kind = token.rsplit("-", 1)[-1]
    if kind not in {"attached", "detached"} or token != run.name + "-" + kind:
        raise ValueError("Invalid synthetic lifecycle token")
    heartbeat = run / "scratch" / ("lifecycle-" + kind + "-heartbeat.json")
    pending = heartbeat.with_suffix(".pending")
    started = time.monotonic()
    deadline = started + 45
    count = 0
    while time.monotonic() < deadline:
        count += 1
        pending.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "token": token,
                    "count": count,
                    "started": started,
                    "expires": deadline,
                }
            )
        )
        pending.replace(heartbeat)
        time.sleep(0.05)
    return 0


def lifecycle_spawner(kind: str, *, cancellation: str | None = None) -> int:
    scope, run = _synthetic_scope()
    token = run.name + "-" + kind
    marker = run / "scratch" / ("lifecycle-" + kind + "-spawn.json")
    heartbeat = run / "scratch" / ("lifecycle-" + kind + "-heartbeat.json")
    command = [
        str(Path(sys.executable).resolve()),
        str(scope.root / "tools/runtime/probe.py"),
        "--lifecycle-heartbeat",
        token,
    ]
    try:
        child = subprocess.Popen(
            command,
            start_new_session=kind == "detached",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except PermissionError as exc:
        if exc.errno not in {errno.EACCES, errno.EPERM}:
            raise
        marker.write_text(
            json.dumps({"status": "denied", "errno": exc.errno, "token": token})
        )
        if cancellation in {"outer", "inner"}:
            time.sleep(45)
        return 0
    deadline = time.monotonic() + 2
    while (
        not heartbeat.is_file() and time.monotonic() < deadline and child.poll() is None
    ):
        time.sleep(0.02)
    try:
        group = os.getpgid(child.pid)
    except ProcessLookupError:
        group = None
    marker.write_text(
        json.dumps(
            {
                "status": "spawned" if heartbeat.is_file() else "failed",
                "pid": child.pid,
                "group_id": group,
                "token": token,
                "script": str(scope.root / "tools/runtime/probe.py"),
            }
        )
    )
    if cancellation in {"outer", "inner"} and heartbeat.is_file():
        time.sleep(45)
    # Intentionally finish normally while the finite public child is active.
    # The outer launcher must own its lifecycle, including a detached session.
    return 0 if heartbeat.is_file() else 2


def inner_lifecycle(kind: str, *, cancellation: bool = False) -> int:
    scope, _run = _synthetic_scope()
    script = scope.root / "tools/agents/wiki-agent.py"
    specification = importlib.util.spec_from_file_location(
        "synthetic_wiki_agent", script
    )
    if not specification or not specification.loader:
        raise ValueError("Synthetic wiki launcher import failed")
    launcher = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(launcher)
    command = [
        str(Path(sys.executable).resolve()),
        str(scope.root / "tools/runtime/probe.py"),
        "--lifecycle-spawner",
        kind,
    ]
    if cancellation:
        command.extend(["--cancel-case", "inner"])
    # The actual invocation/error/cleanup path executes a public fixture command
    # instead of a native provider. Its OS guard is never mocked or bypassed.
    launcher.build_cli_command = lambda *_args, **_kwargs: command
    return launcher.invoke_agent(
        "search",
        "synthetic-lifecycle",
        "",
        None,
        "Synthetic lifecycle check only",
        "",
        [],
        timeout=3 if cancellation else 20,
    )


@contextlib.contextmanager
def _bounded_launcher(seconds: float, *, cancellation_marker: Path | None = None):
    previous = signal.getsignal(signal.SIGALRM)
    original_termination = signal.getsignal(signal.SIGTERM)
    deadline = time.monotonic() + seconds

    def expired(_signum, _frame):
        if time.monotonic() >= deadline:
            raise TimeoutError("Synthetic lifecycle launcher exceeded its deadline")
        if cancellation_marker is not None and cancellation_marker.is_file():
            handler = signal.getsignal(signal.SIGTERM)
            if callable(handler) and handler is not original_termination:
                # Exercise the trusted outer launcher's interruption handler.
                # SRT maps its own child's TERM/INT to exit 0, which cannot
                # represent a user cancelling this host launcher.
                signal.raise_signal(signal.SIGTERM)

    signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(
        signal.ITIMER_REAL,
        0.05 if cancellation_marker is not None else seconds,
        0.05 if cancellation_marker is not None else 0,
    )
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if timer[0] or timer[1]:
            signal.setitimer(signal.ITIMER_REAL, *timer)


def _owned_fixture_process(record: dict) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        raise ProcessCleanupError(
            "Invalid synthetic descendant identity", group_id=record.get("group_id")
        )
    result = _subprocess(["/bin/ps", "-p", str(pid), "-o", "stat=,command="], timeout=2)
    value = result.stdout.strip()
    if result.returncode in {0, 1} and not value:
        return False
    if result.returncode:
        raise ProcessCleanupError(
            "Cannot inspect the synthetic descendant", group_id=record.get("group_id")
        )
    state, _, command = value.partition(" ")
    if state.startswith("Z"):
        return False
    if not all(
        value in command
        for value in (record["script"], record["token"], "--lifecycle-heartbeat")
    ):
        raise ProcessCleanupError(
            "Cannot confirm the synthetic descendant identity before cleanup",
            group_id=record.get("group_id"),
        )
    return True


def _cleanup_fixture_process(record: dict) -> None:
    if not _owned_fixture_process(record):
        return
    for signum, grace in ((signal.SIGTERM, 0.15), (signal.SIGKILL, 0.2)):
        try:
            os.kill(record["pid"], signum)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise ProcessCleanupError(
                "Synthetic descendant cleanup was denied",
                group_id=record.get("group_id"),
            ) from exc
        time.sleep(grace)
        if not _owned_fixture_process(record):
            return
    raise ProcessCleanupError(
        "Synthetic descendant cleanup remains unconfirmed",
        group_id=record.get("group_id"),
    )


def _stopped_heartbeat(record: dict, heartbeat: Path) -> None:
    if record.get("status") == "denied":
        assert record.get("errno") in {errno.EACCES, errno.EPERM}, (
            "Missing OS descendant-creation denial evidence"
        )
        return
    assert record.get("status") == "spawned" and heartbeat.is_file(), (
        "Synthetic heartbeat child did not establish its start handshake"
    )
    first = json.loads(heartbeat.read_text())
    assert (
        first.get("pid") == record["pid"] and first.get("token") == record["token"]
    ), "Synthetic heartbeat identity did not match the launched child"
    assert (
        first["expires"] - first["started"] >= 40
        and time.monotonic() + 2 < first["expires"]
    ), "Finite child expiry cannot be used as cleanup evidence"
    time.sleep(0.25)
    second = json.loads(heartbeat.read_text())
    assert second["count"] == first["count"] and not _owned_fixture_process(record), (
        "Synthetic descendant survived launcher completion or kept writing"
    )


def _synthetic_gate_blocks(root: Path, scope) -> None:
    gate = root / "tools/runtime-state/cancellation-unconfirmed.json"
    assert gate.is_file() and not gate.is_symlink(), (
        "Cancellation did not persist the synthetic vault gate"
    )
    try:
        with local_runtime.prepared_run(
            scope, None, snapshot=False, require_verification=False
        ):
            pass
    except ValueError as exc:
        assert "unconfirmed" in str(exc).casefold(), (
            "Next launch failed for a different reason than the cancellation gate"
        )
    else:
        raise AssertionError("Persistent cancellation gate admitted another launch")


def _lifecycle_case(root: Path, boundary: str, kind: str, cancellation: bool) -> dict:
    name = f"lifecycle.{boundary}.{kind}.{'cancellation' if cancellation else 'normal-success'}"
    scope = resolve_scope(root, "probe-selected-read")
    item = {
        "check": name,
        "status": "passed",
        "detail": "Tracked public descendant stopped before expiry; no writes after return",
    }
    expected_error = None
    try:
        with local_runtime.prepared_run(
            scope, None, snapshot=False, require_verification=False
        ) as (executable, run, env):
            local_runtime._verify_preflight(executable, run, env, root)
            marker = run / "scratch" / ("lifecycle-" + kind + "-spawn.json")
            heartbeat = run / "scratch" / ("lifecycle-" + kind + "-heartbeat.json")
            command = [
                str(Path(sys.executable).resolve()),
                str(root / "tools/runtime/probe.py"),
                "--inner-lifecycle" if boundary == "inner" else "--lifecycle-spawner",
                kind,
            ]
            if cancellation:
                command.extend(
                    ["--inner-cancel"]
                    if boundary == "inner"
                    else ["--cancel-case", "outer"]
                )
            record = None
            result = None
            try:
                try:
                    with _bounded_launcher(
                        20,
                        cancellation_marker=marker
                        if cancellation and boundary == "outer"
                        else None,
                    ):
                        result = local_runtime._execute_prepared(
                            command, executable, run, env, root
                        )
                except ProcessCleanupError as exc:
                    expected_error = exc
                if not marker.is_file():
                    raise ProcessCleanupError(
                        "Synthetic lifecycle start handshake missing; descendants cannot be confirmed",
                        group_id=expected_error.group_id if expected_error else None,
                    )
                record = json.loads(marker.read_text())
                if not cancellation:
                    assert result == 0 and expected_error is None, (
                        "Synthetic lifecycle parent did not finish normally"
                    )
                _stopped_heartbeat(record, heartbeat)
                if cancellation:
                    assert expected_error is not None, (
                        "Cancellation completed without the required unconfirmed-cleanup gate"
                    )
            except (
                AssertionError,
                OSError,
                ValueError,
                subprocess.SubprocessError,
            ) as exc:
                item = {"check": name, "status": "failed", "detail": str(exc)[:500]}
            finally:
                if record is None and marker.is_file():
                    record = json.loads(marker.read_text())
                if record and record.get("status") != "denied":
                    _cleanup_fixture_process(record)
            if expected_error is not None:
                raise expected_error
    except ProcessCleanupError as exc:
        known = str(exc) in {
            "Outer runtime interrupted; provider/tool cleanup requires operator verification",
            "Agent reported unconfirmed descendant cancellation",
        }
        if not cancellation or exc is not expected_error or not known:
            raise
        try:
            _synthetic_gate_blocks(root, scope)
        except (AssertionError, OSError, ValueError) as error:
            item = {"check": name, "status": "failed", "detail": str(error)[:500]}
        else:
            if item["status"] == "passed":
                item["detail"] += "; persisted gate refused the next launch"
        # The only cleared gate belongs to this disposable synthetic vault,
        # after its exact recorded fixture child was independently stopped.
        # Ordinary real-vault gates are never removed by the probe.
        (root / "tools/runtime-state/cancellation-unconfirmed.json").unlink()
    return item


def lifecycle_checks(root: Path) -> Iterator[dict]:
    for case in LIFECYCLE_CASES:
        yield _lifecycle_case(root, *case)


def run_case(root: Path, case: str) -> list[dict]:
    scope = resolve_scope(
        root, "probe-" + case, project="selected" if case == "project-write" else None
    )
    results = []
    with local_runtime.prepared_run(
        scope, None, snapshot=False, require_verification=False
    ) as (
        executable,
        run,
        env,
    ):
        try:
            local_runtime._verify_preflight(executable, run, env, root)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return [
                {
                    "check": case + ".preflight",
                    "status": "failed",
                    "detail": str(exc)[:500],
                },
                *(
                    {
                        "check": name,
                        "status": "skipped",
                        "detail": "Required real-runtime preflight failed; child checks were not started",
                    }
                    for name in _expected(case)
                ),
            ]
        # This file did not exist when permissions were compiled. Denials must
        # apply to later descendants, not only enumerated current documents.
        _write(
            root / "wiki/restricted" / ("future-" + case + ".md"),
            "RESTRICTEDPROBETOKEN\n",
        )
        _write(
            root / "projects/selected/private" / ("future-" + case + ".md"),
            "RESTRICTEDPROBETOKEN\n",
        )
        wrapped = local_runtime.runtime_command(
            executable,
            run / "settings.json",
            [
                str(Path(sys.executable).resolve()),
                str(root / "tools/runtime/probe.py"),
                "--inside",
                case,
            ],
        )
        child = launch_supervised(
            wrapped,
            run=run,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        problem = ""
        escape_job = None
        try:
            try:
                _stdout, stderr = child.communicate(timeout=45)
                if child.returncode not in {0, 1}:
                    problem = (
                        "Synthetic runtime child exited unexpectedly: " + stderr[-500:]
                    )
            except subprocess.TimeoutExpired:
                problem = "Synthetic runtime child exceeded its 45-second bound"
        finally:
            try:
                terminate_group(child)
            finally:
                if case == "selected-read" and sys.platform == "darwin":
                    escape_job = _macos_job_absent(root)
        result_path = run / "scratch/probe-results.json"
        if result_path.is_file():
            results = json.loads(result_path.read_text())
        if escape_job:
            results.append(escape_job)
        delivered = {item["check"] for item in results}
        if problem:
            results.append(
                {
                    "check": case + ".child-process",
                    "status": "failed",
                    "detail": problem,
                }
            )
        for name in _expected(case):
            if name not in delivered:
                results.append(
                    {
                        "check": name,
                        "status": "skipped",
                        "detail": "Synthetic child did not finish this check",
                    }
                )
    return results


def probe(source: Path = ROOT) -> dict:
    """Run real SRT or fail before creating fixtures; never accept an emulator."""
    source = source.resolve()
    report: JsonObject = {
        "version": 1,
        "runtime_version": local_runtime.SRT_VERSION,
        "os_isolation_verified": False,
        "provider_file_tools_verified": False,
        "checks": [],
    }
    try:
        verification.invalidate_verified_runtime(source)
        quarantine = source / "tools/runtime-state/cancellation-unconfirmed.json"
        if quarantine.exists() or quarantine.is_symlink():
            raise ValueError(
                "Previous cancellation remains unconfirmed; inspect tools/runtime-state/cancellation-unconfirmed.json before another probe"
            )
        executable = local_runtime.runtime_executable(source)
        report["runtime_fingerprint"] = verification.fingerprint_runtime(
            source, local_runtime.SRT_VERSION
        )
    except (OSError, ValueError) as exc:
        report["checks"] = [
            {"check": "runtime.prerequisite", "status": "failed", "detail": str(exc)},
            *(
                {
                    "check": name,
                    "status": "skipped",
                    "detail": "Pinned runtime unavailable; no fixture or provider process was created",
                }
                for name in expected_checks()
                if name != "runtime.prerequisite"
            ),
        ]
        return report
    report["runtime_executable"] = str(executable)
    report["checks"].append({"check": "runtime.prerequisite", "status": "passed"})
    temporary = None
    retain = False
    ipc = None
    try:
        baseline = public_baseline()
        temporary = Path(
            tempfile.mkdtemp(prefix="vaultlens-isolation-probe-")
        ).resolve()
        try:
            root, metadata = make_fixture(source, Path(temporary), baseline)
            if sys.platform == "darwin":
                ipc = _macos_ipc_create()
                metadata.update(ipc)
                _write(root / "tools/runtime/probe-fixture.json", json.dumps(metadata))
            report["trusted_source_sha256"] = metadata["trusted_source_sha256"]
            outside = root.parent / "excluded.md"
            before = outside.read_bytes()
            for case in CASE_NAMES:
                report["checks"].extend(run_case(root, case))
                report["checks"].append(
                    {
                        "check": case + ".outside-content-unchanged",
                        "status": "passed"
                        if outside.read_bytes() == before
                        else "failed",
                        "detail": "Compared the synthetic external file after the real-runtime checks",
                    }
                )
            report["checks"].extend(lifecycle_checks(root))
        except ProcessCleanupError:
            retain = True
            raise
    except ProcessCleanupError as exc:
        report["checks"].append(
            {
                "check": "probe.cancellation",
                "status": "failed",
                "detail": str(exc)[:500],
            }
        )
        if temporary:
            report["fixture_retained"] = str(temporary)
        state = source / "tools/runtime-state"
        try:
            local_runtime._private_directory(state, source)
            marker = state / "cancellation-unconfirmed.json"
            data = {
                "kind": "synthetic-isolation-probe",
                "group_id": exc.group_id,
                "reason": str(exc),
                "fixture": str(temporary) if temporary else None,
            }
            descriptor = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w") as stream:
                json.dump(data, stream, indent=2)
                stream.write("\n")
            report["cancellation_gate"] = str(marker)
        except FileExistsError:
            report["cancellation_gate"] = str(state / "cancellation-unconfirmed.json")
        except (OSError, ValueError) as error:
            report["checks"].append(
                {
                    "check": "probe.cancellation-gate",
                    "status": "failed",
                    "detail": str(error)[:500],
                }
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report["checks"].append(
            {"check": "probe.harness", "status": "failed", "detail": str(exc)[:500]}
        )
    finally:
        if ipc:
            _macos_ipc_cleanup(ipc)
        if temporary and not retain:
            shutil.rmtree(temporary)
    delivered = {item["check"] for item in report["checks"]}
    for name in expected_checks():
        if name not in delivered:
            report["checks"].append(
                {
                    "check": name,
                    "status": "skipped",
                    "detail": "The probe stopped before this check",
                }
            )
    report["os_isolation_verified"] = all(
        item["status"] == "passed" for item in report["checks"]
    )
    if report["os_isolation_verified"]:
        try:
            report["verification_receipt"] = str(
                verification.record_verified_probe(
                    source, local_runtime.SRT_VERSION, report, expected_checks()
                )
            )
        except (OSError, ValueError) as exc:
            report["os_isolation_verified"] = False
            report["checks"].append(
                {
                    "check": "receipt.record",
                    "status": "failed",
                    "detail": str(exc)[:500],
                }
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Checkout with reviewed tooling and the installed pinned runtime",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--inside", choices=CASE_NAMES, help=argparse.SUPPRESS)
    parser.add_argument(
        "--lifecycle-spawner", choices=("attached", "detached"), help=argparse.SUPPRESS
    )
    parser.add_argument("--lifecycle-heartbeat", help=argparse.SUPPRESS)
    parser.add_argument(
        "--cancel-case", choices=("outer", "inner"), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--inner-lifecycle", choices=("attached", "detached"), help=argparse.SUPPRESS
    )
    parser.add_argument("--inner-cancel", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--network-baseline", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if args.network_baseline:
        print(json.dumps(_public_request()))
        return 0
    if args.inside:
        return run_child(args.inside)
    if args.lifecycle_heartbeat:
        return lifecycle_heartbeat(args.lifecycle_heartbeat)
    if args.lifecycle_spawner:
        return lifecycle_spawner(args.lifecycle_spawner, cancellation=args.cancel_case)
    if args.inner_lifecycle:
        return inner_lifecycle(args.inner_lifecycle, cancellation=args.inner_cancel)
    # Native fixture children inherit file descriptor 1. Keep their diagnostics
    # outside the JSON stream as well as Python-level diagnostic writes.
    if args.json:
        sys.stdout.flush()
        saved_stdout = os.dup(sys.stdout.fileno())
        try:
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            report = probe(args.root)
            sys.stdout.flush()
        finally:
            os.dup2(saved_stdout, sys.stdout.fileno())
            os.close(saved_stdout)
    else:
        report = probe(args.root)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report["checks"]:
            print(
                f"{item['status'].upper():7} {item['check']}"
                + (": " + item["detail"] if item.get("detail") else "")
            )
        counts = {
            status: sum(item["status"] == status for item in report["checks"])
            for status in ("passed", "failed", "skipped")
        }
        print(
            f"Checks: {counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped. OS isolation {'verified' if report['os_isolation_verified'] else 'unverified'}; provider file tools unverified."
        )
    return 0 if report["os_isolation_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
