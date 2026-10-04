#!/usr/bin/env python3
"""Receipt contracts in disposable public fixtures, never production evidence.

Host fingerprinting is mocked unless testing public synthetic artifacts directly.
No provider, authentication, Node, helper executable or OS probe is invoked.
"""

import copy
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

import runtime_verification as verification
from runtime_probe_checks import expected_checks

REAL_FINGERPRINT = verification._fingerprint_runtime


class RuntimeVerificationTests(unittest.TestCase):
    VERSION = "public-fixture-version"
    CHECKS = expected_checks()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vaultlens-verification-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "tools").mkdir()
        self.receipt = self.root / "tools/runtime-state/verification.json"
        self.report = {
            "os_isolation_verified": True,
            "checks": [{"check": name, "status": "passed"} for name in self.CHECKS],
        }
        self.evidence = {
            "public_synthetic_fixture": "not-operating-system-evidence",
            "runtime_version": self.VERSION,
            "artifacts": {"public.js": "public-original-hash"},
        }
        self.report["runtime_fingerprint"] = copy.deepcopy(self.evidence)
        fingerprint_patch = mock.patch.object(
            verification, "_fingerprint_runtime", return_value=self.evidence
        )
        self.fingerprint = fingerprint_patch.start()
        self.addCleanup(fingerprint_patch.stop)

    def record(self, report=None, expected=None):
        return verification.record_verified_probe(
            self.root,
            self.VERSION,
            self.report if report is None else report,
            self.CHECKS if expected is None else expected,
        )

    def rewrite(self, data):
        self.receipt.write_text(json.dumps(data))
        self.receipt.chmod(0o600)

    def installation(self):
        tree = self.root / "tools/runtime-node"
        package = tree / "node_modules/@anthropic-ai/sandbox-runtime"
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"version": self.VERSION}))
        script = package / "dist/cli.js"
        script.write_text("// Public synthetic runtime artifact\n")
        script.chmod(0o755)
        (tree / "node_modules/.bin").mkdir()
        (tree / "node_modules/.bin/srt").symlink_to(
            "../@anthropic-ai/sandbox-runtime/dist/cli.js"
        )
        for relative in verification.PUBLIC_SOURCES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Public synthetic source: " + relative + "\n")
        executable = self.root / "public-python"
        executable.write_text("public executable fixture; never executed\n")
        executable.chmod(0o755)
        return tree, package, script, executable

    def test_complete_probe_records_atomic_private_receipt_and_launch_accepts_it(self):
        path = self.record()
        self.assertEqual(path, self.receipt)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(path.stat().st_nlink, 1)
        data = json.loads(path.read_text())
        self.assertEqual(data["root"], str(self.root))
        self.assertEqual(data["runtime_version"], self.VERSION)
        self.assertEqual(data["checks"], sorted(self.CHECKS))
        self.assertEqual(data["fingerprint"], self.evidence)
        self.assertEqual(list(path.parent.iterdir()), [path])
        verification.require_verified_runtime(self.root, self.VERSION)

    def test_receipt_reader_requires_lifecycle_network_and_every_profile_check(self):
        self.record()
        original = json.loads(self.receipt.read_text())
        self.fingerprint.reset_mock()
        omitted = (
            "lifecycle.inner.detached.cancellation",
            "selected-read.network.direct-socket",
            "wiki-write.notes.profile-write",
            "project-write.project.future-denied-child-read",
        )
        for name in omitted:
            with self.subTest(omitted=name):
                self.assertIn(name, self.CHECKS)
                self.rewrite(
                    {
                        **original,
                        "checks": [item for item in original["checks"] if item != name],
                    }
                )
                with self.assertRaisesRegex(
                    ValueError, "exactly the expected unique checks"
                ):
                    verification.require_verified_runtime(self.root, self.VERSION)
        for invented in (
            ["public.invented-check"],
            [*self.CHECKS[:-1], "public.invented-check"],
        ):
            with self.subTest(invented=invented[-1]):
                self.rewrite({**original, "checks": invented})
                with self.assertRaises(ValueError):
                    verification.require_verified_runtime(self.root, self.VERSION)
        self.fingerprint.assert_not_called()

    def test_receipt_writer_cannot_accept_caller_defined_or_truncated_expected_set(
        self,
    ):
        for omitted in (
            "lifecycle.outer.attached.normal-success",
            "selected-read.network.http",
            "project-write.notes.profile-write",
        ):
            with self.subTest(omitted=omitted):
                shortened = tuple(name for name in self.CHECKS if name != omitted)
                report = copy.deepcopy(self.report)
                report["checks"] = [
                    item for item in report["checks"] if item["check"] != omitted
                ]
                with self.assertRaisesRegex(ValueError, "canonical probe check set"):
                    self.record(report, expected=shortened)
                with self.assertRaisesRegex(
                    ValueError, "exactly the expected unique checks"
                ):
                    self.record(report)
        report = copy.deepcopy(self.report)
        report["checks"] = [{"check": "public.invented-check", "status": "passed"}]
        with self.assertRaisesRegex(ValueError, "canonical probe check set"):
            self.record(report, expected=("public.invented-check",))
        self.assertFalse(self.receipt.exists())
        self.fingerprint.assert_not_called()

    def test_incomplete_failed_skipped_duplicate_and_extra_checks_never_record(self):
        reports = []
        for value in (False, 1, "true", None):
            report = copy.deepcopy(self.report)
            report["os_isolation_verified"] = value
            reports.append(report)
        reports.extend(
            (
                {},
                {"os_isolation_verified": True, "checks": []},
                {"os_isolation_verified": True, "checks": self.report["checks"][:-1]},
            )
        )
        for status in ("failed", "skipped", "unverified", True):
            report = copy.deepcopy(self.report)
            report["checks"][0]["status"] = status
            reports.append(report)
        report = copy.deepcopy(self.report)
        report["checks"].append(copy.deepcopy(report["checks"][0]))
        reports.append(report)
        report = copy.deepcopy(self.report)
        report["checks"].append({"check": "public.unexpected", "status": "passed"})
        reports.append(report)
        for report in reports:
            with self.subTest(report=report):
                with self.assertRaises(ValueError):
                    self.record(report)
                self.assertFalse(self.receipt.exists())
        self.fingerprint.assert_not_called()

    def test_receipt_requires_unchanged_preprobe_runtime_fingerprint(self):
        report = copy.deepcopy(self.report)
        del report["runtime_fingerprint"]
        with self.assertRaisesRegex(ValueError, "captured before the probe"):
            self.record(report)
        self.fingerprint.assert_not_called()
        self.assertFalse(self.receipt.exists())
        self.fingerprint.return_value = {
            **self.evidence,
            "artifacts": {"public.js": "public-change-during-probe"},
        }
        with self.assertRaisesRegex(ValueError, "changed during the probe"):
            self.record()
        self.assertFalse(self.receipt.exists())

    def test_expected_names_must_be_nonempty_unique_and_exact(self):
        for expected in (
            [],
            [""],
            "public.check",
            [*self.CHECKS, self.CHECKS[0]],
            ["public.other"],
            [1],
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError):
                    self.record(expected=expected)
        self.assertFalse(self.receipt.exists())

    def test_missing_unreadable_or_empty_receipt_blocks_without_fingerprinting(self):
        with self.assertRaises(ValueError):
            verification.require_verified_runtime(self.root, self.VERSION)
        self.receipt.parent.mkdir(mode=0o700)
        self.receipt.write_text("")
        self.receipt.chmod(0o600)
        with self.assertRaises(ValueError):
            verification.require_verified_runtime(self.root, self.VERSION)
        with mock.patch.object(
            verification,
            "_bytes",
            side_effect=PermissionError("public unreadable fixture"),
        ):
            with self.assertRaises(ValueError):
                verification.require_verified_runtime(self.root, self.VERSION)
        self.fingerprint.assert_not_called()

    def test_stale_fingerprint_and_mismatched_runtime_version_block(self):
        self.record()
        self.fingerprint.return_value = {
            **self.evidence,
            "artifacts": {"public.js": "public-changed-hash"},
        }
        with self.assertRaisesRegex(ValueError, "stale"):
            verification.require_verified_runtime(self.root, self.VERSION)
        with self.assertRaises(ValueError):
            verification.require_verified_runtime(self.root, "different-public-version")

    def test_malformed_receipt_fields_and_duplicate_json_keys_block(self):
        self.record()
        original = json.loads(self.receipt.read_text())
        changes = (
            {"version": True},
            {"version": 2},
            {"root": str(self.root.parent)},
            {"runtime_version": "wrong"},
            {"verified_at": "not-a-date"},
            {"verified_at": "2026-01-01T00:00:00"},
            {"checks": []},
            {"checks": ["public.repeat", "public.repeat"]},
            {"checks": "public.one"},
            {"checks": [None]},
            {"fingerprint": {}},
            {"fingerprint": None},
            {"unknown": True},
        )
        for change in changes:
            with self.subTest(change=change):
                self.rewrite({**original, **change})
                with self.assertRaises(ValueError):
                    verification.require_verified_runtime(self.root, self.VERSION)
        self.receipt.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            verification.require_verified_runtime(self.root, self.VERSION)

    def test_public_permissions_symlink_and_hardlink_receipts_are_rejected(self):
        self.record()
        self.receipt.chmod(0o644)
        with self.assertRaises(ValueError):
            verification.require_verified_runtime(self.root, self.VERSION)
        self.receipt.chmod(0o600)
        link = self.root / "public-receipt-link"
        os.link(self.receipt, link)
        for operation in (
            lambda: verification.require_verified_runtime(self.root, self.VERSION),
            self.record,
        ):
            with self.assertRaises(ValueError):
                operation()
        link.unlink()
        self.receipt.rename(link)
        self.receipt.symlink_to(link)
        for operation in (
            lambda: verification.require_verified_runtime(self.root, self.VERSION),
            self.record,
        ):
            with self.assertRaises(ValueError):
                operation()

    def test_state_alias_and_quarantine_block_before_fingerprint(self):
        alias = self.root / "public-other-state"
        alias.mkdir(mode=0o700)
        self.receipt.parent.symlink_to(alias, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.record()
        self.receipt.parent.unlink()
        self.receipt.parent.mkdir(mode=0o700)
        (self.receipt.parent / "cancellation-unconfirmed.json").write_text(
            "Public synthetic quarantine marker\n"
        )
        for operation in (
            lambda: verification.require_verified_runtime(self.root, self.VERSION),
            self.record,
        ):
            with self.assertRaisesRegex(ValueError, "unconfirmed process cancellation"):
                operation()
        self.fingerprint.assert_not_called()

    def test_explicit_probe_revocation_is_idempotent_and_never_loads_receipt_contents(
        self,
    ):
        verification.invalidate_verified_runtime(self.root)
        self.record()
        self.fingerprint.reset_mock()
        with mock.patch.object(
            verification,
            "_bytes",
            side_effect=AssertionError("Revocation must not read contents"),
        ):
            verification.invalidate_verified_runtime(self.root)
            verification.invalidate_verified_runtime(self.root)
        self.assertFalse(self.receipt.exists())
        self.fingerprint.assert_not_called()
        with self.assertRaises(ValueError):
            verification.require_verified_runtime(self.root, self.VERSION)

    def test_revocation_preserves_quarantine_and_other_runtime_state(self):
        self.record()
        gate = self.receipt.parent / "cancellation-unconfirmed.json"
        gate.write_text("Public synthetic quarantine marker\n")
        other = self.receipt.parent / "public-state.fixture"
        other.write_text("Public unrelated state\n")
        verification.invalidate_verified_runtime(self.root)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(gate.read_text(), "Public synthetic quarantine marker\n")
        self.assertEqual(other.read_text(), "Public unrelated state\n")

    def test_revocation_refuses_receipt_aliases_hardlinks_and_directory_aliases(self):
        self.record()
        outside = self.root / "public-unrelated-receipt"
        os.link(self.receipt, outside)
        before = outside.read_bytes()
        with self.assertRaisesRegex(ValueError, "aliased or hardlinked"):
            verification.invalidate_verified_runtime(self.root)
        self.assertEqual(outside.read_bytes(), before)
        self.assertTrue(self.receipt.exists())
        outside.unlink()
        self.receipt.rename(outside)
        self.receipt.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "aliased or hardlinked"):
            verification.invalidate_verified_runtime(self.root)
        self.assertTrue(self.receipt.is_symlink())
        self.assertEqual(outside.read_bytes(), before)
        self.receipt.unlink()
        state = self.receipt.parent
        redirected = self.root / "public-other-runtime-state"
        state.rename(redirected)
        state.symlink_to(redirected, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "safely revoked"):
            verification.invalidate_verified_runtime(self.root)
        self.assertTrue(state.is_symlink())

    def test_failed_atomic_refresh_preserves_previous_receipt_and_removes_staging(self):
        self.record()
        before = self.receipt.read_bytes()
        with mock.patch.object(
            verification.os,
            "replace",
            side_effect=OSError("public failed atomic replacement"),
        ):
            with self.assertRaises(ValueError):
                self.record()
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(list(self.receipt.parent.iterdir()), [self.receipt])

    def test_runtime_tree_hash_captures_public_content_modes_and_internal_links(self):
        tree, _package, script, _executable = self.installation()
        original = verification._runtime_tree(self.root)
        script.write_text("// Public changed runtime artifact\n")
        changed = verification._runtime_tree(self.root)
        self.assertNotEqual(original["sha256"], changed["sha256"])
        script.chmod(0o644)
        self.assertNotEqual(
            changed["sha256"], verification._runtime_tree(self.root)["sha256"]
        )
        (tree / "node_modules/.bin/srt").unlink()
        self.assertNotEqual(
            original["entries"], verification._runtime_tree(self.root)["entries"]
        )

    def test_runtime_tree_external_alias_nonregular_object_and_bounds_fail_closed(self):
        tree, _package, _script, _executable = self.installation()
        outside = self.root / "public-external.js"
        outside.write_text("Public external fixture\n")
        link = tree / "external-link.js"
        link.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "within the public installation"):
            verification._runtime_tree(self.root)
        link.unlink()
        fifo = tree / "public-fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "nonregular"):
            verification._runtime_tree(self.root)
        fifo.unlink()
        with mock.patch.object(verification, "MAX_TREE_ENTRIES", 1):
            with self.assertRaisesRegex(ValueError, "entry limit"):
                verification._runtime_tree(self.root)
        with mock.patch.object(verification, "MAX_TREE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "content limit"):
                verification._runtime_tree(self.root)
        with mock.patch.object(verification, "MAX_ARTIFACT_BYTES", 1):
            with self.assertRaises(ValueError):
                verification._runtime_tree(self.root)

    def test_fingerprint_tracks_public_sources_policy_python_node_os_and_linux_helpers(
        self,
    ):
        _tree, _package, _script, executable = self.installation()
        public_versions = {
            "path": "/public-helper",
            "sha256": "public-binary-hash",
            "version": "public-version",
        }
        with (
            mock.patch.object(verification, "_find", return_value=executable),
            mock.patch.object(
                verification, "_executable", return_value=public_versions
            ) as versions,
            mock.patch.object(verification.sys, "executable", str(executable)),
            mock.patch.object(verification.platform, "system", return_value="Linux"),
            mock.patch.object(
                verification.platform, "release", return_value="public-os-release"
            ),
            mock.patch.object(
                verification.platform, "machine", return_value="public-architecture"
            ),
            mock.patch.object(
                verification.platform,
                "python_version",
                return_value="public-python-version",
            ),
        ):
            fingerprint = REAL_FINGERPRINT(self.root, self.VERSION)
            self.assertEqual(
                set(fingerprint["linux_helpers"]), {"bwrap", "socat", "rg"}
            )
            self.assertEqual(versions.call_count, 4)
            self.assertEqual(fingerprint["node"], public_versions)
            self.assertEqual(fingerprint["python"]["path"], str(executable))
            self.assertEqual(fingerprint["os"]["release"], "public-os-release")
            self.assertEqual(
                set(fingerprint["sources"]),
                {*verification.PUBLIC_SOURCES, "tools/access.local.json"},
            )
            self.assertIsNone(fingerprint["sources"]["tools/access.local.json"])
            policy = self.root / "tools/access.local.json"
            policy.write_text('{"public_policy_fixture":1}')
            with_policy = REAL_FINGERPRINT(self.root, self.VERSION)
            self.assertIsNotNone(with_policy["sources"]["tools/access.local.json"])
            policy.write_text('{"public_policy_fixture":2}')
            self.assertNotEqual(
                with_policy["sources"]["tools/access.local.json"],
                REAL_FINGERPRINT(self.root, self.VERSION)["sources"][
                    "tools/access.local.json"
                ],
            )
            (self.root / "tools/provider_commands.py").write_text(
                "Public changed source\n"
            )
            self.assertNotEqual(
                fingerprint["sources"]["tools/provider_commands.py"],
                REAL_FINGERPRINT(self.root, self.VERSION)["sources"][
                    "tools/provider_commands.py"
                ],
            )

    def test_dependency_version_command_uses_clean_environment_and_public_binary_hash(
        self,
    ):
        executable = self.root / "public-node"
        executable.write_text("Public executable fixture; never executed\n")
        executable.chmod(0o755)
        with mock.patch.object(
            verification.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "v22.public\n", ""),
        ) as invoke:
            result = verification._executable(executable, ("--version",))
        self.assertEqual(result["version"], "v22.public")
        self.assertEqual(invoke.call_args.args[0], [str(executable), "--version"])
        self.assertEqual(
            invoke.call_args.kwargs["env"],
            {"PATH": verification.RUNTIME_PATH, "LANG": "C", "LC_ALL": "C"},
        )
        self.assertEqual(invoke.call_args.kwargs["cwd"], "/")
        self.assertEqual(invoke.call_args.kwargs["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
