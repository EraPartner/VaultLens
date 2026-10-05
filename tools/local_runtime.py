#!/usr/bin/env python3
"""Run native agents behind a whole-process, provider-independent boundary.

No container or unsandboxed fallback exists. The pinned standalone runtime owns
the process boundary; profiles own permissions; native adapters own CLI syntax.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pwd
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, NoReturn, TypedDict, cast
from process_control import (
    ProcessCleanupError,
    check_process_records,
    launch_supervised,
    terminate_group,
)
from runtime_verification import require_verified_runtime
from runtime_maintenance import shared_runtime

from local_access import (
    FORBIDDEN_DIRS,
    PROTECTED_NAMES,
    ROOT,
    SECRET_NAMES,
    RunScope,
    default_profile,
    load_policy,
    resolve_scope,
)

if TYPE_CHECKING:
    from run_reports import Recorder


class FilesystemSettings(TypedDict):
    denyRead: list[str]
    allowRead: list[str]
    allowWrite: list[str]
    denyWrite: list[str]


class NetworkSettings(TypedDict):
    allowedDomains: list[str]
    deniedDomains: list[str]
    allowUnixSockets: list[str]
    allowLocalBinding: bool
    allowAllUnixSockets: bool
    deniedResolvedAddresses: list[str]


class RuntimeSettings(TypedDict):
    """The reviewed SRT configuration schema compiled from a scope."""

    filesystem: FilesystemSettings
    network: NetworkSettings
    enableWeakerNestedSandbox: bool
    enableWeakerNetworkIsolation: bool
    allowAppleEvents: bool


SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None
SRT_VERSION = "0.0.78"
PROVIDER_DOMAINS = {
    "claude": ("api.anthropic.com:443", "claude.ai:443", "platform.claude.com:443"),
    "codex": ("api.openai.com:443", "chatgpt.com:443", "auth.openai.com:443"),
}
AUTH_FILES = {"codex": ("auth.json",), "claude": (".credentials.json",)}
PROVIDER_CONFIG_PROBES = {
    "codex": tuple(
        f"/etc/codex/{name}"
        for name in ("requirements.toml", "managed_config.toml", "config.toml")
    )
}
SYSTEM_READS = (
    "/System/Library",
    "/usr/bin",
    "/usr/sbin",
    "/usr/lib",
    "/usr/libexec",
    "/usr/share",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/opt/homebrew/bin",
    "/opt/homebrew/lib",
    "/opt/homebrew/Cellar",
    "/opt/homebrew/opt",
    "/opt/homebrew/share",
    "/private/etc/ssl",
    "/private/etc/resolv.conf",
    "/private/etc/hosts",
    "/private/etc/localtime",
    "/etc/ssl",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/localtime",
    "/etc/nsswitch.conf",
    "/etc/ld.so.cache",
    "/dev/null",
    "/dev/urandom",
    "/dev/random",
    "/dev/tty",
    "/dev/fd",
    "/dev/pts",
    "/proc/self",
    "/Library/Apple",
    "/Library/Frameworks",
    "/Library/Keychains/System.keychain",
)


def default_access_profile(role: str, root: Path = ROOT) -> str:
    return default_profile(role, root)


def _absent_provider_config_reads(cli: str | None) -> tuple[str, ...]:
    """Let native clients see ENOENT for required, absent system config probes.

    Existing host policy may contain credentials. Refuse it rather than importing
    it or skipping native management controls. Never grant parent directory data.
    """
    allowed: list[str] = []
    for value in () if cli is None else PROVIDER_CONFIG_PROBES.get(cli, ()):
        path = Path(value)
        aliases = [path]
        if sys.platform == "darwin" and path.is_relative_to("/etc"):
            path = Path("/private/etc") / path.relative_to("/etc")
            aliases.append(path)
        if any(parent.is_symlink() for parent in (path.parent, *path.parents)):
            raise ValueError("Native system configuration probes cannot follow aliases")
        try:
            path.lstat()
        except FileNotFoundError:
            allowed.extend(str(alias) for alias in aliases)
        except OSError as exc:
            raise ValueError(
                "Native system configuration presence is unavailable"
            ) from exc
        else:
            raise ValueError(
                f"Native system configuration requires review before agent use: {path}"
            )
    return tuple(dict.fromkeys(allowed))


def runtime_executable(root: Path = ROOT) -> Path:
    package = root / "tools/runtime-node/node_modules/@anthropic-ai/sandbox-runtime"
    try:
        version = json.loads((package / "package.json").read_text())["version"]
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(
            "Standalone sandbox runtime is missing. Run tools/runtime/install.sh; there is no host fallback."
        ) from exc
    executable = root / "tools/runtime-node/node_modules/.bin/srt"
    if (
        version != SRT_VERSION
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise ValueError(f"Sandbox runtime must be the reviewed version {SRT_VERSION}")
    if sys.platform == "darwin":
        if not Path("/usr/bin/sandbox-exec").is_file():
            raise ValueError("macOS Seatbelt is unavailable")
    elif sys.platform == "linux":
        if not all(shutil.which(tool) for tool in ("bwrap", "socat", "rg")):
            raise ValueError("Linux requires bubblewrap, socat and ripgrep")
    else:
        raise ValueError("This runtime adapter currently supports macOS and Linux")
    return executable


def native_executable(cli: str) -> Path:
    located = shutil.which(cli)
    candidates = [Path(located)] if located else []
    candidates.extend(
        (
            Path.home() / ".local/bin" / cli,
            Path("/opt/homebrew/bin") / cli,
            Path("/usr/local/bin") / cli,
        )
    )
    for candidate in dict.fromkeys(candidates):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            executable = candidate.resolve()
            if any(
                part.lower() == ".devcontainer"
                for part in executable.parts
            ):
                raise ValueError(
                    f"Native CLI {cli!r} resolves to a retired container launcher; replace that installed wrapper"
                )
            return executable
    raise ValueError(f"Native CLI {cli!r} is not installed")


def cancellation_gate_present(root: Path) -> bool:
    """True if the gate exists, including as a dangling symlink (exists() follows links)."""
    gate = root / "tools/runtime-state/cancellation-unconfirmed.json"
    return gate.is_symlink() or gate.exists()


def _inner_cancellation_group(evidence: Path) -> int | None:
    """Read the agent-writable evidence file; any bad content yields None, never raises."""
    try:
        data: object = json.loads(evidence.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # json.loads objects always have str keys; isinstance only narrows to dict[Unknown, Unknown].
    group_id = cast("dict[str, object]", data).get("group_id")
    if isinstance(group_id, int) and not isinstance(group_id, bool):
        return group_id
    return None


def runtime_available(root: Path = ROOT, cli: str | None = None) -> bool:
    try:
        check_process_records(root)
        if cancellation_gate_present(root):
            return False
        load_policy(root)
        runtime_executable(root)
        require_verified_runtime(root, SRT_VERSION)
        if cli is not None:
            if cli not in PROVIDER_DOMAINS:
                return False
            native_executable(cli)
        return True
    except (OSError, ValueError):
        return False


def clean_environment(
    run: Path, root: Path, cli: str | None, *, provider_executable: Path | None = None
) -> dict[str, str]:
    """No login shell, keychain/SSH socket, inherited API keys or ambient hooks."""
    home = run / "home"
    config = run / "config"
    cache = run / "cache"
    scratch = run / "scratch"
    for path in (home, config, cache, scratch, run / "bin"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [
        str(run / "bin"),
        str(Path(sys.executable).resolve().parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join(dict.fromkeys(paths)),
        "TMPDIR": str(scratch),
        "XDG_CONFIG_HOME": str(config),
        "XDG_CACHE_HOME": str(cache),
        "XDG_DATA_HOME": str(run / "data"),
        "XDG_STATE_HOME": str(run / "state"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CLAUDE_CODE_TMPDIR": str(scratch),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_AUTO_UPDATE": "1",
        "DISABLE_AUTOUPDATER": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        "VAULTLENS_RUNTIME_MANIFEST": str(run / "scope.json"),
        "VAULTLENS_SCOPED_MCP": str(run / "mcp.json"),
        "BRAIN_HOME": str(root),
        "BRAIN_PROJECT_ROOT": str(root),
        "BRAIN_PYTHON": sys.executable,
    }
    for key in ("TERM", "COLORTERM", "TZ"):
        if key in os.environ:
            env[key] = os.environ[key]
    budget = os.environ.get("VAULTLENS_COS_CONTEXT_CHARS", "").strip()
    if budget:
        if not budget.isdecimal() or int(budget) <= 0:
            raise ValueError("Context budget must be a positive integer")
        env["VAULTLENS_COS_CONTEXT_CHARS"] = budget
    if cli:
        state = run / "provider"
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR" if cli == "claude" else "CODEX_HOME"] = str(state)
        if provider_executable:
            env["VAULTLENS_PROVIDER_CLI"] = cli
            env["VAULTLENS_PROVIDER_EXECUTABLE"] = str(provider_executable)
        if cli == "claude":
            (state / ".claude.json").write_text('{"hasCompletedOnboarding":true}\n')
        if sys.platform == "darwin":
            if cli == "codex":
                # Select Codex's validated PEM/Rustls transport. Its native TLS
                # path requires the host security service, which is excluded.
                # SSL_CERT_FILE also prevents Rustls from loading host roots.
                env["SSL_CERT_FILE"] = "/private/etc/ssl/cert.pem"
            missing = _absent_provider_config_reads(cli)
            metadata = dict.fromkeys(str(Path(path).parent) for path in missing)
            if any(Path(path).is_relative_to("/etc") for path in missing):
                # SRT resolves grants to /private/etc. Native clients still
                # need metadata on the /etc symlink to traverse their spelling.
                metadata["/etc"] = None
            env["VAULTLENS_PROVIDER_METADATA"] = json.dumps(list(metadata))
    return env


def _private_directory(path: Path, root: Path) -> None:
    current = root
    for component in path.relative_to(root).parts:
        current /= component
        if current.is_symlink():
            raise ValueError(f"Runtime state cannot use symbolic links: {current}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _operator_home() -> Path:
    """Use the account's real home, independent of inherited runtime variables."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute() or ".." in home.parts:
        raise ValueError("Operator home must be an absolute account directory")
    return home.resolve(strict=True)


