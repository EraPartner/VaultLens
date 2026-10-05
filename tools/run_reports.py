"""Bounded stdout reports written by the trusted headless launcher.

The recorder has no model or credential access. Finish it after the child and its
tool groups have stopped. Report writes use descriptor-relative, no-follow paths.
"""

from __future__ import annotations

import codecs
import io
import json
import os
import re
import secrets
import select
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from local_access import REPORT_SUBTREE, RunScope

MAX_REPORT_BYTES = 4 * 1024 * 1024
_METADATA_BYTES = 4096
_TEMPLATER_OPEN = "<%"
_TEMPLATER_ESCAPED = "&lt;%"
_CHUNK_BYTES = 8192
_DRAIN_SECONDS = 3.0
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class _Echo(Protocol):
    """Where captured text is mirrored: a text stream such as ``sys.stdout``."""

    def write(self, text: str, /) -> object: ...

    def flush(self) -> object: ...


class _Stream(Protocol):
    """A byte or text source. Only ``read`` is required; pipes also expose ``fileno``."""

    def read(self, size: int, /) -> bytes | str: ...


class ReportCaptureError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        report_path: Path | None = None,
        cleanup_unconfirmed: bool = False,
    ) -> None:
        super().__init__(message)
        self.report_path: Path | None = report_path
        self.cleanup_unconfirmed: bool = cleanup_unconfirmed


def _label(value: str) -> str:
    # Runtime validation: callers outside the type checker can pass any object.
    if not isinstance(value, str) or not _LABEL.fullmatch(value):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(
            "Report role, provider and profile need simple labels of at most 64 characters"
        )
    return value


