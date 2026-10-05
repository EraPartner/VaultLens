#!/usr/bin/env python3
"""Trusted-parent report contracts using public in-memory and subprocess fixtures."""

import hashlib
import io
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_access import JsonObject, RunScope
import run_reports as reports

METADATA_BYTES = reports._METADATA_BYTES  # pyright: ignore[reportPrivateUsage] - tests assert the reserved metadata budget


def saved_path(error: reports.ReportCaptureError) -> Path:
    """The partial report a capture failure saved; its absence would be a test failure."""
    assert error.report_path is not None
    return error.report_path


def retained_bytes(recorder: reports.Recorder) -> int:
    """Length of the in-memory capture buffer, which the 4 MiB bound must cap."""
    return len(recorder._body)  # pyright: ignore[reportPrivateUsage] - tests assert the memory bound


class ReportTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable] - assigned in setUp
    scope: RunScope  # pyright: ignore[reportUninitializedInstanceVariable] - assigned in setUp

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="vaultlens-report-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scope = self.scope_at(self.root / "wiki/reports/agents")

    def scope_at(self, directory: Path, *, root: Path | None = None) -> RunScope:
        return RunScope(root or self.root, "selected-read", (), (), (), (), directory)

    def recorder(
        self,
        scope: RunScope | None = None,
        *,
        max_bytes: int = reports.MAX_REPORT_BYTES,
    ) -> reports.Recorder:
        return reports.Recorder(
            scope or self.scope, "search", "codex", max_bytes=max_bytes
        )

    @staticmethod
    def metadata(path: Path) -> tuple[JsonObject, str]:
        text = path.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        return {
            key: json.loads(value)
            for key, value in (
                line.split(": ", 1) for line in frontmatter.strip().splitlines()
            )
        }, text

    def test_complete_report_has_required_frontmatter_private_mode_and_echo(
        self,
    ) -> None:
        recorder = self.recorder()
        echo = io.StringIO()
        recorder.consume(io.BytesIO("Public fixture: café.\n".encode()), echo)
        path = recorder.finish(0)
        metadata, body = self.metadata(path)
        self.assertEqual(echo.getvalue(), "Public fixture: café.\n")
        self.assertEqual(metadata["type"], "report")
        self.assertEqual(metadata["status"], "active")
        self.assertEqual(metadata["provider"], "codex")
        self.assertEqual(metadata["access_profile"], "selected-read")
        self.assertEqual(metadata["result_code"], 0)
        self.assertFalse(metadata["output_truncated"])
        for field in ("title", "created", "updated", "summary"):
            self.assertTrue(metadata[field])
        self.assertIn("Public fixture: café.", body)
        self.assertRegex(
            path.name, r"^agent-\d{8}T\d{12}Z-search-selected-read-[0-9a-f]{16}\.md$"
        )
        self.assertEqual(path.parent, self.scope.reports)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_subprocess_pipe_is_drained_after_leader_eof(self) -> None:
        recorder = self.recorder()
        echo = io.StringIO()
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('Public fixture line.\\n' * 32768)",
            ],
            stdout=subprocess.PIPE,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        stdout = child.stdout
        assert stdout is not None
        thread = recorder.pump_in_thread(stdout, echo)
        code = child.wait(timeout=10)
        path = recorder.finish(code)
        self.assertFalse(thread.is_alive())
        self.assertTrue(recorder.eof)
        self.assertTrue(stdout.closed)
        expected = ("Public fixture line.\n" * 32768).encode()
        self.assertEqual(
            hashlib.sha256(echo.getvalue().encode()).hexdigest(),
            hashlib.sha256(expected).hexdigest(),
        )
        metadata, body = self.metadata(path)
        self.assertEqual(metadata["status"], "active")
        self.assertIn("Public fixture line.", body)

    def test_4_mib_limit_bounds_file_and_memory_without_stopping_echo(self) -> None:
        class CountingEcho:
            count: int = 0

            def write(self, value: str) -> int:
                self.count += len(value.encode())
                return len(value)

            def flush(self) -> None:
                pass

        recorder = self.recorder()
        echo = CountingEcho()
        payload = b"p" * (reports.MAX_REPORT_BYTES + 131072)
        recorder.consume(io.BytesIO(payload), echo)
        path = recorder.finish(0)
        self.assertEqual(echo.count, len(payload))
        self.assertLessEqual(
            retained_bytes(recorder), reports.MAX_REPORT_BYTES - METADATA_BYTES
        )
        self.assertLessEqual(path.stat().st_size, reports.MAX_REPORT_BYTES)
        metadata, body = self.metadata(path)
        self.assertTrue(metadata["output_truncated"])
        self.assertEqual(metadata["status"], "draft")
        self.assertIn("was truncated", body)
        self.assertTrue(recorder.eof)

    def test_failed_child_is_saved_as_partial_draft(self) -> None:
        recorder = self.recorder()
        recorder.consume(io.StringIO("Public failed-run fixture.\n"), io.StringIO())
        metadata, text = self.metadata(recorder.finish(7))
        self.assertEqual(metadata["result_code"], 7)
        self.assertEqual(metadata["status"], "draft")
        self.assertIn("Run failed with exit code 7", text)
        self.assertIn("capture is partial", metadata["summary"])

    def test_stream_read_error_surfaces_with_saved_partial_output(self) -> None:
        class BrokenInput:
            calls: int = 0

            def read(self, _size: int) -> str:
                self.calls += 1
                if self.calls == 1:
                    return "Public partial fixture.\n"
                raise OSError("Untrusted exception detail")

        recorder = self.recorder()
        with self.assertRaises(reports.ReportCaptureError):
            recorder.consume(BrokenInput(), io.StringIO())
        with self.assertRaises(reports.ReportCaptureError) as failure:
            recorder.finish(0)
        self.assertFalse(failure.exception.cleanup_unconfirmed)
        metadata, text = self.metadata(saved_path(failure.exception))
        self.assertEqual(metadata["capture_errors"], ["read-OSError"])
        self.assertEqual(metadata["status"], "draft")
        self.assertIn("Public partial fixture.", text)
        self.assertNotIn("Untrusted exception detail", text)

    def test_echo_error_still_drains_and_retains_the_stream(self) -> None:
        class BrokenEcho:
            def write(self, _text: str) -> int:
                raise OSError("Public echo failure")

            def flush(self) -> None:
                pass

        recorder = self.recorder()
        stream = io.BytesIO(b"p" * 65536)
        with self.assertRaises(reports.ReportCaptureError):
            recorder.consume(stream, BrokenEcho())
        self.assertTrue(recorder.eof)
        self.assertEqual(stream.tell(), 65536)
        self.assertEqual(retained_bytes(recorder), 65536)
        with self.assertRaises(reports.ReportCaptureError) as failure:
            recorder.finish(0)
        metadata, _ = self.metadata(saved_path(failure.exception))
        self.assertEqual(metadata["capture_errors"], ["echo-OSError"])

    def test_background_read_error_ends_thread_and_closes_pipe(self) -> None:
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        self.addCleanup(stream.close)
        self.addCleanup(lambda: os.close(write_fd))
        os.write(write_fd, b"Public pipe fixture")
        recorder = self.recorder()
        with mock.patch.object(
            os, "read", side_effect=OSError("Public read failure")
        ):
            thread = recorder.pump_in_thread(stream, io.StringIO())
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(stream.closed)
        with self.assertRaises(reports.ReportCaptureError) as failure:
            recorder.finish(0)
        self.assertEqual(
            self.metadata(saved_path(failure.exception))[0]["capture_errors"],
            ["read-OSError"],
        )

    def test_missing_eof_stops_pump_and_reports_unconfirmed_cleanup(self) -> None:
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        self.addCleanup(stream.close)
        self.addCleanup(lambda: os.close(write_fd))
        recorder = self.recorder()
        thread = recorder.pump_in_thread(stream, io.StringIO())
        with mock.patch.object(reports, "_DRAIN_SECONDS", 0.01):
            with self.assertRaises(reports.ReportCaptureError) as failure:
                recorder.finish(0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(stream.closed)
        self.assertTrue(failure.exception.cleanup_unconfirmed)
        self.assertEqual(
            self.metadata(saved_path(failure.exception))[0]["capture_errors"],
            ["stdout-eof-unconfirmed"],
        )

    def test_background_accepts_bounded_memory_streams(self) -> None:
        for stream in (io.StringIO("Public fixture"), io.BytesIO(b"Public fixture")):
            with self.subTest(stream=type(stream).__name__):
                recorder = self.recorder()
                thread = recorder.pump_in_thread(stream, io.StringIO())
                path = recorder.finish(0)
                self.assertFalse(thread.is_alive())
                self.assertTrue(path.is_file())

    def test_background_rejects_unknown_blocking_streams(self) -> None:
        class CustomStream:
            def read(self, _size: int) -> bytes:
                raise AssertionError("Unknown background stream was read")

        with self.assertRaisesRegex(ValueError, "requires a pipe"):
            self.recorder().pump_in_thread(CustomStream(), io.StringIO())

    def test_utf8_split_across_chunks_is_decoded_once(self) -> None:
        class Fragmented:
            remaining: Iterator[int] = iter("Public café fixture.\n".encode())

            def read(self, _size: int) -> bytes:
                return (
                    bytes([item])
                    if (item := next(self.remaining, None)) is not None
                    else b""
                )

        recorder = self.recorder()
        echo = io.StringIO()
        recorder.consume(Fragmented(), echo)
        self.assertEqual(echo.getvalue(), "Public café fixture.\n")
        self.assertIn("Public café fixture.", self.metadata(recorder.finish(0))[1])

    def test_truncated_utf8_never_expands_the_file_past_limit(self) -> None:
        recorder = self.recorder(max_bytes=METADATA_BYTES + 1)
        recorder.consume(io.BytesIO("éé".encode()), io.StringIO())
        path = recorder.finish(0)
        self.assertLessEqual(path.stat().st_size, recorder.max_bytes)
        self.assertNotIn("\ufffd", path.read_text())
        self.assertTrue(recorder.truncated)

    def test_paths_outside_report_subtree_or_with_traversal_are_rejected(self) -> None:
        for directory in (
            self.root / "projects/alpha/reports",
            self.root / "wiki/reports/isolated-agent",
            self.root / "wiki/reports",
            self.root.parent / "outside-public-reports",
            self.root / "wiki/reports/../concepts",
            Path("wiki/reports"),
        ):
            with self.subTest(directory=directory):
                with self.assertRaises(reports.ReportCaptureError):
                    self.recorder(self.scope_at(directory)).finish(0)
        self.assertFalse((self.root / "wiki").exists())

    def test_nested_folder_within_reserved_report_subtree_is_supported(self) -> None:
        scope = self.scope_at(self.root / "wiki/reports/agents/selected")
        recorder = self.recorder(scope)
        recorder.consume(io.StringIO("Public nested report fixture.\n"), io.StringIO())
        path = recorder.finish(0)
        self.assertEqual(path.parent, scope.reports)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_report_parent_symlinks_are_not_followed(self) -> None:
        outside = self.root / "public-outside"
        outside.mkdir()
        for prefix in ("wiki", "wiki/reports", "wiki/reports/agents"):
            case_root = self.root / prefix.replace("/", "-")
            case_root.mkdir()
            link = case_root / prefix
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(outside, target_is_directory=True)
            scope = self.scope_at(case_root / "wiki/reports/agents", root=case_root)
            with (
                self.subTest(prefix=prefix),
                self.assertRaises(reports.ReportCaptureError),
            ):
                self.recorder(scope).finish(0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_vault_root_symlink_is_not_followed(self) -> None:
        alias = self.root / "public-vault-alias"
        actual = self.root / "public-actual-vault"
        actual.mkdir()
        alias.symlink_to(actual, target_is_directory=True)
        scope = self.scope_at(alias / "wiki/reports/agents", root=alias)
        with self.assertRaises(reports.ReportCaptureError):
            self.recorder(scope).finish(0)
        self.assertEqual(list(actual.iterdir()), [])

    def test_existing_target_link_or_file_is_never_modified(self) -> None:
        self.scope.reports.mkdir(parents=True)
        outside = self.root / "public-outside.md"
        outside.write_text("Public protected fixture")
        for kind in ("symlink", "hardlink", "file"):
            recorder = self.recorder()
            target = self.scope.reports / recorder.filename
            if kind == "symlink":
                target.symlink_to(outside)
            elif kind == "hardlink":
                os.link(outside, target)
            else:
                target.write_text("Public existing report")
            with self.subTest(kind=kind), self.assertRaises(reports.ReportCaptureError):
                recorder.finish(0)
            self.assertEqual(outside.read_text(), "Public protected fixture")
            self.assertTrue(target.exists())
            self.assertFalse(
                any(
                    path.name.endswith(".part") for path in self.scope.reports.iterdir()
                )
            )

    def test_regular_hardlink_in_parent_position_is_not_written(self) -> None:
        outside = self.root / "public-file"
        outside.write_text("Public protected fixture")
        os.link(outside, self.root / "wiki")
        with self.assertRaises(reports.ReportCaptureError):
            self.recorder().finish(0)
        self.assertEqual(outside.read_text(), "Public protected fixture")

    def test_temporary_name_collision_preserves_existing_file(self) -> None:
        recorder = self.recorder()
        self.scope.reports.mkdir(parents=True)
        temporary = self.scope.reports / f".{recorder.filename}.publicnonce.part"
        temporary.write_text("Public existing temporary fixture")
        with mock.patch.object(
            secrets, "token_hex", return_value="publicnonce"
        ):
            with self.assertRaises(reports.ReportCaptureError):
                recorder.finish(0)
        self.assertEqual(temporary.read_text(), "Public existing temporary fixture")
        self.assertEqual(list(self.scope.reports.iterdir()), [temporary])

    def test_failed_disk_write_leaves_no_partial_published_file(self) -> None:
        with mock.patch.object(os, "write", return_value=0):
            with self.assertRaises(reports.ReportCaptureError):
                self.recorder().finish(0)
        self.assertEqual(list(self.scope.reports.iterdir()), [])

    def test_unsafe_labels_and_size_configuration_are_rejected(self) -> None:
        for value in ("../search", "bad label", "", "a" * 65):
            with self.subTest(label=value), self.assertRaises(ValueError):
                reports.Recorder(self.scope, value, "codex")
        for maximum in (
            0,
            METADATA_BYTES,
            reports.MAX_REPORT_BYTES + 1,
            "4096",
        ):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                reports.Recorder(
                    self.scope,
                    "search",
                    "codex",
                    max_bytes=maximum,  # pyright: ignore[reportArgumentType] -- "4096" deliberately violates the int contract
                )

    def test_stream_and_finalization_are_each_single_use(self) -> None:
        recorder = self.recorder()
        recorder.consume(io.StringIO("Public fixture"), io.StringIO())
        with self.assertRaises(ValueError):
            recorder.consume(io.StringIO("Public duplicate"), io.StringIO())
        path = recorder.finish(0)
        with self.assertRaises(ValueError):
            recorder.finish(0)
        self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