def auth_store_path(root: Path, cli: str) -> Path:
    """Return the dedicated per-vault login path without reading or creating it."""
    if cli not in AUTH_FILES:
        raise ValueError("Authentication store requires a supported native CLI")
    home = _operator_home()
    vault = root.resolve(strict=True)
    identifier = hashlib.sha256(os.fsencode(str(vault))).hexdigest()
    if sys.platform == "darwin":
        base = home / "Library/Application Support/VaultLens/agent-state"
    elif sys.platform == "linux":
        base = home / ".local/state/vaultlens/agent-state"
    else:
        raise ValueError("Authentication storage supports macOS and Linux")
    path = base / identifier / "providers" / cli
    if path.is_relative_to(vault):
        raise ValueError("Authentication storage must remain outside the vault")
    return path


def _prepare_auth_store(root: Path, cli: str) -> Path:
    """Create private state below the real home through no-follow descriptors."""
    path = auth_store_path(root, cli)
    home = _operator_home()
    components = path.relative_to(home).parts
    with _auth_directory(home) as home_fd:
        metadata = os.fstat(home_fd)
        if metadata.st_uid != os.getuid():
            raise ValueError("Operator home must belong to the current account")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ValueError("Operator home cannot be writable by other accounts")
        descriptor = os.dup(home_fd)
        try:
            for index, component in enumerate(components):
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    raise ValueError(
                        "Authentication store cannot use symbolic links or directory aliases"
                    ) from exc
                try:
                    metadata = os.fstat(child)
                    if metadata.st_uid != os.getuid():
                        raise ValueError(
                            "Authentication directories must belong to the current account"
                        )
                    # Library/Application Support and .local/state are shared
                    # application parents. Keep their existing modes, while all
                    # dedicated VaultLens descendants remain private.
                    if index >= 2:
                        os.fchmod(child, 0o700)
                    elif stat.S_IMODE(metadata.st_mode) & 0o022:
                        raise ValueError(
                            "Authentication parent directories cannot be writable by other accounts"
                        )
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
        finally:
            os.close(descriptor)
    return path


