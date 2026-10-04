"""Update gating and interprocess lock tests, using public disposable fixtures."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from runtime_maintenance import runtime_lock  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "sandbox_maintain", TOOLS / "runtime/maintain.py"
)
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vaultlens-maintenance-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "tools/runtime").mkdir(parents=True)
        (self.root / "tools/runtime-state").mkdir(mode=0o700)
        version = maintenance.local_runtime.SRT_VERSION
        (self.root / "tools/runtime/install.sh").write_text(
            f"'@anthropic-ai/sandbox-runtime@{version}'"
        )
        (self.root / "tools/runtime/macos-process-guard.mjs").write_text(
            f"const VERSION = '{version}';"
        )
        self.processes = mock.patch.object(maintenance, "check_process_records")
        self.processes.start()
        self.addCleanup(self.processes.stop)

    def test_agents_share_lock_but_block_maintenance(self):
        with runtime_lock(self.root), runtime_lock(self.root):
            with self.assertRaises(ValueError):
                with runtime_lock(self.root, exclusive=True):
                    self.fail("Maintenance must not overlap active agents")
        with runtime_lock(self.root, exclusive=True):
            with self.assertRaises(ValueError):
                with runtime_lock(self.root):
                    self.fail("An agent must not start during maintenance")
        with runtime_lock(self.root):
            pass

    def test_exclusive_lock_blocks_another_process(self):
        script = (
            "import sys,pathlib; sys.path.insert(0,sys.argv[1]); "
            "from runtime_maintenance import runtime_lock; "
            "lease=runtime_lock(pathlib.Path(sys.argv[2])); lease.__enter__()"
        )
        with runtime_lock(self.root, exclusive=True):
            result = subprocess.run(
                [sys.executable, "-I", "-c", script, str(TOOLS), str(self.root)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("maintenance is active", result.stderr)

    def test_alias_lock_is_rejected_without_touching_target(self):
        target = self.root / "untouched"
        target.write_text("public sentinel")
        (self.root / "tools/runtime-state/maintenance.lock").symlink_to(target)
        with self.assertRaises(OSError):
            with runtime_lock(self.root):
                pass
        self.assertEqual(target.read_text(), "public sentinel")

    def test_hardlinked_lock_is_rejected(self):
        target = self.root / "untouched"
        target.write_text("public sentinel")
        target.chmod(0o600)
        os.link(target, self.root / "tools/runtime-state/maintenance.lock")
        with self.assertRaises(ValueError):
            with runtime_lock(self.root):
                pass

    def test_current_receipt_does_not_install_or_probe(self):
        with (
            mock.patch.object(maintenance, "needs_install", return_value=False),
            mock.patch.object(maintenance, "require_verified_runtime"),
            mock.patch.object(maintenance.subprocess, "run") as run,
        ):
            maintenance.maintain(self.root)
            run.assert_not_called()

    def test_failed_probe_revokes_prior_receipt(self):
        receipt = self.root / "tools/runtime-state/verification.json"
        receipt.write_text(json.dumps({"public": "old receipt"}))
        receipt.chmod(0o600)
        failure = subprocess.CompletedProcess([], 2, '{"failed":true}', "public error")
        with (
            mock.patch.object(maintenance, "needs_install", return_value=False),
            mock.patch.object(
                maintenance, "require_verified_runtime", side_effect=ValueError("stale")
            ),
            mock.patch.object(maintenance.subprocess, "run", return_value=failure),
        ):
            with self.assertRaisesRegex(ValueError, "launches remain blocked"):
                maintenance.maintain(self.root)
        self.assertFalse(receipt.exists())

    def test_mismatched_pins_stop_before_installation(self):
        (self.root / "tools/runtime/install.sh").write_text("unreviewed-version")
        with mock.patch.object(maintenance.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "pins disagree"):
                maintenance.maintain(self.root)
            run.assert_not_called()

    def test_new_registry_release_is_reported_without_updating_pin(self):
        with (
            mock.patch.object(maintenance, "maintain") as maintain,
            mock.patch.object(maintenance, "latest_release", return_value="99.0.0"),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(maintenance.main(["--root", str(self.root)]), 0)
            maintain.assert_called_once_with(self.root, check=False)
            self.assertIn(
                "Compatibility review is required",
                " ".join(str(call) for call in output.call_args_list),
            )


if __name__ == "__main__":
    unittest.main()
