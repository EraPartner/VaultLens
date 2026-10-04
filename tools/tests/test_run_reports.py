#!/usr/bin/env python3
"""Trusted-parent report contracts using public in-memory and subprocess fixtures."""

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_access import RunScope
import run_reports as reports


class ReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vaultlens-report-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scope = self.scope_at(self.root / "wiki/reports/agents")

    def scope_at(self, directory, *, root=None):
        return RunScope(root or self.root, "selected-read", (), (), (), (), directory)

    def recorder(self, scope=None, **kwargs):
        return reports.Recorder(scope or self.scope, "search", "codex", **kwargs)

    @staticmethod
    def metadata(path):
        text = path.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        return {
            key: json.loads(value)
            for key, value in (
                line.split(": ", 1) for line in frontmatter.strip().splitlines()
            )
        }, text

    def test_complete_report_has_required_frontmatter_private_mode_and_echo(self):
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

    def test_subprocess_pipe_is_drained_after_leader_eof(self):
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
        thread = recorder.pump_in_thread(child.stdout, echo)
        code = child.wait(timeout=10)
        path = recorder.finish(code)
        self.assertFalse(thread.is_alive())
        self.assertTrue(recorder.eof)
        self.assertTrue(child.stdout.closed)
        expected = ("Public fixture line.\n" * 32768).encode()
        self.assertEqual(
            hashlib.sha256(echo.getvalue().encode()).hexdigest(),
            hashlib.sha256(expected).hexdigest(),
        )
        metadata, body = self.metadata(path)
        self.assertEqual(metadata["status"], "active")
        self.assertIn("Public fixture line.", body)

    def test_4_mib_limit_bounds_file_and_memory_without_stopping_echo(self):
        class CountingEcho:
            count = 0

            def write(self, value):
                self.count += len(value.encode())

            def flush(self):
                pass

        recorder = self.recorder()
        echo = CountingEcho()
        payload = b"p" * (reports.MAX_REPORT_BYTES + 131072)
        recorder.consume(io.BytesIO(payload), echo)
        path = recorder.finish(0)
        self.assertEqual(echo.count, len(payload))
        self.assertLessEqual(
            len(recorder._body), reports.MAX_REPORT_BYTES - reports._METADATA_BYTES
        )
        self.assertLessEqual(path.stat().st_size, reports.MAX_REPORT_BYTES)
        metadata, body = self.metadata(path)
        self.assertTrue(metadata["output_truncated"])
        self.assertEqual(metadata["status"], "draft")
        self.assertIn("was truncated", body)
        self.assertTrue(recorder.eof)

    def test_failed_child_is_saved_as_partial_draft(self):
        recorder = self.recorder()
        recorder.consume(io.StringIO("Public failed-run fixture.\n"), io.StringIO())
        metadata, text = self.metadata(recorder.finish(7))
        self.assertEqual(metadata["result_code"], 7)
        self.assertEqual(metadata["status"], "draft")
        self.assertIn("Run failed with exit code 7", text)
        self.assertIn("capture is partial", metadata["summary"])

    def test_stream_read_error_surfaces_with_saved_partial_output(self):
        class BrokenInput:
            calls = 0

            def read(self, _size):
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
        metadata, text = self.metadata(failure.exception.report_path)
        self.assertEqual(metadata["capture_errors"], ["read-OSError"])
        self.assertEqual(metadata["status"], "draft")
        self.assertIn("Public partial fixture.", text)
        self.assertNotIn("Untrusted exception detail", text)

    def test_echo_error_still_drains_and_retains_the_stream(self):
        class BrokenEcho:
            def write(self, _text):
                raise OSError("Public echo failure")

        recorder = self.recorder()
        stream = io.BytesIO(b"p" * 65536)
        with self.assertRaises(reports.ReportCaptureError):
            recorder.consume(stream, BrokenEcho())
        self.assertTrue(recorder.eof)
        self.assertEqual(stream.tell(), 65536)
        self.assertEqual(len(recorder._body), 65536)
        with self.assertRaises(reports.ReportCaptureError) as failure:
            recorder.finish(0)
        metadata, _ = self.metadata(failure.exception.report_path)
        self.assertEqual(metadata["capture_errors"], ["echo-OSError"])

    def test_background_read_error_ends_thread_and_closes_pipe(self):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        self.addCleanup(stream.close)
        self.addCleanup(lambda: os.close(write_fd))
        os.write(write_fd, b"Public pipe fixture")
        recorder = self.recorder()
        with mock.patch.object(
            reports.os, "read", side_effect=OSError("Public read failure")
        ):
            thread = recorder.pump_in_thread(stream, io.StringIO())
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(stream.closed)
        with self.assertRaises(reports.ReportCaptureError) as failure:
            recorder.finish(0)
        self.assertEqual(
            self.metadata(failure.exception.report_path)[0]["capture_errors"],
            ["read-OSError"],
        )

    def test_missing_eof_stops_pump_and_reports_unconfirmed_cleanup(self):
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
            self.metadata(failure.exception.report_path)[0]["capture_errors"],
            ["stdout-eof-unconfirmed"],
        )

    def test_background_accepts_bounded_memory_streams(self):
        for stream in (io.StringIO("Public fixture"), io.BytesIO(b"Public fixture")):
            with self.subTest(stream=type(stream).__name__):
                recorder = self.recorder()
                thread = recorder.pump_in_thread(stream, io.StringIO())
                path = recorder.finish(0)
                self.assertFalse(thread.is_alive())
                self.assertTrue(path.is_file())

    def test_background_rejects_unknown_blocking_streams(self):
        class CustomStream:
            def read(self, _size):
                raise AssertionError("Unknown background stream was read")

        with self.assertRaisesRegex(ValueError, "requires a pipe"):
            self.recorder().pump_in_thread(CustomStream(), io.StringIO())

    def test_utf8_split_across_chunks_is_decoded_once(self):
        class Fragmented:
            remaining = iter("Public café fixture.\n".encode())

            def read(self, _size):
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

    def test_truncated_utf8_never_expands_the_file_past_limit(self):
        recorder = self.recorder(max_bytes=reports._METADATA_BYTES + 1)
        recorder.consume(io.BytesIO("éé".encode()), io.StringIO())
        path = recorder.finish(0)
        self.assertLessEqual(path.stat().st_size, recorder.max_bytes)
        self.assertNotIn("\ufffd", path.read_text())
        self.assertTrue(recorder.truncated)

    def test_paths_outside_report_subtree_or_with_traversal_are_rejected(self):
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

    def test_nested_folder_within_reserved_report_subtree_is_supported(self):
        scope = self.scope_at(self.root / "wiki/reports/agents/selected")
        recorder = self.recorder(scope)
        recorder.consume(io.StringIO("Public nested report fixture.\n"), io.StringIO())
        path = recorder.finish(0)
        self.assertEqual(path.parent, scope.reports)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_report_parent_symlinks_are_not_followed(self):
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

    def test_vault_root_symlink_is_not_followed(self):
        alias = self.root / "public-vault-alias"
        actual = self.root / "public-actual-vault"
        actual.mkdir()
        alias.symlink_to(actual, target_is_directory=True)
        scope = self.scope_at(alias / "wiki/reports/agents", root=alias)
        with self.assertRaises(reports.ReportCaptureError):
            self.recorder(scope).finish(0)
        self.assertEqual(list(actual.iterdir()), [])

    def test_existing_target_link_or_file_is_never_modified(self):
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

    def test_regular_hardlink_in_parent_position_is_not_written(self):
        outside = self.root / "public-file"
        outside.write_text("Public protected fixture")
        os.link(outside, self.root / "wiki")
        with self.assertRaises(reports.ReportCaptureError):
            self.recorder().finish(0)
        self.assertEqual(outside.read_text(), "Public protected fixture")

    def test_temporary_name_collision_preserves_existing_file(self):
        recorder = self.recorder()
        self.scope.reports.mkdir(parents=True)
        temporary = self.scope.reports / f".{recorder.filename}.publicnonce.part"
        temporary.write_text("Public existing temporary fixture")
        with mock.patch.object(
            reports.secrets, "token_hex", return_value="publicnonce"
        ):
            with self.assertRaises(reports.ReportCaptureError):
                recorder.finish(0)
        self.assertEqual(temporary.read_text(), "Public existing temporary fixture")
        self.assertEqual(list(self.scope.reports.iterdir()), [temporary])

    def test_failed_disk_write_leaves_no_partial_published_file(self):
        with mock.patch.object(reports.os, "write", return_value=0):
            with self.assertRaises(reports.ReportCaptureError):
                self.recorder().finish(0)
        self.assertEqual(list(self.scope.reports.iterdir()), [])

    def test_unsafe_labels_and_size_configuration_are_rejected(self):
        for value in ("../search", "bad label", "", "a" * 65):
            with self.subTest(label=value), self.assertRaises(ValueError):
                reports.Recorder(self.scope, value, "codex")
        for maximum in (
            0,
            reports._METADATA_BYTES,
            reports.MAX_REPORT_BYTES + 1,
            "4096",
        ):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                self.recorder(max_bytes=maximum)

    def test_stream_and_finalization_are_each_single_use(self):
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