def _directory(scope: RunScope) -> int:
    """Open every ancestor without following aliases; create report folders only."""
    root, reports = Path(scope.root), Path(scope.reports)
    if not root.is_absolute() or not reports.is_absolute():
        raise ValueError("Report paths must be absolute")
    try:
        relative = reports.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Reports must stay in this vault's wiki/reports/agents subtree"
        ) from exc
    if relative.parts[:3] != REPORT_SUBTREE.parts or any(
        part in {".", ".."} for part in (*root.parts, *relative.parts)
    ):
        raise ValueError(
            "Reports must stay in this vault's wiki/reports/agents subtree"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root.anchor, flags)
    try:
        for part in root.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        for part in relative.parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _publish(scope: RunScope, filename: str, payload: bytes) -> Path:
    descriptor = _directory(scope)
    temporary = f".{filename}.{secrets.token_hex(8)}.part"
    published = False
    temporary_created = False
    file_descriptor = None
    try:
        file_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        temporary_created = True
        os.fchmod(file_descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(file_descriptor, view)
            if not written:
                raise OSError("Report write made no progress")
            view = view[written:]
        os.fsync(file_descriptor)
        expected = os.fstat(file_descriptor)
        actual = os.stat(temporary, dir_fd=descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(actual.st_mode)
            or actual.st_nlink != 1
            or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise ValueError("Report temporary file changed before publication")
        # Publish complete bytes atomically without replacing any existing name.
        # This link never follows a source or destination alias. Its private
        # temporary name is removed before returning, leaving one regular file.
        os.link(
            temporary,
            filename,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
            follow_symlinks=False,
        )
        published = True
        actual = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(actual.st_mode)
            or actual.st_nlink != 2
            or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise ValueError("Report destination changed during publication")
        os.unlink(temporary, dir_fd=descriptor)
        os.fsync(descriptor)
        return scope.reports / filename
    except BaseException:
        if published:
            os.unlink(filename, dir_fd=descriptor)
        raise
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_created:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        os.close(descriptor)


class Recorder:
    """Echo all stdout, retain bounded UTF-8 text, and save one private report."""

    def __init__(
        self,
        scope: RunScope,
        role: str,
        provider: str,
        *,
        max_bytes: int = MAX_REPORT_BYTES,
    ) -> None:
        if (
            not isinstance(max_bytes, int)  # pyright: ignore[reportUnnecessaryIsInstance] -- runtime validation
            or not _METADATA_BYTES < max_bytes <= MAX_REPORT_BYTES
        ):
            raise ValueError(
                "Report size must exceed 4096 bytes and stay at most 4 MiB"
            )
        self.scope: RunScope = scope
        self.role: str
        self.provider: str
        self.profile: str
        self.role, self.provider, self.profile = map(
            _label, (role, provider, scope.name)
        )
        self.max_bytes: int = max_bytes
        self.started: datetime = datetime.now(timezone.utc)
        stamp = self.started.strftime("%Y%m%dT%H%M%S%fZ")
        self.filename: str = (
            f"agent-{stamp}-{self.role}-{self.profile}-{secrets.token_hex(8)}.md"
        )
        self._body: bytearray = bytearray()
        self._errors: list[str] = []
        self._lock: threading.Lock = threading.Lock()
        self._stop: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started: bool = False
        self._finished: bool = False
        self.truncated: bool = False
        self.eof: bool = False
        self.cleanup_unconfirmed: bool = False

    def _error(self, kind: str) -> None:
        with self._lock:
            if kind not in self._errors:
                self._errors.append(kind)

    @staticmethod
    def _fileno(stream: _Stream) -> int | None:
        try:
            # getattr: the protocol requires only read(); pipes and files add fileno().
            return getattr(stream, "fileno")()
        except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
            return None

    def _drain(self, stream: _Stream, echo: _Echo | None) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        descriptor = self._fileno(stream)

        def retain(text: str) -> None:
            nonlocal echo
            data = text.encode("utf-8")
            with self._lock:
                available = self.max_bytes - _METADATA_BYTES - len(self._body)
                self._body.extend(data[:available])
                self.truncated = self.truncated or len(data) > available
            if echo is not None and text:
                try:
                    echo.write(text)
                    echo.flush()
                except Exception as exc:
                    self._error(f"echo-{type(exc).__name__}")
                    echo = None  # Keep draining so a full child pipe cannot stall it.

        try:
            while not self._stop.is_set():
                if descriptor is not None:
                    ready, _, _ = select.select([descriptor], [], [], 0.1)
                    if not ready:
                        continue
                    chunk = os.read(descriptor, _CHUNK_BYTES)
                else:
                    chunk = stream.read(_CHUNK_BYTES)
                if chunk == b"" or chunk == "":
                    self.eof = True
                    retain(decoder.decode(b"", final=True))
                    break
                if isinstance(chunk, str):
                    retain(chunk)
                elif isinstance(chunk, bytes):  # pyright: ignore[reportUnnecessaryIsInstance] -- duck-typed streams may return other types
                    retain(decoder.decode(chunk))
                else:
                    raise TypeError("Report stream must return bytes or text")
        except Exception as exc:
            self._error(f"read-{type(exc).__name__}")

    def consume(self, stream: _Stream, echo: _Echo | None) -> None:
        """Drain synchronously. A capture error can still be saved by finish()."""
        if self._started or self._finished:
            raise ValueError("A recorder accepts one stdout stream")
        self._started = True
        self._drain(stream, echo)
        if self._errors:
            raise ReportCaptureError("Headless report capture failed")

    def pump_in_thread(
        self, stream: _Stream, echo: _Echo | None
    ) -> threading.Thread:
        """Drain a subprocess pipe without blocking the parent's signal handling."""
        if self._started or self._finished:
            raise ValueError("A recorder accepts one stdout stream")
        descriptor = self._fileno(stream)
        if descriptor is None and not isinstance(stream, (io.StringIO, io.BytesIO)):
            raise ValueError(
                "Background report capture requires a pipe or an in-memory stream"
            )
        self._started = True

        def pump() -> None:
            try:
                self._drain(stream, echo)
            except Exception as exc:
                self._error(f"read-{type(exc).__name__}")
            finally:
                if descriptor is not None:
                    try:
                        getattr(stream, "close")()  # pipes expose close() with fileno()
                    except Exception as exc:
                        self._error(f"close-{type(exc).__name__}")

        self._thread = threading.Thread(
            target=pump, name="vaultlens-report", daemon=True
        )
        self._thread.start()
        return self._thread

    def finish(self, returncode: int) -> Path:
        """Save output after child cleanup; errors include any saved partial report."""
        if self._finished:
            raise ValueError("A report can only be finalized once")
        if not isinstance(returncode, int) or isinstance(returncode, bool):  # pyright: ignore[reportUnnecessaryIsInstance] -- runtime validation
            raise ValueError("Report result must be a process exit code")
        self._finished = True
        if self._thread is not None:
            self._thread.join(_DRAIN_SECONDS)
            if self._thread.is_alive():
                self.cleanup_unconfirmed = True
                self._error("stdout-eof-unconfirmed")
                self._stop.set()
                self._thread.join(1.0)
        with self._lock:
            text = bytes(self._body).decode("utf-8", errors="ignore")
            errors = tuple(self._errors)
            truncated = self.truncated
        # Obsidian Templater runs `<%` commands in newly created notes, outside
        # the sandbox. Agent output is untrusted, so never publish a live marker.
        text = text.replace(_TEMPLATER_OPEN, _TEMPLATER_ESCAPED)
        budget = self.max_bytes - _METADATA_BYTES
        encoded = text.encode("utf-8")
        if len(encoded) > budget:
            text = encoded[:budget].decode("utf-8", errors="ignore")
            truncated = True
        finished = datetime.now(timezone.utc)
        partial = bool(returncode or errors or truncated)
        summary = (
            f"Headless {self.role} via {self.provider} using {self.profile} exited "
            f"with code {returncode}; stdout capture is {'partial' if partial else 'complete'}."
        )
        metadata = {
            "type": "report",
            "status": "draft" if partial else "active",
            "title": f"Headless {self.role} {self.started:%Y-%m-%d %H:%M:%S} UTC",
            "created": f"{finished:%Y-%m-%d}",
            "updated": f"{finished:%Y-%m-%d}",
            "summary": summary,
            "tags": ["headless", "agent-report"],
            "agent_role": self.role,
            "provider": self.provider,
            "access_profile": self.profile,
            "started_at": self.started.isoformat(),
            "finished_at": finished.isoformat(),
            "result_code": returncode,
            "output_truncated": truncated,
            "capture_errors": errors,
            "cleanup_unconfirmed": self.cleanup_unconfirmed,
        }
        header = (
            "---\n"
            + "".join(
                f"{key}: {json.dumps(value)}\n" for key, value in metadata.items()
            )
            + "---\n\n"
        )
        provenance = "Captured from headless run stdout by the VaultLens launcher.\n\n"
        if returncode:
            provenance += (
                f"Run failed with exit code {returncode}; output may be partial.\n\n"
            )
        if truncated:
            provenance += "Output exceeded the report size limit and was truncated; the live stream was still drained.\n\n"
        if errors:
            provenance += "Output capture failed; this report contains the captured partial output.\n\n"
        payload = (header + provenance + text.rstrip() + "\n").encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ReportCaptureError("Report metadata exceeded the reserved size")
        try:
            path = _publish(self.scope, self.filename, payload)
        except (OSError, ValueError) as exc:
            raise ReportCaptureError(
                f"Cannot save headless report: {exc}",
                cleanup_unconfirmed=self.cleanup_unconfirmed,
            ) from exc
        if errors:
            raise ReportCaptureError(
                "Headless output capture failed; a partial report was saved",
                report_path=path,
                cleanup_unconfirmed=self.cleanup_unconfirmed,
            )
        return path