@contextlib.contextmanager
def _auth_directory(path: Path) -> Generator[int, None, None]:
    """Anchor every directory component without following agent-created links."""
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("Authentication directory cannot contain traversal")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise ValueError(
                    "Authentication directory cannot use symbolic links or missing components"
                ) from exc
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _transfer_auth(
    source: Path,
    destination: Path,
    cli: str,
    *,
    source_fd: int | None = None,
    destination_fd: int | None = None,
) -> None:
    """Transfer only native authentication files, never prior sessions or config."""
    with (
        _auth_directory(source) as opened_source,
        _auth_directory(destination) as opened_destination,
    ):
        for retained, opened in (
            (source_fd, opened_source),
            (destination_fd, opened_destination),
        ):
            if retained is not None:
                before, after = os.fstat(retained), os.fstat(opened)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise ValueError("Authentication directory changed during run")
        source_fd = opened_source if source_fd is None else source_fd
        destination_fd = (
            opened_destination if destination_fd is None else destination_fd
        )
        for name in AUTH_FILES[cli]:
            try:
                metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > 1024 * 1024
            ):
                raise ValueError(
                    "Authentication state must be a bounded private regular file"
                )
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd
            )
            with os.fdopen(descriptor, "rb") as stream:
                actual = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(actual.st_mode)
                    or actual.st_nlink != 1
                    or actual.st_size > 1024 * 1024
                ):
                    raise ValueError(
                        "Authentication state must be a bounded private regular file"
                    )
                contents = stream.read(1024 * 1024 + 1)
            if len(contents) > 1024 * 1024:
                raise ValueError("Authentication state exceeds size limit")
            try:
                target = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
            except FileNotFoundError:
                target = None
            if target and (not stat.S_ISREG(target.st_mode) or target.st_nlink != 1):
                raise ValueError(
                    "Authentication destination cannot be a symbolic link or alias"
                )
            staged = ".auth-transfer-" + secrets.token_hex(16)
            output = os.open(
                staged,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                with os.fdopen(output, "wb") as stream:
                    stream.write(contents)
                    os.fsync(stream.fileno())
                os.replace(
                    staged, name, src_dir_fd=destination_fd, dst_dir_fd=destination_fd
                )
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(staged, dir_fd=destination_fd)


def compile_settings(
    scope: RunScope, run: Path, cli: str | None, *, executables: tuple[Path, ...] = ()
) -> RuntimeSettings:
    """Compile a neutral scope into the reviewed SRT configuration schema."""
    trusted = [
        scope.root / "tools",
        scope.root / ".agents",
        scope.root / "AGENTS.md",
        scope.root / "wiki/AGENTS.md",
        scope.root / "projects/AGENTS.md",
    ]
    state = scope.root / "tools/runtime-state"
    allowed = [
        *SYSTEM_READS,
        *_absent_provider_config_reads(cli),
        *(str(path) for path in trusted),
        *(str(path) for path in scope.read_paths),
        str(run),
        *(str(path.resolve()) for path in executables),
    ]
    denied = [
        "/",
        str(state),
        str(scope.root / "tools/runtime-node"),
        str(scope.root / "raw/review-inbox"),
        *(str(path) for path in scope.denied_paths),
        str(Path.home()),
    ]
    aliases: set[str] = set()
    for grant in dict.fromkeys((*scope.read_paths, *scope.write_paths)):
        candidates = [grant]
        if grant.is_dir():
            for directory, children, files in os.walk(grant, followlinks=False):
                candidates.extend(
                    Path(directory) / name for name in (*children, *files)
                )
                children[:] = [
                    name
                    for name in children
                    if not (Path(directory) / name).is_symlink()
                    and name not in FORBIDDEN_DIRS
                ]
        for path in candidates:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISLNK(metadata.st_mode)
                or stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink > 1
            ):
                aliases.add(str(path))
    denied.extend(sorted(aliases))
    # Reopening an explicitly selected subtree is supported by SRT allowRead.
    # A narrower deny inside that subtree still wins over the broad read grant.
    for subtree in ("wiki", "raw", "projects", "tools"):
        for pattern in (
            f"**/{name}" for name in sorted(FORBIDDEN_DIRS) + list(SECRET_NAMES)
        ):
            denied.append(str(scope.root / subtree / pattern))
    protected = [
        str(scope.root / path)
        for path in (
            "raw",
            "tools",
            ".agents",
            ".codex",
            ".claude",
            ".obsidian",
            ".git",
            "AGENTS.md",
            "CLAUDE.md",
            ".mcp.json",
        )
    ]
    protected.extend(
        [
            *(str(path) for path in scope.denied_paths),
            *sorted(aliases),
            "/tmp/claude",
            "/private/tmp/claude",
            str(run / "scope.json"),
            str(run / "settings.json"),
            str(run / "mcp.json"),
            str(run / "workspace"),
            str(run / "bin"),
            str(run / "read-canary"),
            str(run / "write-canary"),
            str(scope.reports),
        ]
    )
    denied.append(str(run / "read-canary"))
    denied.append(str(run / "supervision"))
    protected.append(str(run / "supervision"))
    for grant in scope.write_paths:
        protected.extend(
            str(grant / pattern)
            for pattern in (
                f"**/{name}"
                for name in sorted(FORBIDDEN_DIRS | PROTECTED_NAMES)
                + list(SECRET_NAMES)
            )
        )
    # Tools are immutable; login state is handled by the native CLI outside note
    # write grants. Use concrete tool paths so the selected state can refresh.
    protected.remove(str(scope.root / "tools"))
    protected.extend(
        str(path)
        for path in (scope.root / "tools").iterdir()
        if path.name not in {"runtime-state", "runtime-node", "__pycache__"}
    )
    protected.extend(
        [
            str(state / "backups"),
            str(state / "deployments"),
            str(state / "locks"),
            str(scope.root / "tools/runtime-node"),
        ]
    )
    write = [
        *(str(path) for path in scope.write_paths),
        str(run / "scratch"),
        str(run / "home"),
        str(run / "cache"),
        str(run / "state"),
        str(run / "data"),
    ]
    if cli:
        write.append(str(run / "provider"))
    return {
        "filesystem": {
            "denyRead": list(dict.fromkeys(denied)),
            "allowRead": list(dict.fromkeys(allowed)),
            "allowWrite": write,
            "denyWrite": list(dict.fromkeys(protected)),
        },
        "network": {
            "allowedDomains": [
                *(() if cli is None else PROVIDER_DOMAINS.get(cli, ())),
                *scope.research_domains,
            ],
            "deniedDomains": [],
            "allowUnixSockets": [],
            "allowLocalBinding": False,
            "allowAllUnixSockets": False,
            "deniedResolvedAddresses": [
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "100.64.0.0/10",
                "127.0.0.0/8",
                "169.254.0.0/16",
                "0.0.0.0/8",
                "::1/128",
                "fe80::/10",
                "fc00::/7",
            ],
        },
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowAppleEvents": False,
    }


