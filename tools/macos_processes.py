"""Private launchd audit sessions contain a complete macOS process lifetime.

The kernel audit session survives fork, exec, reparenting and setsid. Membership
is read through task-name ports, never task memory. Signals carry the complete
audit token, so PID reuse cannot redirect cleanup to an unrelated process.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import json
import os
import plistlib
import pty
import select
import signal
import stat
import subprocess
import sys
import termios
import threading
import time
import tty
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import IO, Any, ClassVar, TextIO, TypeGuard, cast, final

# Isolated Python guardian execution still imports only its trusted neighbour.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from process_control import ProcessCleanupError  # noqa: E402

MAX_PROCESSES = 16384
MAX_DOCUMENT = 2 * 1024 * 1024

# One kernel audit token: eight unsigned 32-bit words (pid at [5], audit session at [6]).
Token = tuple[int, ...]
# Parsed JSON state file. Any is the honest type of json.loads output; every field is
# validated where it is used (_token, isinstance checks, ProcessCleanupError).
Document = dict[str, Any]  # pyright: ignore[reportExplicitAny] -- see comment above
# Pipe streams are text or bytes depending on the text flag, like Popen's.
Stream = IO[str] | IO[bytes]


def _is_document(value: object) -> TypeGuard[Document]:
    return isinstance(value, dict)


def _is_sequence(value: object) -> TypeGuard[list[object] | tuple[object, ...]]:
    return isinstance(value, (list, tuple))


class AuditIdentityUnavailable(ProcessCleanupError):
    """A live task refuses its read-only audit token."""


def _private_directory(path: Path) -> None:
    for ancestor in (*reversed(path.parents), path):
        if ancestor.is_symlink():
            raise ValueError("Process state directory cannot follow symbolic links")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ValueError("Process state directory must be owned by this user")
    path.chmod(0o700)


def _read_document(path: Path) -> Document:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or metadata.st_size > MAX_DOCUMENT
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("Process state must be a bounded private regular file")
        data = json.loads(stream.read(MAX_DOCUMENT + 1))
    if not _is_document(data) or data.get("version") != 1:
        raise ValueError("Unrecognized process state schema")
    return data


def _write_document(
    path: Path, data: Mapping[str, object], *, replace: bool = False
) -> None:
    payload = json.dumps(data).encode()
    if len(payload) > MAX_DOCUMENT:
        raise ValueError("Process state exceeds the size limit")
    temporary = path.with_name(".pending-" + uuid.uuid4().hex)
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            if path.exists() or path.is_symlink():
                _read_document(path)
            os.replace(temporary, path)
        elif sys.platform == "darwin":
            system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            rename = system.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            # RENAME_EXCL publishes complete JSON atomically and never replaces
            # an existing protocol leaf. A reader cannot observe a partial file.
            if rename(os.fsencode(temporary), os.fsencode(path), 0x4):
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code), path)
        else:
            # Only isolated unit fixtures use this module outside macOS.
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _token(value: object) -> Token:
    if not _is_sequence(value):
        raise ValueError("Invalid process audit token")
    words: list[int] = []
    for item in value:
        if type(item) is not int or item < 0 or item > 0xFFFFFFFF:
            raise ValueError("Invalid process audit token")
        words.append(item)
    if len(words) != 8 or words[5] <= 1 or words[6] <= 0:
        raise ValueError("Invalid process audit token")
    return tuple(words)


@final
class AuditKernel:
    """All private macOS calls are bounded and fail closed when unavailable."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise ValueError("macOS audit supervision is unavailable on this platform")
        self.system: ctypes.CDLL = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.proc: ctypes.CDLL = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.self_port: int = ctypes.c_uint.in_dll(self.system, "mach_task_self_").value
        self.system.task_name_for_pid.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.system.task_name_for_pid.restype = ctypes.c_int
        self.system.task_info.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.system.task_info.restype = ctypes.c_int
        self.system.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.proc.proc_listpids.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.proc.proc_listpids.restype = ctypes.c_int
        self.proc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.proc.proc_pidinfo.restype = ctypes.c_int
        try:
            self.signal_call = self.proc.proc_signal_with_audittoken
        except AttributeError as exc:
            raise ValueError(
                "Identity-safe macOS process signaling is unavailable"
            ) from exc
        self.signal_call.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.signal_call.restype = ctypes.c_int
        self.foreign_lifetimes: dict[int, int] = {}

    def token(self, pid: int) -> Token | None:
        port = ctypes.c_uint()
        result = self.system.task_name_for_pid(self.self_port, pid, ctypes.byref(port))
        if result in (3, 5):
            # KERN_NO_SPACE/KERN_FAILURE are returned for a process already gone.
            # Confirm existence separately: permission failures must not disappear.
            if self.state(pid) in (None, 5):
                return None
        if result:
            raise AuditIdentityUnavailable("Cannot obtain read-only process identity")
        try:
            data = (ctypes.c_uint * 8)()
            count = ctypes.c_uint(8)
            result = self.system.task_info(
                port.value, 15, ctypes.byref(data), ctypes.byref(count)
            )
            if result:
                # The task-name port remains bound to its original task lifetime.
                if result in (4, 5) and self.state(pid) in (None, 5):
                    return None
                raise AuditIdentityUnavailable("Cannot read process audit identity")
            if count.value != 8 or data[5] != pid:
                raise ProcessCleanupError("Kernel process identity is inconsistent")
            return _token(tuple(data))
        finally:
            self.system.mach_port_deallocate(self.self_port, port.value)

    def state(self, pid: int) -> int | None:
        # The public SDK's PROC_PIDT_SHORTBSDINFO is 64 bytes. Its fourth
        # uint32 is process status; names and arguments are never retained.
        value = (ctypes.c_uint32 * 16)()
        ctypes.set_errno(0)
        amount = self.proc.proc_pidinfo(
            pid, 13, 0, ctypes.byref(value), ctypes.sizeof(value)
        )
        if amount == 0 and ctypes.get_errno() in (errno.ESRCH, errno.ENOENT):
            return None
        if amount != ctypes.sizeof(value) or value[0] != pid:
            raise ProcessCleanupError("Cannot confirm process execution state")
        return value[3]

    def user_pids(self) -> list[int]:
        # PROC_UID_ONLY avoids reading names, argv, files or other users' tasks.
        capacity = 256
        while capacity <= MAX_PROCESSES:
            values = (ctypes.c_int * capacity)()
            amount = self.proc.proc_listpids(
                4, os.getuid(), values, ctypes.sizeof(values)
            )
            if amount <= 0 or amount % ctypes.sizeof(ctypes.c_int):
                raise ProcessCleanupError("Cannot enumerate owned process identities")
            if amount < ctypes.sizeof(values):
                return [
                    value
                    for value in values[: amount // ctypes.sizeof(ctypes.c_int)]
                    if value > 1
                ]
            capacity *= 2
        raise ProcessCleanupError("Process enumeration exceeded its bounded capacity")

    def lifetime(self, pid: int) -> int | None:
        # XNU proc_info_private.h declares this 56-byte structure as API.
        # Only p_uniqueid is retained; executable UUID and ancestry are unused.
        value = (ctypes.c_ubyte * 56)()
        ctypes.set_errno(0)
        amount = self.proc.proc_pidinfo(
            pid, 17, 0, ctypes.byref(value), ctypes.sizeof(value)
        )
        if amount == 0 and ctypes.get_errno() in (errno.ESRCH, errno.ENOENT):
            return None
        if amount != ctypes.sizeof(value):
            raise ProcessCleanupError("Cannot confirm immutable process lifetime")
        identity = int.from_bytes(bytes(value)[16:24], sys.byteorder)
        if identity <= 0:
            raise ProcessCleanupError("Kernel process lifetime is invalid")
        return identity

    def capture_foreign_lifetimes(self) -> list[list[int]]:
        """Capture numeric lifetimes before the fresh audit session is created.

        Such tasks cannot inherit a future audit session. The sandbox also denies
        audit-session replacement and job creation. A reused PID or a new task
        that refuses inspection is never excluded.
        """
        values: dict[int, int] = {}
        for pid in self.user_pids():
            identity = self.lifetime(pid)
            if identity is not None:
                values[pid] = identity
        self.foreign_lifetimes = values
        return [[pid, identity] for pid, identity in values.items()]

    def use_foreign_lifetimes(self, values: object) -> None:
        if not _is_sequence(values) or isinstance(values, tuple) or len(values) > MAX_PROCESSES:
            raise ValueError("Invalid pre-session process identities")
        accepted: dict[int, int] = {}
        for value in values:
            if not _is_sequence(value) or isinstance(value, tuple) or len(value) != 2:
                raise ValueError("Invalid pre-session process identity")
            pid, identity = value
            if (
                type(pid) is not int
                or pid <= 1
                or type(identity) is not int
                or identity <= 0
                or identity > 0xFFFFFFFFFFFFFFFF
                or pid in accepted
            ):
                raise ValueError("Invalid pre-session process identity")
            accepted[pid] = identity
        self.foreign_lifetimes = accepted

    def members(self, session: int) -> list[Token]:
        members: list[Token] = []
        for pid in self.user_pids():
            try:
                token = self.token(pid)
            except AuditIdentityUnavailable:
                lifetime = self.lifetime(pid)
                if lifetime is None:
                    continue
                if self.foreign_lifetimes.get(pid) != lifetime:
                    raise
                continue
            if token is not None and token[6] == session:
                members.append(token)
        return members

    def signal(self, token: object, signum: int) -> bool:
        actual = _token(token)
        value = (ctypes.c_uint * 8)(*actual)
        result = self.signal_call(ctypes.byref(value), signum)
        if result == errno.ESRCH:
            return False
        if result:
            raise ProcessCleanupError(
                "Identity-safe process signal was denied", group_id=actual[5]
            )
        return True


def verify_process_records(directory: Path) -> None:
    _private_directory(directory)
    kernel: AuditKernel | None = None
    for entry in directory.iterdir():
        if entry.name.startswith(".pending-"):
            raise ValueError(
                "Unfinished process ownership record requires verification"
            )
        data = _read_document(entry)
        owner = _token(data.get("owner"))
        kernel = kernel or AuditKernel()
        current = kernel.token(owner[5])
        if current != owner:
            raise ValueError(
                "A previous process supervisor was lost; verification is required"
            )


def _stop_members(
    kernel: AuditKernel,
    session: int,
    guardian: Token,
    *,
    grace: float = 0.0,
    exclude_self: bool = False,
) -> None:
    """Freeze all remaining session members before a final identity-safe kill."""
    self_pid = os.getpid() if exclude_self else None
    if grace > 0:
        for value in kernel.members(session):
            if value[5] not in (guardian[5], self_pid):
                kernel.signal(value, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            remaining = [
                value
                for value in kernel.members(session)
                if value[5] not in (guardian[5], self_pid)
            ]
            if not remaining:
                break
            time.sleep(0.05)
    if not exclude_self:
        kernel.signal(guardian, signal.SIGSTOP)
    deadline = time.monotonic() + 5
    previous = None
    while time.monotonic() < deadline:
        remaining = [
            value
            for value in kernel.members(session)
            if value[5] not in (guardian[5], self_pid)
        ]
        for value in remaining:
            kernel.signal(value, signal.SIGSTOP)
        current = frozenset(remaining)
        frozen = all(kernel.state(value[5]) in (None, 4, 5) for value in remaining)
        if current == previous and frozen:
            for value in remaining:
                kernel.signal(value, signal.SIGKILL)
            break
        previous = current
        time.sleep(0.02)
    else:
        raise ProcessCleanupError(
            "Process session could not be frozen", group_id=guardian[5]
        )
    while time.monotonic() < deadline:
        remaining = [
            value
            for value in kernel.members(session)
            if value[5] not in (guardian[5], self_pid)
        ]
        if not remaining:
            return
        for value in remaining:
            kernel.signal(value, signal.SIGKILL)
        time.sleep(0.03)
    raise ProcessCleanupError(
        "Process session cleanup is unconfirmed", group_id=guardian[5]
    )


def _launchctl(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments], capture_output=True, text=True, timeout=8
    )


class AuditSessionProcess:
    """Popen-compatible owner for one temporary launchd audit session."""

    audit_session_owner: ClassVar[bool] = True
    handles_terminal: ClassVar[bool] = True
    # Class-level default: terminate_owned also runs on instances built without __init__.
    _cleanup_error: ProcessCleanupError | None = None

    def __init__(
        self,
        command: Sequence[str],
        *,
        run: str | os.PathLike[str],
        cwd: str | os.PathLike[str],
        env: Mapping[str, str],
        stdout: int | None = None,
        stderr: int | None = None,
        text: bool = False,
        interactive: bool = False,
    ) -> None:
        if (
            not isinstance(command, (list, tuple))
            or not command
            or any(type(value) is not str or "\0" in value for value in command)
        ):
            raise ValueError("Supervised command must be an argument sequence")
        self.kernel: AuditKernel = AuditKernel()
        owner = self.kernel.token(os.getpid())
        if owner is None:
            raise ValueError("Cannot establish the trusted supervisor identity")
        self.owner: Token = owner
        foreign_lifetimes = self.kernel.capture_foreign_lifetimes()
        self.returncode: int | None = None
        self.pid: int = 0
        self.guardian: Token | None = None
        self.session: int | None = None
        self.started: bool = False
        self.closed: bool = False
        self.interactive: bool = interactive
        self.text: bool = text
        self.run: Path = Path(run)
        self.control: Path = self.run / "supervision" / uuid.uuid4().hex
        _private_directory(self.control)
        self.service: str = f"gui/{os.getuid()}/com.vaultlens.runtime.{self.control.name}"
        self._dummy: list[int] = []
        self._streams: list[Stream] = []
        self._forwarders: list[threading.Thread] = []
        self._master: int | None = None
        self._slave: int | None = None
        self.stdin: Stream | None = None
        self.stdout: Stream | None = None
        self.stderr: Stream | None = None
        manifest = json.loads((self.run / "scope.json").read_text())
        root = Path(manifest["root"])
        self.records: Path = root / "tools/runtime-state/processes"
        _private_directory(self.records)
        self.record: Path = self.records / (self.control.name + ".json")
        guardian_source = root / "tools/macos_processes.py"
        guard_source = root / "tools/runtime/macos-process-guard.mjs"
        for source in (guardian_source, guard_source):
            metadata = source.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Trusted process supervision helper is unavailable")
        if Path(command[0]).name != "srt" or not Path(command[0]).is_file():
            raise ValueError("Audit supervision requires the pinned SRT launcher")
        guarded_environment = dict(env)
        guarded_environment.pop("VAULTLENS_PROCESS_GUARD_PTY", None)
        metadata_probes = guarded_environment.pop("VAULTLENS_PROVIDER_METADATA", "[]")
        guarded_environment.update(
            {
                "NODE_OPTIONS": "--import=" + guard_source.as_uri(),
                "VAULTLENS_PROCESS_GUARD_SRT": str(Path(command[0]).resolve()),
                "VAULTLENS_PROCESS_GUARD_PYTHON": str(Path(sys.executable).resolve()),
                "VAULTLENS_PROCESS_GUARD_METADATA": metadata_probes,
                "VAULTLENS_PROCESS_GUARD_MARKER": str(
                    self.control / "profile-applied.json"
                ),
            }
        )
        self._record_data: Document = {
            "version": 1,
            "owner": list(self.owner),
            "service": self.service,
        }
        _write_document(self.record, self._record_data)
        try:
            stdio: dict[str, object]
            if interactive:
                self._master, self._slave = pty.openpty()
                self._resize_terminal()
                terminal_path = os.ttyname(self._slave)
                terminal_identity = _terminal_identity(self._slave)
                stdio = {"pty": terminal_path, "pty_identity": terminal_identity}
                metadata = Path(terminal_path).lstat()
                if (
                    not stat.S_ISCHR(metadata.st_mode)
                    or [metadata.st_dev, metadata.st_ino, metadata.st_rdev]
                    != terminal_identity
                ):
                    raise ValueError(
                        "Private terminal pathname does not match its descriptor"
                    )
                # Keep this descriptor open through confirmed cleanup. The
                # guard grants only our private terminal, never all host PTYs.
                guarded_environment["VAULTLENS_PROCESS_GUARD_PTY"] = terminal_path
            else:
                output = self._pipe("stdout")
                error = output if stderr == subprocess.STDOUT else self._pipe("stderr")
                stdio = {
                    "stdout": str(self.control / "stdout"),
                    "stderr": str(
                        self.control
                        / ("stdout" if stderr == subprocess.STDOUT else "stderr")
                    ),
                }
                if stdout == subprocess.PIPE:
                    self.stdout = output
                else:
                    self._forward(output, sys.stdout)
                if stderr == subprocess.PIPE:
                    self.stderr = error
                elif error is not output:
                    self._forward(error, sys.stderr)
            request: dict[str, object] = {
                "version": 1,
                "owner": self.owner,
                "foreign_lifetimes": foreign_lifetimes,
                "argv": list(command),
                "cwd": str(Path(cwd).resolve()),
                "env": guarded_environment,
                "stdio": stdio,
                "control": str(self.control),
                "quarantine": str(
                    root / "tools/runtime-state/cancellation-unconfirmed.json"
                ),
            }
            _write_document(self.control / "request.json", request)
            job: dict[str, object] = {
                "Label": self.service.rsplit("/", 1)[-1],
                "ProgramArguments": [
                    str(Path(sys.executable).resolve()),
                    "-I",
                    str(guardian_source),
                    "--audit-guardian",
                    str(self.control / "request.json"),
                ],
                "SessionCreate": True,
                "RunAtLoad": True,
                "KeepAlive": False,
                "AbandonProcessGroup": False,
                "EnvironmentVariables": {"HOME": env["HOME"], "PATH": "/usr/bin:/bin"},
                "StandardOutPath": str(self.control / "guardian.log"),
                "StandardErrorPath": str(self.control / "guardian.log"),
            }
            self.plist: Path = self.control / "job.plist"
            descriptor = os.open(
                self.plist, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(plistlib.dumps(job))
            result = _launchctl(
                ["bootstrap", self.service.rsplit("/", 1)[0], str(self.plist)]
            )
            if result.returncode:
                raise ValueError("Temporary audit-session launch failed")
            self.started = True
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    handshake = _read_document(self.control / "identity.json")
                except FileNotFoundError:
                    time.sleep(0.02)
                    continue
                candidate = _token(handshake.get("token"))
                if (
                    candidate[6] == self.owner[6]
                    or self.kernel.token(candidate[5]) != candidate
                ):
                    raise ProcessCleanupError(
                        "Fresh process audit session was not established"
                    )
                self.guardian = candidate
                self.pid, self.session = candidate[5], candidate[6]
                self._record_data["guardian"] = list(candidate)
                _write_document(self.record, self._record_data, replace=True)
                _write_document(
                    self.control / "start.json", {"version": 1, "start": True}
                )
                break
            else:
                raise ProcessCleanupError(
                    "Audit-session guardian did not establish ownership"
                )
        except BaseException:
            try:
                if self.started and self.guardian:
                    self.terminate_owned(grace=0)
                elif self.started:
                    _launchctl(["bootout", self.service])
                else:
                    self._remove_record()
            finally:
                self._close_io()
            raise

    def _pipe(self, name: str) -> Stream:
        path = self.control / name
        os.mkfifo(path, 0o600)
        reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        self._dummy.append(os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW))
        os.set_blocking(reader, True)
        stream: Stream = (
            os.fdopen(reader, "r") if self.text else os.fdopen(reader, "rb")
        )
        self._streams.append(stream)
        return stream

    def _forward(self, stream: Stream, destination: TextIO) -> None:
        def pump() -> None:
            try:
                while value := stream.read(4096):
                    if isinstance(value, str):
                        destination.write(value)
                        destination.flush()
                        continue
                    # cast: bytes go to the underlying binary stream, or to destination
                    # itself when it has no buffer (getattr returns Any | TextIO).
                    target = cast(IO[bytes], getattr(destination, "buffer", destination))
                    target.write(value)
                    target.flush()
            except (OSError, ValueError):
                return

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        self._forwarders.append(thread)

    def poll(self) -> int | None:
        if self.returncode is None:
            try:
                status = _read_document(self.control / "status.json")
            except FileNotFoundError:
                if self.guardian and self.kernel.token(self.pid) != self.guardian:
                    raise ProcessCleanupError(
                        "Audit-session guardian disappeared before completion",
                        group_id=self.pid,
                    )
                return None
            if type(status.get("returncode")) is not int:
                raise ProcessCleanupError(
                    "Invalid supervised completion status", group_id=self.pid
                )
            try:
                applied = _read_document(self.control / "profile-applied.json")
            except (OSError, ValueError) as exc:
                raise ProcessCleanupError(
                    "Trusted sandbox process guard did not confirm application",
                    group_id=self.pid,
                ) from exc
            if applied != {"version": 1, "runtime": "0.0.78", "applied": True}:
                raise ProcessCleanupError(
                    "Trusted sandbox process guard returned invalid evidence",
                    group_id=self.pid,
                )
            self.returncode = status["returncode"]
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.monotonic() + timeout if timeout is not None else None
        saved = None
        resize_handler = None
        try:
            if self.interactive and sys.stdin.isatty():
                saved = termios.tcgetattr(sys.stdin.fileno())
                tty.setraw(sys.stdin.fileno())
                self._resize_terminal()
                resize_handler = signal.signal(signal.SIGWINCH, self._resize_terminal)
            while (code := self.poll()) is None:
                if (
                    timeout is not None
                    and deadline is not None
                    and time.monotonic() >= deadline
                ):
                    raise subprocess.TimeoutExpired("supervised SRT", timeout)
                if self.interactive:
                    self._terminal_tick()
                else:
                    time.sleep(0.02)
            return code
        finally:
            if resize_handler is not None:
                signal.signal(signal.SIGWINCH, resize_handler)
            if saved is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)

    def _resize_terminal(
        self, _signum: int | None = None, _frame: FrameType | None = None
    ) -> None:
        if self._master is None or not sys.stdin.isatty():
            return
        size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        # TIOCSWINSZ notifies the private terminal's foreground process group.
        fcntl.ioctl(self._master, termios.TIOCSWINSZ, size)

    def _terminal_output(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            try:
                amount = os.write(sys.stdout.fileno(), remaining)
            except InterruptedError:
                continue
            if amount <= 0:
                raise OSError("Supervised terminal output closed")
            remaining = remaining[amount:]

    def _drain_terminal(self) -> None:
        """Drain the finite final buffer after every sandbox writer is gone."""
        if self._master is None:
            return
        # Keep our non-writing slave descriptor open until the drain finishes.
        # macOS can discard unread master data when the last slave is closed.
        deadline = time.monotonic() + 0.5
        remaining = MAX_DOCUMENT
        while remaining > 0 and time.monotonic() < deadline:
            readable, _, _ = select.select([self._master], [], [], 0.02)
            if not readable:
                return
            try:
                data = os.read(self._master, min(65536, remaining))
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return
                raise
            if not data:
                return
            self._terminal_output(data)
            remaining -= len(data)
        raise OSError("Supervised terminal final output exceeded its drain limit")

    def _terminal_tick(self) -> None:
        master = self._master
        if master is None:
            raise ValueError("Supervised terminal is closed")
        descriptors = [master]
        if sys.stdin.isatty():
            descriptors.append(sys.stdin.fileno())
        readable, _, _ = select.select(descriptors, [], [], 0.02)
        for descriptor in readable:
            try:
                data = os.read(descriptor, 4096)
            except OSError as exc:
                if descriptor == master and exc.errno == errno.EIO:
                    return
                raise
            if not data:
                continue
            if descriptor == master:
                self._terminal_output(data)
            else:
                os.write(master, data)

    def communicate(
        self, input: object = None, timeout: float | None = None
    ) -> tuple[str | bytes | None, str | bytes | None]:
        if input is not None:
            raise ValueError("Unattended sandbox input must be explicit argv")
        collected: list[str | bytes | None] = [None, None]

        def collect(index: int, stream: Stream) -> None:
            collected[index] = stream.read()

        readers: list[threading.Thread] = []
        for index, stream in enumerate((self.stdout, self.stderr)):
            if stream is not None:
                thread = threading.Thread(
                    target=collect, args=(index, stream), daemon=True
                )
                thread.start()
                readers.append(thread)
        self.wait(timeout=timeout)
        self.terminate_owned(grace=0)
        for reader in readers:
            reader.join(timeout=2)
            if reader.is_alive():
                raise ProcessCleanupError(
                    "Supervised output pipe did not close", group_id=self.pid
                )
        return (collected[0], collected[1])

    def _remove_record(self) -> None:
        try:
            data = _read_document(self.record)
        except FileNotFoundError:
            return
        if data != self._record_data:
            raise ProcessCleanupError(
                "Process ownership record changed", group_id=self.pid
            )
        self.record.unlink()

    def _close_io(self) -> None:
        for descriptor in self._dummy:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self._dummy.clear()
        if self._slave is not None:
            with contextlib.suppress(OSError):
                os.close(self._slave)
            self._slave = None
        if self._master is not None:
            with contextlib.suppress(OSError):
                os.close(self._master)
            self._master = None
        for thread in self._forwarders:
            thread.join(timeout=2)

    def terminate_owned(self, grace: float = 2.0) -> None:
        if self.closed:
            return
        if self._cleanup_error is not None:
            raise self._cleanup_error
        guardian, session = self.guardian, self.session
        if guardian is None or session is None:
            raise ProcessCleanupError("Audit-session ownership is missing")
        try:
            if self.kernel.token(self.pid) != guardian:
                raise ProcessCleanupError(
                    "Audit-session guardian identity was lost", group_id=self.pid
                )
            _stop_members(
                self.kernel,
                session,
                guardian,
                grace=grace if self.returncode is None else 0,
            )
            # The live guardian anchors the session until every workload task is
            # gone. It cannot be recycled while the final membership check runs.
            remaining = self.kernel.members(session)
            if remaining != [guardian]:
                raise ProcessCleanupError(
                    "Process session retained an unknown member", group_id=self.pid
                )
            self.kernel.signal(guardian, signal.SIGKILL)
            result = _launchctl(["bootout", self.service])
            absent = _launchctl(["print", self.service])
            deadline = time.monotonic() + 2.0
            while absent.returncode == 0 and time.monotonic() < deadline:
                time.sleep(0.03)
                absent = _launchctl(["print", self.service])
            if result.returncode not in (0, 3, 5, 113) or absent.returncode != 113:
                raise ProcessCleanupError(
                    "Temporary process job cleanup is unconfirmed", group_id=self.pid
                )
            self._drain_terminal()
            self._remove_record()
            self.closed = True
        except ProcessCleanupError as exc:
            # communicate() and a caller's finally may both request cleanup.
            # Do not replace the original failure after we killed the anchor.
            self._cleanup_error = exc
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            self._cleanup_error = ProcessCleanupError(
                "Temporary process cleanup could not be verified", group_id=self.pid
            )
            raise self._cleanup_error from exc
        finally:
            self._close_io()


def _open_output(path: str) -> int:
    descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    metadata = os.fstat(descriptor)
    if not stat.S_ISFIFO(metadata.st_mode) or metadata.st_uid != os.getuid():
        os.close(descriptor)
        raise ValueError("Guardian output must be its private FIFO")
    return descriptor


def _terminal_identity(descriptor: int) -> list[int]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISCHR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or not os.isatty(descriptor)
    ):
        raise ValueError("Private terminal must be an owned terminal character device")
    return [metadata.st_dev, metadata.st_ino, metadata.st_rdev]


