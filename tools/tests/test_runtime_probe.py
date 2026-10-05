#!/usr/bin/env python3
"""Harness and synthetic fixture tests. These are not operating-system evidence."""

from __future__ import annotations

import email.message
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

from _loader import load_module

if TYPE_CHECKING:  # the tools directory is on sys.path only after load_module runs
    from local_access import JsonObject

SCRIPT = Path(__file__).resolve().parents[1] / "runtime/probe.py"
probe = load_module("runtime_probe", SCRIPT)


class ProbeHarnessTests(unittest.TestCase):
    """Mocks validate control flow, never the sandbox's OS behavior."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.directory: tempfile.TemporaryDirectory[str]
        self.base: Path
        self.source: Path
        self.unrelated: Path
        self.fixture_base: Path

    def setUp(self) -> None:
        for name, value in (
            (
                "_macos_host_metadata",
                {
                    "host_audit_session": 999,
                    "escape_job_label": "com.vaultlens.synthetic",
                },
            ),
            (
                "_macos_ipc_create",
                {"host_shm": "/synthetic-shm", "host_sem": "/synthetic-sem"},
            ),
            ("_macos_ipc_cleanup", None),
        ):
            patch = mock.patch.object(probe, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.directory = tempfile.TemporaryDirectory(prefix="probe harness fixtures ")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()
        self.source = self.base / "checkout"
        self.source.mkdir()
        for relative in probe.TRUSTED_FILES:
            destination = self.source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(probe.ROOT / relative, destination)
        self.unrelated = self.source / "wiki/private-note.md"
        self.unrelated.parent.mkdir()
        self.unrelated.write_text("UNRELATED_PRIVATE_FIXTURE")
        (self.source / "tools/access.local.json").write_text(
            "unrelated local configuration"
        )
        self.fixture_base = self.base / "synthetic"
        self.fixture_base.mkdir()

    def fixture(self) -> tuple[Path, JsonObject]:
        return probe.make_fixture(
            self.source,
            self.fixture_base,
            {
                "status": "skipped",
                "detail": "Synthetic baseline intentionally unverified",
            },
        )

    def test_fixture_runtime_dependencies_import_without_host_tools(self) -> None:
        root, _ = self.fixture()
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.path.insert(0,sys.argv[1]); import local_runtime; "
                "assert local_runtime.__file__.startswith(sys.argv[1])",
                str(root / "tools"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_runtime_stops_before_fixture_network_or_provider_state(self) -> None:
        with (
            mock.patch.object(
                probe.local_runtime,
                "runtime_executable",
                side_effect=ValueError("Pinned runtime missing"),
            ),
            mock.patch.object(probe, "make_fixture") as make,
            mock.patch.object(probe, "public_baseline") as network,
            mock.patch.object(probe, "run_case") as run,
        ):
            report = probe.probe(self.source)
        make.assert_not_called()
        network.assert_not_called()
        run.assert_not_called()
        self.assertFalse(report["os_isolation_verified"])
        self.assertFalse(report["provider_file_tools_verified"])
        self.assertEqual(report["checks"][0]["status"], "failed")
        self.assertTrue(
            all(item["status"] == "skipped" for item in report["checks"][1:])
        )
        self.assertFalse((self.source / "tools/runtime-state").exists())

    def test_explicit_failed_probe_revokes_old_receipt_without_reading_it(self) -> None:
        state = self.source / "tools/runtime-state"
        state.mkdir(mode=0o700)
        receipt = state / "verification.json"
        receipt.write_text("UNREAD_SYNTHETIC_OLD_RECEIPT")
        receipt.chmod(0o600)
        report = probe.probe(self.source)
        self.assertFalse(receipt.exists())
        self.assertFalse(report["os_isolation_verified"])

    def test_lifecycle_declares_both_normal_and_cancel_inner_and_outer_cases(self) -> None:
        self.assertEqual(len(probe.LIFECYCLE_CHECKS), 8)
        expected = probe.expected_checks()
        self.assertEqual(len(expected), len(set(expected)))
        self.assertTrue(set(probe.LIFECYCLE_CHECKS).issubset(expected))

    def test_macos_receipts_require_guard_denials_and_linux_does_not_invent_them(self) -> None:
        linux = set(probe.expected_checks(platform="linux"))
        macos = set(probe.expected_checks(platform="darwin"))
        guard = {"selected-read." + name for name in probe.MACOS_GUARD_CHECKS}
        self.assertEqual(macos - linux, guard)
        self.assertTrue(linux.issubset(macos))
        self.assertIn("selected-read.macos.host-shared-memory-read", guard)
        self.assertIn("selected-read.macos.host-semaphore-open", guard)

    def test_lifecycle_refuses_natural_expiry_as_cleanup_proof(self) -> None:
        heartbeat = self.base / "public-heartbeat.json"
        record = {"status": "spawned", "pid": 123456789, "token": "synthetic"}
        heartbeat.write_text(
            json.dumps(
                {
                    "pid": 123456789,
                    "token": "synthetic",
                    "count": 1,
                    "started": 0,
                    "expires": 45,
                }
            )
        )
        with (
            mock.patch.object(probe.time, "monotonic", return_value=46),
            self.assertRaisesRegex(AssertionError, "expiry"),
        ):
            probe._stopped_heartbeat(record, heartbeat)

    def test_lifecycle_rejects_live_descendant_even_with_stopped_heartbeat(self) -> None:
        heartbeat = self.base / "public-heartbeat.json"
        record = {"status": "spawned", "pid": 123456789, "token": "synthetic"}
        heartbeat.write_text(
            json.dumps(
                {
                    "pid": 123456789,
                    "token": "synthetic",
                    "count": 1,
                    "started": 0,
                    "expires": 45,
                }
            )
        )
        with (
            mock.patch.object(probe.time, "monotonic", return_value=1),
            mock.patch.object(probe.time, "sleep"),
            mock.patch.object(probe, "_owned_fixture_process", return_value=True),
            self.assertRaisesRegex(AssertionError, "survived"),
        ):
            probe._stopped_heartbeat(record, heartbeat)

    def test_cleanup_refuses_reused_or_unknown_process_identity_without_signalling(
        self,
    ) -> None:
        record = {
            "pid": 123456789,
            "group_id": 123456789,
            "script": "/tmp/synthetic-probe.py",
            "token": "fixture-token",
        }
        result = subprocess.CompletedProcess([], 0, "S unrelated-process", "")
        with (
            mock.patch.object(probe, "_subprocess", return_value=result),
            mock.patch.object(probe.os, "kill") as kill,
            self.assertRaises(probe.ProcessCleanupError),
        ):
            probe._cleanup_fixture_process(record)
        kill.assert_not_called()

    def test_existing_cancellation_gate_stops_before_runtime_or_fixture(self) -> None:
        marker = self.source / "tools/runtime-state/cancellation-unconfirmed.json"
        marker.parent.mkdir()
        marker.write_text('{"group_id":123456789}')
        before = marker.read_bytes()
        with mock.patch.object(probe.local_runtime, "runtime_executable") as runtime:
            report = probe.probe(self.source)
        runtime.assert_not_called()
        self.assertEqual(marker.read_bytes(), before)
        self.assertFalse(report["os_isolation_verified"])
        self.assertIn("unconfirmed", report["checks"][0]["detail"])

    def test_fixture_copies_only_reviewed_tools_and_synthetic_documents(self) -> None:
        root, metadata = self.fixture()
        self.assertEqual(
            set(metadata["trusted_source_sha256"]), set(probe.TRUSTED_FILES)
        )
        self.assertEqual(
            (root / "tools/runtime-node").readlink(), self.source / "tools/runtime-node"
        )
        self.assertNotIn(
            "UNRELATED_PRIVATE_FIXTURE", (root / "wiki/approved/note.md").read_text()
        )
        self.assertFalse((root / "wiki/private-note.md").exists())
        self.assertFalse((root / "tools/runtime-state/providers").exists())
        self.assertEqual(
            json.loads((root / "tools/access.local.json").read_text())["version"], 1
        )
        self.assertIn(
            "STALEINDEXPROBETOKEN",
            (root / "tools/runtime-state/index/copied-note.md").read_text(),
        )
        self.assertEqual(
            (root / "wiki/approved/external-symlink.md").readlink(),
            root.parent / "excluded.md",
        )
        if metadata["hardlinks"]:
            self.assertEqual(
                (root / "wiki/approved/external-hardlink.md").stat().st_ino,
                (root.parent / "excluded.md").stat().st_ino,
            )

    def test_fixture_rejects_symlink_or_hardlink_trusted_sources(self) -> None:
        source = self.source / "tools/runtime/check_boundary.py"
        data = source.read_bytes()
        original = self.base / "source-copy.py"
        original.write_bytes(data)
        source.unlink()
        source.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.fixture()
        source.unlink()
        os.link(original, source)
        with self.assertRaisesRegex(ValueError, "single-link"):
            probe._regular_source(source)

    def test_fixture_profiles_exclude_selected_and_future_denied_material(self) -> None:
        root, _metadata = self.fixture()
        selected = probe.resolve_scope(root, "probe-selected-read")
        self.assertTrue(selected.readable(root / "wiki/approved/note.md"))
        self.assertFalse(selected.readable(root / "wiki/unselected/note.md"))
        self.assertFalse(selected.writable(root / "wiki/approved/note.md"))
        project = probe.resolve_scope(root, "probe-project-write", project="selected")
        self.assertTrue(project.writable(root / "projects/selected/new.md"))
        self.assertFalse(project.writable(root / "wiki/approved/note.md"))
        self.assertFalse(project.readable(root / "projects/sibling/note.md"))
        self.assertFalse(project.writable(root / "raw/sources/source.md"))
        future = root / "projects/selected/private/later.md"
        future.write_text("RESTRICTEDPROBETOKEN")
        self.assertFalse(project.readable(future))
        self.assertFalse(project.writable(future))

    def test_declared_checks_match_all_child_operations(self) -> None:
        root, metadata = self.fixture()
        run = self.base / "run"
        with mock.patch.dict(
            os.environ, {"VAULTLENS_RUNTIME_MANIFEST": str(run / "scope.json")}
        ):
            for case in probe.CASE_NAMES:
                scope = probe.resolve_scope(
                    root,
                    "probe-" + case,
                    project="selected" if case == "project-write" else None,
                )
                actual = {
                    case + "." + name
                    for name, _operation in probe.child_checks(scope, metadata, case)
                }
                host_checks: set[str] = (
                    {"selected-read.macos.job-absent"}
                    if case == "selected-read" and probe.sys.platform == "darwin"
                    else set()
                )
                self.assertEqual(actual | host_checks, set(probe._expected(case)))

    def test_permission_denial_helper_refuses_missing_paths_or_success(self) -> None:
        probe._denied(mock.Mock(side_effect=PermissionError(errno.EACCES, "denied")))
        with self.assertRaises(FileNotFoundError):
            probe._denied(
                mock.Mock(side_effect=FileNotFoundError(errno.ENOENT, "missing"))
            )
        with self.assertRaises(AssertionError):
            probe._denied(lambda: None)

    def test_spawned_shell_requires_permission_evidence_and_passes_paths_as_arguments(
        self,
    ) -> None:
        literal_path = Path("/tmp/literal $(name) `value` note.md")
        result = subprocess.CompletedProcess([], 1, "", "cat: Operation not permitted")
        with mock.patch.object(probe, "_subprocess", return_value=result) as command:
            probe._shell(literal_path, write=False, allowed=False)
        self.assertEqual(command.call_args.args[0][-1], str(literal_path))
        self.assertNotIn(str(literal_path), command.call_args.args[0][2])
        result.stderr = "cat: No such file or directory"
        with (
            mock.patch.object(probe, "_subprocess", return_value=result),
            self.assertRaisesRegex(AssertionError, "without evidence"),
        ):
            probe._shell(literal_path, write=False, allowed=False)

    def test_dns_timeouts_and_generic_http_errors_are_not_denial_proof(self) -> None:
        values = (
            urllib.error.URLError("Name or service not known"),
            TimeoutError("timed out"),
            urllib.error.HTTPError(probe.PUBLIC_URL, 403, "Forbidden", email.message.Message(), None),
        )
        for value in values:
            self.assertEqual(probe.network_error_kind(value), "unavailable")
        self.assertEqual(
            probe.network_error_kind(
                urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
            ),
            "denied",
        )
        self.assertEqual(
            probe.network_error_kind(PermissionError(errno.EPERM, "denied")), "denied"
        )

    def test_network_missing_baseline_skips_without_making_request(self) -> None:
        metadata = {
            "network_baseline": {"status": "skipped", "detail": "DNS unavailable"}
        }
        with (
            mock.patch.object(probe, "_subprocess") as request,
            self.assertRaises(probe.SkippedCheck),
        ):
            probe._network_http(metadata)
        request.assert_not_called()

    def test_partial_mocked_results_never_mark_os_isolation_verified(self) -> None:
        with (
            mock.patch.object(
                probe.local_runtime,
                "runtime_executable",
                return_value=Path("/synthetic/runtime"),
            ),
            mock.patch.object(
                probe, "public_baseline", return_value={"status": "skipped"}
            ),
            mock.patch.object(probe, "run_case", return_value=[]),
            mock.patch.object(
                probe.verification,
                "fingerprint_runtime",
                return_value={"harness_only": True},
            ),
            mock.patch.object(probe, "lifecycle_checks", return_value=[]),
            mock.patch.object(probe.verification, "record_verified_probe") as record,
        ):
            report = probe.probe(self.source)
        self.assertFalse(report["os_isolation_verified"])
        self.assertFalse(report["provider_file_tools_verified"])
        self.assertTrue(any(item["status"] == "skipped" for item in report["checks"]))
        record.assert_not_called()

    def test_unconfirmed_cleanup_retains_fixture_and_persists_launch_gate(self) -> None:
        failure = probe.ProcessCleanupError(
            "Synthetic descendant cleanup unconfirmed", group_id=123456789
        )
        with (
            mock.patch.object(
                probe.local_runtime,
                "runtime_executable",
                return_value=Path("/synthetic/runtime"),
            ),
            mock.patch.object(
                probe, "public_baseline", return_value={"status": "skipped"}
            ),
            mock.patch.object(probe, "run_case", side_effect=failure),
            mock.patch.object(
                probe.verification,
                "fingerprint_runtime",
                return_value={"harness_only": True},
            ),
        ):
            report = probe.probe(self.source)
        retained = Path(report["fixture_retained"])
        self.addCleanup(shutil.rmtree, retained, True)
        self.assertTrue((retained / "synthetic-vault/wiki/approved/note.md").is_file())
        marker = self.source / "tools/runtime-state/cancellation-unconfirmed.json"
        self.assertEqual(json.loads(marker.read_text())["group_id"], 123456789)
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertFalse(report["os_isolation_verified"])


if __name__ == "__main__":
    unittest.main()