def active_scope() -> RunScope | None:
    value = os.environ.get("VAULTLENS_RUNTIME_MANIFEST")
    if not value:
        return None
    path = Path(value)
    try:
        data = json.loads(path.read_text())
        return RunScope(
            Path(data["root"]),
            data["profile"],
            tuple(Path(p) for p in data["read"]),
            tuple(Path(p) for p in data["write"]),
            tuple(Path(p) for p in data["deny_read"]),
            tuple(data["research_domains"]),
            Path(data["reports"]),
            data.get("review_queue_metadata", False),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            "Invalid active runtime manifest; refusing host execution"
        ) from exc


def active_working_directory() -> Path:
    """Keep native configuration discovery in the immutable private run view."""
    value = os.environ.get("VAULTLENS_RUNTIME_MANIFEST")
    if not value or not Path(value).is_absolute():
        raise ValueError("A native working directory requires an active run")
    return Path(value).parent / "workspace"


def _prepare_workspace(scope: RunScope, run: Path) -> Path:
    """Expose approved paths without importing ambient provider configuration.

    The view contains names and links only. The outer sandbox still checks the
    original targets, including every read denial and protected write path.
    """
    workspace = run / "workspace"
    workspace.mkdir(mode=0o700)
    references = {
        scope.root / name
        for name in (
            "AGENTS.md",
            ".agents",
            "tools",
            "wiki/AGENTS.md",
            "projects/AGENTS.md",
        )
    }
    references.update(scope.read_paths)
    for source in sorted(references, key=lambda path: (len(path.parts), str(path))):
        relative = source.relative_to(scope.root)
        if not relative.parts:
            raise ValueError("Private workspace grants must name a vault path")
        if not source.exists():
            continue
        parent = workspace
        covered = False
        for part in relative.parts[:-1]:
            parent /= part
            if parent.is_symlink():
                covered = True
                break
            parent.mkdir(mode=0o700, exist_ok=True)
        if covered:
            continue
        target = parent / relative.name
        if not target.exists() and not target.is_symlink():
            target.symlink_to(source, target_is_directory=source.is_dir())
    return workspace