def _guardian(request_path: Path) -> int:
    request = _read_document(request_path)
    control = Path(request["control"])
    if control != request_path.parent or control.is_symlink():
        raise ValueError("Guardian control directory is inconsistent")
    owner = _token(request["owner"])
    kernel = AuditKernel()
    kernel.use_foreign_lifetimes(request.get("foreign_lifetimes"))
    guardian = kernel.token(os.getpid())
    if guardian is None or guardian[6] == owner[6]:
        raise ValueError("launchd did not assign a unique process audit session")
    if kernel.foreign_lifetimes.get(os.getpid()) == kernel.lifetime(os.getpid()):
        raise ValueError("Guardian process predates the fresh audit session")
    _write_document(control / "identity.json", {"version": 1, "token": guardian})
    deadline = time.monotonic() + 8
    while not (control / "start.json").exists():
        if kernel.token(owner[5]) != owner or time.monotonic() > deadline:
            return 125
        time.sleep(0.02)
    if _read_document(control / "start.json").get("start") is not True:
        raise ValueError("Supervisor did not approve its guardian identity")
    descriptors: list[int] = []
    stdio = request["stdio"]
    terminal: int | None = None
    output = error = -1

    def terminal_child() -> None:
        import fcntl

        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    if "pty" in stdio:
        descriptor = os.open(stdio["pty"], os.O_RDWR | os.O_NOFOLLOW)
        if _terminal_identity(descriptor) != stdio.get("pty_identity"):
            os.close(descriptor)
            raise ValueError("Guardian terminal identity differs from the supervisor")
        descriptors.append(descriptor)
        terminal = descriptor
    else:
        output = _open_output(stdio["stdout"])
        error = (
            output
            if stdio["stderr"] == stdio["stdout"]
            else _open_output(stdio["stderr"])
        )
        descriptors.extend(set((output, error)))
    child: subprocess.Popen[bytes] | None = None
    cancelled = False

    def stopping(_signum: int, _frame: FrameType | None) -> None:
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    try:
        if terminal is not None:
            child = subprocess.Popen(
                request["argv"],
                cwd=request["cwd"],
                env=request["env"],
                stdin=terminal,
                stdout=terminal,
                stderr=terminal,
                preexec_fn=terminal_child,
            )
        else:
            child = subprocess.Popen(
                request["argv"],
                cwd=request["cwd"],
                env=request["env"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=error,
                start_new_session=True,
            )
        while child.poll() is None:
            if kernel.token(owner[5]) != owner:
                quarantine = Path(request["quarantine"])
                with contextlib.suppress(FileExistsError):
                    _write_document(
                        quarantine,
                        {
                            "version": 1,
                            "group_id": guardian[5],
                            "reason": "Trusted process supervisor disappeared",
                        },
                    )
                _stop_members(kernel, guardian[6], guardian, exclude_self=True)
                return 125
            if cancelled:
                for value in kernel.members(guardian[6]):
                    if value != guardian:
                        kernel.signal(value, signal.SIGTERM)
                break
            time.sleep(0.05)
        result = child.returncode if child.returncode is not None else 143
        _write_document(control / "status.json", {"version": 1, "returncode": result})
        # Keep a live immutable session anchor until the owner confirms cleanup.
        while kernel.token(owner[5]) == owner:
            time.sleep(0.1)
        quarantine = Path(request["quarantine"])
        with contextlib.suppress(FileExistsError):
            _write_document(
                quarantine,
                {
                    "version": 1,
                    "group_id": guardian[5],
                    "reason": "Trusted process supervisor disappeared",
                },
            )
        _stop_members(kernel, guardian[6], guardian, exclude_self=True)
        return 125
    finally:
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--audit-guardian":
        raise SystemExit("This helper is only the private native process supervisor")
    try:
        raise SystemExit(_guardian(Path(sys.argv[2])))
    except Exception as exc:
        evidence = {
            "version": 1,
            "failure_type": type(exc).__name__,
            "errno": getattr(exc, "errno", None),
        }
        if isinstance(exc, ProcessCleanupError):
            evidence["reason"] = str(exc)[:500]
        with contextlib.suppress(OSError, ValueError):
            _write_document(Path(sys.argv[2]).parent / "failure.json", evidence)
        # Never print the request, environment, provider output or authentication.
        print(
            "Native process guardian failed; cleanup verification is required",
            file=sys.stderr,
        )
        raise SystemExit(125)