def verify_active_boundary() -> RunScope:
    """Reject a missing or forged marker before gathering document context."""
    scope = active_scope()
    if scope is None:
        raise ValueError("No whole-process runtime is active")
    run = Path(os.environ["VAULTLENS_RUNTIME_MANIFEST"]).parent
    try:
        (run / "read-canary").read_text()
    except PermissionError:
        pass
    else:
        raise ValueError("Runtime read confinement is absent")
    try:
        with (run / "write-canary").open("a") as stream:
            stream.write("CONFINEMENT_FAILURE")
    except PermissionError:
        pass
    else:
        raise ValueError("Runtime write confinement is absent")
    return scope


def _snapshot(scope: RunScope, run_id: str) -> Path | None:
    if not scope.write_paths:
        return None
    backup = scope.root / "tools/runtime-state/backups" / run_id
    backup.mkdir(mode=0o700, parents=True)
    (backup / "scope.json").write_text(json.dumps(scope.manifest(), indent=2) + "\n")
    for source in scope.write_paths:
        if source.is_dir():
            shutil.copytree(
                source,
                backup / source.relative_to(scope.root),
                symlinks=True,
                ignore=shutil.ignore_patterns(
                    "node_modules", "__pycache__", ".git", ".claude", ".codex"
                ),
            )
    return backup


@contextlib.contextmanager
def _disposable_run() -> Generator[Path, None, None]:
    """Keep failed cleanup evidence outside the vault and cloud folders."""
    directory = "/private/tmp" if sys.platform == "darwin" else "/tmp"
    run = Path(tempfile.mkdtemp(prefix="vaultlens-run-", dir=directory)).resolve()
    run.chmod(0o700)
    retain = False
    try:
        yield run
    except ProcessCleanupError:
        retain = True
        raise
    finally:
        if not retain:
            shutil.rmtree(run)


@contextlib.contextmanager
@shared_runtime
def prepared_run(
    scope: RunScope,
    cli: str | None,
    *,
    snapshot: bool = True,
    require_verification: bool = True,
) -> Generator[tuple[Path, Path, dict[str, str]], None, None]:
    """Prepare private per-run state, scope search, and recoverable writer changes."""
    check_process_records(scope.root)
    quarantine = scope.root / "tools/runtime-state/cancellation-unconfirmed.json"
    if cancellation_gate_present(scope.root):
        raise ValueError(
            "Previous tool cancellation is unconfirmed. Inspect tools/runtime-state/cancellation-unconfirmed.json before another run."
        )
    executable = runtime_executable(scope.root)
    if require_verification:
        require_verified_runtime(scope.root, SRT_VERSION)
    provider = native_executable(cli) if cli else None
    with _disposable_run() as run:
        env = clean_environment(run, scope.root, cli, provider_executable=provider)
        _prepare_workspace(scope, run)
        manifest = scope.manifest()
        manifest["run_id"] = (
            dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + run.name.rsplit("-", 1)[-1]
        )
        review_queue: list[dict[str, str | int]] = []
        manifest["review_queue"] = review_queue
        if scope.review_queue_metadata:
            queue = scope.root / "raw/review-inbox"
            if any(
                (scope.root / prefix).is_symlink()
                for prefix in ("raw", "raw/review-inbox")
            ):
                raise ValueError("Consent queue metadata cannot follow symbolic links")
            if queue.is_dir() and not queue.is_symlink():
                for item in sorted(queue.iterdir()):
                    if not item.name.startswith(".") and not item.is_symlink():
                        try:
                            metadata = item.lstat()
                        except OSError:
                            continue
                        review_queue.append(
                            {"name": item.name, "size": metadata.st_size}
                        )
        (run / "scope.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (run / "read-canary").write_text("Public synthetic read sentinel\n")
        (run / "write-canary").write_text("Public synthetic write sentinel\n")
        server = {
            "mcpServers": {
                "qmd": {
                    "type": "stdio",
                    "command": str(Path(sys.executable).resolve()),
                    "args": [
                        str(scope.root / "tools/scoped_search.py"),
                        "--manifest",
                        str(run / "scope.json"),
                        "mcp",
                    ],
                }
            }
        }
        (run / "mcp.json").write_text(json.dumps(server, indent=2) + "\n")
        shim = run / "bin/qmd"
        shim.write_text(
            "#!/bin/sh\nexec "
            + shlex.join(
                [
                    str(Path(sys.executable).resolve()),
                    str(scope.root / "tools/scoped_search.py"),
                    "--manifest",
                    str(run / "scope.json"),
                ]
            )
            + ' "$@"\n'
        )
        shim.chmod(0o700)
        settings = compile_settings(
            scope,
            run,
            cli,
            executables=tuple(
                path for path in (Path(sys.executable), provider) if path
            ),
        )
        (run / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        locks = scope.root / "tools/runtime-state/locks"
        _private_directory(locks, scope.root)
        # All writers in one vault serialize, including manual and scheduled
        # runs; overlapping ancestor scopes cannot evade this lock.
        transfer_back: tuple[Path, int, int] | None = None
        with contextlib.ExitStack() as stack:
            lock = stack.enter_context((locks / "writers.lock").open("a"))
            if scope.write_paths:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError(
                        "Another scoped writer is running in this vault"
                    ) from exc
            if cli:
                auth_store = _prepare_auth_store(scope.root, cli)
                auth_lock = stack.enter_context((locks / f"{cli}.lock").open("a"))
                try:
                    fcntl.flock(auth_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError(
                        f"Another {cli} runtime is using its login state"
                    ) from exc
                store_fd = stack.enter_context(_auth_directory(auth_store))
                run_provider_fd = stack.enter_context(_auth_directory(run / "provider"))
                _transfer_auth(
                    auth_store,
                    run / "provider",
                    cli,
                    source_fd=store_fd,
                    destination_fd=run_provider_fd,
                )
                transfer_back = (auth_store, store_fd, run_provider_fd)
            backup = _snapshot(scope, manifest["run_id"]) if snapshot else None
            if backup:
                print(f"Recovery snapshot: {backup}", file=sys.stderr)
            try:
                yield executable, run, env
            except ProcessCleanupError as exc:
                quarantine.write_text(
                    json.dumps(
                        {
                            "run": manifest["run_id"],
                            "run_directory": str(run),
                            "group_id": exc.group_id,
                            "reason": str(exc),
                        },
                        indent=2,
                    )
                    + "\n"
                )
                quarantine.chmod(0o600)
                raise
            if cli and transfer_back is not None:
                auth_store, store_fd, run_provider_fd = transfer_back
                _transfer_auth(
                    run / "provider",
                    auth_store,
                    cli,
                    source_fd=run_provider_fd,
                    destination_fd=store_fd,
                )


def runtime_command(executable: Path, settings: Path, argv: Sequence[str]) -> list[str]:
    """Pass original arguments to SRT, which owns its shell quoting layer."""
    if isinstance(argv, (str, bytes)):
        raise ValueError("Runtime command must be an argument sequence")
    arguments = list(argv)
    if (
        not arguments
        or not arguments[0]
        or any(
            not isinstance(argument, str)  # pyright: ignore[reportUnnecessaryIsInstance] -- runtime validation
            or "\0" in argument
            for argument in arguments
        )
    ):
        raise ValueError(
            "Runtime command needs a valid executable and string arguments"
        )
    return [str(executable), "--settings", str(settings), "--", *arguments]


def _verify_preflight(
    executable: Path, run: Path, env: dict[str, str], root: Path
) -> None:
    """Test real OS denials with the exact settings, before reading any notes."""
    command = [
        str(Path(sys.executable).resolve()),
        str(root / "tools/runtime/check_boundary.py"),
    ]
    child = launch_supervised(
        runtime_command(executable, run / "settings.json", command),
        run=run,
        cwd=run / "workspace",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    previous: dict[signal.Signals, SignalHandler] = {}

    def stop(signum: int, _frame: FrameType | None) -> None:
        raise _StopRun(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, stop)
    try:
        try:
            stdout, stderr = child.communicate(timeout=30)
        except (subprocess.TimeoutExpired, _StopRun, KeyboardInterrupt) as exc:
            raise ValueError(
                "Whole-process isolation preflight interrupted or timed out"
            ) from exc
        finally:
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
            terminate_group(child)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if child.returncode:
        detail = (stderr or stdout).strip()[-2000:]
        raise ValueError(
            f"Whole-process isolation preflight failed: {detail or child.returncode}"
        )


class _StopRun(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__()
        self.signum: int = signum


def _finish_report(
    recorder: Recorder, result: int, pending: BaseException | None
) -> None:
    from run_reports import ReportCaptureError

    if isinstance(pending, ProcessCleanupError):
        recorder.cleanup_unconfirmed = True
    if pending is not None:
        result = 125
    try:
        path = recorder.finish(result)
    except ReportCaptureError as exc:
        if exc.report_path:
            print(f"Partial agent report: {exc.report_path}", file=sys.stderr)
        if exc.cleanup_unconfirmed and not isinstance(pending, ProcessCleanupError):
            raise ProcessCleanupError(str(exc)) from exc
        if pending is not None:
            pending.add_note(str(exc))
        else:
            raise ValueError(str(exc)) from exc
    else:
        print(f"Agent report: {path}", file=sys.stderr)


def _execute_prepared(
    command: Sequence[str],
    executable: Path,
    run: Path,
    env: dict[str, str],
    cwd: Path,
    *,
    interactive: bool = False,
    recorder: Recorder | None = None,
) -> int:
    """Own the wrapper's group, including tools that outlive its leader."""
    wrapped = runtime_command(executable, run / "settings.json", command)
    child = launch_supervised(
        wrapped,
        run=run,
        cwd=cwd,
        env=env,
        interactive=interactive,
        stdout=subprocess.PIPE if recorder is not None else None,
    )
    previous: dict[signal.Signals, SignalHandler] = {}
    terminal: int | None = None
    foreground: int | None = None
    if (
        interactive
        and sys.stdin.isatty()
        and getattr(child, "handles_terminal", False) is not True
    ):
        # Annotated so a stdin typed as Any cannot widen the narrowed type back to int | None.
        stdin_fd: int = sys.stdin.fileno()
        terminal = stdin_fd
        foreground = os.tcgetpgrp(terminal)
        previous[signal.SIGTTOU] = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            os.tcsetpgrp(terminal, child.pid)
        except OSError:
            terminate_group(child, grace=6.0)
            signal.signal(signal.SIGTTOU, previous[signal.SIGTTOU])
            raise

    def stop(signum: int, _frame: FrameType | None) -> None:
        raise _StopRun(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, stop)
    result = 125
    try:
        try:
            if recorder is not None:
                if child.stdout is None:
                    raise ValueError("Report capture requires a stdout pipe")
                recorder.pump_in_thread(child.stdout, sys.stdout)
            try:
                result = child.wait()
            except _StopRun as stop_request:
                result = 128 + stop_request.signum
            except KeyboardInterrupt:
                result = 128 + signal.SIGINT
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, signal.SIG_IGN)
            # The headless orchestrator has its own two-second cleanup grace.
            # Allow it to finish before killing the outer wrapper and proxies.
            terminate_group(child, grace=6.0)
        if result == 125:
            group_id = _inner_cancellation_group(
                run / "scratch/inner-cancellation.json"
            )
            raise ProcessCleanupError(
                "Agent reported unconfirmed descendant cancellation",
                group_id=group_id,
            )
        if result < 0 or result in (128 + signal.SIGTERM, 128 + signal.SIGINT):
            # Native providers may own separate tool groups. After an outer
            # signal their orchestrator cannot certify every group's cleanup.
            # Persist a vault-wide gate instead of starting another writer.
            raise ProcessCleanupError(
                "Outer runtime interrupted; provider/tool cleanup requires operator verification"
            )
        return result
    finally:
        try:
            if recorder is not None:
                _finish_report(recorder, result, sys.exc_info()[1])
        finally:
            if terminal is not None and foreground is not None:
                os.tcsetpgrp(terminal, foreground)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def _run(
    command: list[str],
    *,
    scope: RunScope,
    cli: str | None,
    interactive: bool = False,
    snapshot: bool = True,
    report_role: str | None = None,
    private_cwd: bool = False,
) -> int:
    with prepared_run(scope, cli, snapshot=snapshot) as (executable, run, env):
        _verify_preflight(executable, run, env, scope.root)
        recorder: Recorder | None = None
        if report_role:
            from run_reports import Recorder

            if cli is None:
                raise ValueError("Report capture requires a native provider")
            recorder = Recorder(scope, report_role, cli)
        if cli and command[0] == cli:
            command = [env["VAULTLENS_PROVIDER_EXECUTABLE"], *command[1:]]
        return _execute_prepared(
            command,
            executable,
            run,
            env,
            run / "home" if private_cwd else run / "workspace",
            interactive=interactive,
            recorder=recorder,
        )


def launch_headless(root: Path, args: argparse.Namespace, *, argv: list[str]) -> int:
    from agent_profiles import AGENT_FILES, load_role

    role = load_role(root / ".agents/roles" / AGENT_FILES[args.agent])
    scope = resolve_scope(
        root,
        args.access_profile or default_access_profile(args.agent, root),
        project=args.project,
        read_paths=tuple(args.read_path),
        capability=role.permission_profile,
    )
    if args.debug:
        print(json.dumps(scope.manifest(), indent=2))
        print("Runtime: standalone SRT " + SRT_VERSION)
        print("No model or document content was read.")
        return 0
    return _run(
        [
            str(Path(sys.executable).resolve()),
            str(root / "tools/agents/wiki-agent.py"),
            *argv,
        ],
        scope=scope,
        cli=args.cli,
        report_role=args.agent,
    )


def launch_interactive(
    root: Path,
    provider: str,
    args: list[str],
    *,
    profile: str | None = None,
    project: str | None = None,
    read_paths: Sequence[str] = (),
) -> int:
    from provider_commands import ProviderCommandRequest, build_provider_command

    scope = resolve_scope(
        root,
        profile or ("project-write" if project else "wiki-read"),
        project=project,
        read_paths=tuple(read_paths),
    )
    native = native_executable(provider)
    # Native flags cannot change runtime access, but reject policy/config escape
    # options rather than letting an operator accidentally undo CLI restrictions.
    unsafe = (
        "--danger",
        "--yolo",
        "--config",
        "-c",
        "--settings",
        "--setting-sources",
        "--mcp",
        "--add-dir",
        "--sandbox",
        "--resume",
        "--continue",
        "--worktree",
        "--agent",
        "--no-daemon",
        "--plugin-dir",
    )
    if any(
        value == prefix
        or value.startswith(prefix + "=")
        or prefix.startswith("--danger")
        and value.startswith(prefix)
        for value in args
        for prefix in unsafe
    ):
        raise ValueError(
            "Interactive runtime policy flags belong in an approved access profile"
        )

    class NativeOptions(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            raise ValueError(
                f"Native agent options: {message}; use --model, --effort and an optional prompt"
            )

    parser = NativeOptions(add_help=False, allow_abbrev=False)
    parser.add_argument("--model", "-m", default="")
    parser.add_argument("--effort")
    parser.add_argument("--help", "-h", action="store_true")
    parser.add_argument("--version", "-V", action="store_true")
    parser.add_argument("prompt", nargs="*")
    options = parser.parse_args(args)
    with prepared_run(scope, provider) as (executable, run, env):
        _verify_preflight(executable, run, env, scope.root)
        cwd = run / "workspace"
        instruction = "Use the approved local access profile. This runtime contract supersedes legacy container, mount and proxy instructions in vault documents. Sources, tools, instructions, credentials of other providers, and Git metadata are protected. Search uses a fresh scoped lexical corpus, never a global qmd index. This session cannot change its own permissions. Read AGENTS.md at the vault root and wiki/AGENTS.md before wiki work."
        if project:
            instruction += f" Work inside projects/{project}. Read projects/AGENTS.md, this project's AGENTS.md and project.md; its Rules section controls project work."
        instruction += f" The vault root is {scope.root}. Your private working directory exposes only approved paths and trusted tools; use those paths or absolute vault paths."
        request = ProviderCommandRequest(
            options.model,
            options.effort,
            instruction,
            " ".join(options.prompt),
            cwd,
            True,
            bool(scope.write_paths),
            interactive=True,
            os_isolation_delegated=True,
            writable_roots=scope.write_paths,
            mcp_config=run / "mcp.json",
            web_search=False,
            network_access=bool(scope.research_domains),
        )
        command = build_provider_command(
            provider, request, executable=str(Path(native).resolve())
        )
        if options.help or options.version:
            command = [
                str(Path(native).resolve()),
                "--help" if options.help else "--version",
            ]
        return _execute_prepared(command, executable, run, env, cwd, interactive=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("profiles", "plan", "doctor", "exec"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--profile", default="wiki-read")
    parser.add_argument("--project")
    parser.add_argument("--cli", choices=tuple(PROVIDER_DOMAINS))
    parser.add_argument("--read-path", action="append", default=[])
    parser.add_argument(
        "--private-cwd",
        action="store_true",
        help="Run exec from its disposable private home",
    )
    args, command = parser.parse_known_args(argv)
    try:
        if args.command != "exec" and command:
            raise ValueError("Unexpected arguments: " + " ".join(command))
        if args.command == "profiles":
            print(json.dumps(load_policy(args.root), indent=2))
        elif args.command == "doctor":
            runtime_executable(args.root)
            print(
                f"Runtime package {SRT_VERSION} and OS prerequisites found; isolation requires tools/runtime/probe.py"
            )
        else:
            scope = resolve_scope(
                args.root,
                args.profile,
                project=args.project,
                read_paths=tuple(args.read_path),
            )
            if args.command == "plan":
                print(json.dumps(scope.manifest(), indent=2))
            else:
                if command and command[0] == "--":
                    command = command[1:]
                if not command:
                    raise ValueError("exec requires a command after --")
                return _run(
                    command,
                    scope=scope,
                    cli=args.cli,
                    interactive=sys.stdin.isatty(),
                    private_cwd=args.private_cwd,
                )
    except ProcessCleanupError as exc:
        print(f"Local runtime cancellation UNCONFIRMED: {exc}", file=sys.stderr)
        return 125
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Local runtime blocked: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
