"""Public synthetic contracts for macOS lifetime ownership; no native jobs."""

import errno
import json
import os
import fcntl
import pty
import shutil
import signal
import subprocess
import struct
import sys
import tempfile
import termios
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import macos_processes as supervision
import process_control


def token(pid, session=900, version=1):
    return (501, 501, 20, 501, 20, pid, session, version)


class AuditKernelTests(unittest.TestCase):
    def setUp(self):
        # Deliberately bypass ctypes initialization: these are fake public IDs.
        self.kernel = object.__new__(supervision.AuditKernel)
        self.kernel.foreign_lifetimes = {}

    def test_exact_session_membership_includes_detached_unrelated_parent(self):
        with (
            mock.patch.object(self.kernel, "user_pids", return_value=[101, 102, 103]),
            mock.patch.object(
                self.kernel,
                "token",
                side_effect=[token(101), token(102, session=901), token(103)],
            ),
        ):
            self.assertEqual(self.kernel.members(900), [token(101), token(103)])

    def test_preexisting_inaccessible_exact_lifetime_is_foreign(self):
        self.kernel.use_foreign_lifetimes([[102, 1000]])
        with (
            mock.patch.object(self.kernel, "user_pids", return_value=[101, 102]),
            mock.patch.object(
                self.kernel,
                "token",
                side_effect=[
                    token(101),
                    supervision.AuditIdentityUnavailable("private"),
                ],
            ),
            mock.patch.object(self.kernel, "lifetime", return_value=1000),
        ):
            self.assertEqual(self.kernel.members(900), [token(101)])

    def test_pid_reuse_never_excludes_an_inaccessible_new_task(self):
        self.kernel.use_foreign_lifetimes([[102, 1000]])
        with (
            mock.patch.object(self.kernel, "user_pids", return_value=[102]),
            mock.patch.object(
                self.kernel,
                "token",
                side_effect=supervision.AuditIdentityUnavailable("private"),
            ),
            mock.patch.object(self.kernel, "lifetime", return_value=1001),
        ):
            with self.assertRaises(process_control.ProcessCleanupError):
                self.kernel.members(900)

    def test_new_inaccessible_task_cannot_be_ignored(self):
        with (
            mock.patch.object(self.kernel, "user_pids", return_value=[103]),
            mock.patch.object(
                self.kernel,
                "token",
                side_effect=supervision.AuditIdentityUnavailable("private"),
            ),
            mock.patch.object(self.kernel, "lifetime", return_value=1001),
        ):
            with self.assertRaises(process_control.ProcessCleanupError):
                self.kernel.members(900)

    def test_kernel_inconsistency_is_never_an_exclusion(self):
        self.kernel.use_foreign_lifetimes([[102, 1000]])
        with (
            mock.patch.object(self.kernel, "user_pids", return_value=[102]),
            mock.patch.object(
                self.kernel,
                "token",
                side_effect=process_control.ProcessCleanupError("invalid"),
            ),
            mock.patch.object(self.kernel, "lifetime") as lifetime,
        ):
            with self.assertRaises(process_control.ProcessCleanupError):
                self.kernel.members(900)
            lifetime.assert_not_called()

    def test_gone_lifetime_is_not_an_unknown_live_task(self):
        with (
            mock.patch.object(self.kernel, "user_pids", return_value=[102]),
            mock.patch.object(
                self.kernel,
                "token",
                side_effect=supervision.AuditIdentityUnavailable("gone"),
            ),
            mock.patch.object(self.kernel, "lifetime", return_value=None),
        ):
            self.assertEqual(self.kernel.members(900), [])

    def test_capture_retains_only_numeric_lifetimes(self):
        with (
            mock.patch.object(self.kernel, "user_pids", return_value=[101, 102]),
            mock.patch.object(self.kernel, "lifetime", side_effect=[1000, None]),
        ):
            self.assertEqual(self.kernel.capture_foreign_lifetimes(), [[101, 1000]])
            self.assertEqual(self.kernel.foreign_lifetimes, {101: 1000})

    def test_invalid_foreign_identity_and_duplicates_refuse(self):
        for values in ([[102, 1], [102, 1]], None, [[1, 2]], [[102, True]]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.kernel.use_foreign_lifetimes(values)

    def test_signal_carries_exact_kernel_token_and_reuse_is_esrch(self):
        observed = []

        def invoke(pointer, signum):
            import ctypes

            observed.append(
                (
                    tuple(
                        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint * 8)).contents
                    ),
                    signum,
                )
            )
            return errno.ESRCH

        self.kernel.signal_call = invoke
        self.assertFalse(self.kernel.signal(token(101, version=7), signal.SIGKILL))
        self.assertEqual(observed, [(token(101, version=7), signal.SIGKILL)])

    def test_signal_permission_failure_is_unconfirmed(self):
        self.kernel.signal_call = mock.Mock(return_value=errno.EPERM)
        with self.assertRaises(process_control.ProcessCleanupError):
            self.kernel.signal(token(101), signal.SIGKILL)


class FreezeTests(unittest.TestCase):
    def test_guardian_is_frozen_before_members_and_double_fork_is_killed(self):
        guardian = token(101)
        orphan = token(104)
        kernel = mock.Mock()
        kernel.members.side_effect = [
            [guardian, orphan],
            [guardian, orphan],
            [guardian],
        ]
        kernel.state.return_value = 4
        with mock.patch.object(supervision.time, "sleep"):
            supervision._stop_members(kernel, 900, guardian)
        self.assertEqual(
            kernel.method_calls[0], mock.call.signal(guardian, signal.SIGSTOP)
        )
        kernel.signal.assert_any_call(orphan, signal.SIGKILL)
        self.assertNotIn(
            mock.call.signal(guardian, signal.SIGKILL), kernel.signal.call_args_list
        )

    def test_membership_failure_propagates_without_completion(self):
        kernel = mock.Mock()
        kernel.members.side_effect = process_control.ProcessCleanupError(
            "unknown new task"
        )
        with self.assertRaises(process_control.ProcessCleanupError):
            supervision._stop_members(kernel, 900, token(101))


class PrivateStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vaultlens-public-audit-unit-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.path = self.directory / "public-state.json"

    def test_private_document_round_trip_and_atomic_update(self):
        supervision._write_document(self.path, {"version": 1, "public": 1})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        supervision._write_document(
            self.path, {"version": 1, "public": 2}, replace=True
        )
        self.assertEqual(supervision._read_document(self.path)["public"], 2)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_symbolic_alias_and_hard_link_refuse(self):
        supervision._write_document(self.path, {"version": 1})
        alias = self.directory / "alias.json"
        alias.symlink_to(self.path)
        with self.assertRaises(OSError):
            supervision._read_document(alias)
        alias.unlink()
        os.link(self.path, alias)
        with self.assertRaises(ValueError):
            supervision._read_document(self.path)

    def test_changed_permissions_and_schema_refuse(self):
        supervision._write_document(self.path, {"version": 1})
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            supervision._read_document(self.path)
        self.path.chmod(0o600)
        self.path.write_text('{"version":2}')
        with self.assertRaises(ValueError):
            supervision._read_document(self.path)

    def test_lost_owner_blocks_future_runs(self):
        supervision._write_document(self.path, {"version": 1, "owner": token(101)})
        with mock.patch.object(supervision, "AuditKernel") as kernel:
            kernel.return_value.token.return_value = token(101, version=2)
            with self.assertRaisesRegex(ValueError, "supervisor was lost"):
                supervision.verify_process_records(self.directory)

    def test_live_exact_owner_retains_parallel_run_record(self):
        supervision._write_document(self.path, {"version": 1, "owner": token(101)})
        with mock.patch.object(supervision, "AuditKernel") as kernel:
            kernel.return_value.token.return_value = token(101)
            supervision.verify_process_records(self.directory)
        self.assertTrue(self.path.is_file())

    def test_pending_record_blocks_future_runs(self):
        (self.directory / ".pending-public").write_text("public")
        with self.assertRaisesRegex(ValueError, "Unfinished"):
            supervision.verify_process_records(self.directory)

    def test_missing_guard_helper_never_bootstraps(self):
        run = self.directory / "run"
        run.mkdir()
        (run / "scope.json").write_text(json.dumps({"root": str(self.directory)}))
        with (
            mock.patch.object(supervision, "AuditKernel") as kernel,
            mock.patch.object(supervision, "_launchctl") as launch,
        ):
            kernel.return_value.token.return_value = token(101)
            kernel.return_value.capture_foreign_lifetimes.return_value = []
            with self.assertRaises(FileNotFoundError):
                supervision.AuditSessionProcess(
                    ["/public/srt"], run=run, cwd=self.directory, env={"HOME": str(run)}
                )
            launch.assert_not_called()


class DispatcherTests(unittest.TestCase):
    def test_linux_retains_process_group_policy_and_exact_argv(self):
        with (
            mock.patch.object(process_control.sys, "platform", "linux"),
            mock.patch.object(process_control.subprocess, "Popen") as popen,
        ):
            process_control.launch_supervised(
                ["public", "literal argument"], run=Path("/public/run"), cwd="/public"
            )
            popen.assert_called_once_with(
                ["public", "literal argument"], start_new_session=True, cwd="/public"
            )

    def test_macos_uses_audit_owner(self):
        with (
            mock.patch.object(process_control.sys, "platform", "darwin"),
            mock.patch.object(supervision, "AuditSessionProcess") as child,
        ):
            process_control.launch_supervised(
                ["public"], run=Path("/public/run"), interactive=True, cwd="/public"
            )
            child.assert_called_once_with(
                ["public"], run=Path("/public/run"), interactive=True, cwd="/public"
            )

    def test_audit_cleanup_never_falls_back_to_pid_group_signaling(self):
        child = mock.Mock(audit_session_owner=True)
        with mock.patch.object(process_control, "signal_group") as fallback:
            process_control.terminate_group(child, grace=0.1)
            child.terminate_owned.assert_called_once_with(grace=0.1)
            fallback.assert_not_called()


class TerminalTests(unittest.TestCase):
    def public_terminal(self):
        owner = object.__new__(supervision.AuditSessionProcess)
        owner._master, owner._slave = pty.openpty()
        self.addCleanup(owner._close_io)
        owner._dummy = []
        owner._streams = []
        owner._forwarders = []
        return owner

    def test_cleanup_drains_final_output_before_closing_terminal(self):
        owner = self.public_terminal()
        owner.closed = False
        owner.returncode = 0
        owner.pid = 101
        owner.guardian = token(101)
        owner.session = 900
        owner.service = "public-synthetic-service"
        owner.kernel = mock.Mock()
        owner.kernel.token.return_value = owner.guardian
        owner.kernel.members.return_value = [owner.guardian]
        owner._remove_record = mock.Mock()
        owner._terminal_output = mock.Mock()
        os.write(owner._slave, b"public final CLI output\n")
        with (
            mock.patch.object(supervision, "_stop_members"),
            mock.patch.object(
                supervision,
                "_launchctl",
                side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=113)],
            ),
        ):
            owner.terminate_owned(grace=0)
        output = b"".join(
            call.args[0] for call in owner._terminal_output.call_args_list
        )
        self.assertIn(b"public final CLI output", output)
        self.assertTrue(owner.closed)
        self.assertIsNone(owner._master)
        owner._remove_record.assert_called_once()

    def cleanup_owner(self):
        owner = self.public_terminal()
        owner.closed = False
        owner.returncode = 0
        owner.pid = 101
        owner.guardian = token(101)
        owner.session = 900
        owner.service = "public-synthetic-service"
        owner.kernel = mock.Mock()
        owner.kernel.token.return_value = owner.guardian
        owner.kernel.members.return_value = [owner.guardian]
        owner._remove_record = mock.Mock()
        owner._drain_terminal = mock.Mock()
        return owner

    def test_cleanup_waits_for_asynchronous_job_disappearance(self):
        owner = self.cleanup_owner()
        with (
            mock.patch.object(supervision, "_stop_members"),
            mock.patch.object(supervision.time, "sleep"),
            mock.patch.object(
                supervision,
                "_launchctl",
                side_effect=[
                    mock.Mock(returncode=0),
                    mock.Mock(returncode=0),
                    mock.Mock(returncode=113),
                ],
            ) as launchctl,
        ):
            owner.terminate_owned(grace=0)
        self.assertTrue(owner.closed)
        self.assertEqual(launchctl.call_count, 3)
        owner._remove_record.assert_called_once()

    def test_repeated_cleanup_preserves_original_failure_after_anchor_kill(self):
        owner = self.cleanup_owner()
        with (
            mock.patch.object(supervision, "_stop_members"),
            mock.patch.object(supervision.time, "monotonic", side_effect=[0, 3]),
            mock.patch.object(
                supervision, "_launchctl", return_value=mock.Mock(returncode=0)
            ),
        ):
            with self.assertRaisesRegex(
                process_control.ProcessCleanupError, "job cleanup is unconfirmed"
            ) as original:
                owner.terminate_owned(grace=0)
        owner.kernel.token.return_value = None
        with self.assertRaises(process_control.ProcessCleanupError) as repeated:
            owner.terminate_owned(grace=0)
        self.assertIs(repeated.exception, original.exception)
        owner._remove_record.assert_not_called()
        self.assertFalse(owner.closed)

    def test_initial_size_and_resize_forward_to_private_terminal(self):
        owner = self.public_terminal()
        host_master, host_slave = pty.openpty()
        self.addCleanup(os.close, host_master)
        self.addCleanup(os.close, host_slave)
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        stdin.fileno.return_value = host_slave
        with mock.patch.object(supervision.sys, "stdin", stdin):
            for rows, columns in ((24, 100), (36, 140)):
                size = struct.pack("HHHH", rows, columns, 0, 0)
                fcntl.ioctl(host_slave, termios.TIOCSWINSZ, size)
                owner._resize_terminal(signal.SIGWINCH, None)
                self.assertEqual(
                    fcntl.ioctl(owner._slave, termios.TIOCGWINSZ, b"\0" * 8), size
                )

    def test_wait_restores_sigwinch_and_host_terminal(self):
        owner = self.public_terminal()
        owner.interactive = True
        owner.returncode = 0
        owner.poll = mock.Mock(side_effect=[None, 0])
        owner._terminal_tick = mock.Mock()
        owner._resize_terminal = mock.Mock()
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        stdin.fileno.return_value = 102
        with (
            mock.patch.object(supervision.sys, "stdin", stdin),
            mock.patch.object(
                supervision.termios, "tcgetattr", return_value=["public attributes"]
            ),
            mock.patch.object(supervision.termios, "tcsetattr") as restore,
            mock.patch.object(supervision.tty, "setraw"),
            mock.patch.object(
                supervision.signal, "signal", return_value=signal.SIG_DFL
            ) as handlers,
        ):
            self.assertEqual(owner.wait(timeout=1), 0)
        self.assertEqual(
            handlers.call_args_list,
            [
                mock.call(signal.SIGWINCH, owner._resize_terminal),
                mock.call(signal.SIGWINCH, signal.SIG_DFL),
            ],
        )
        restore.assert_called_once_with(102, termios.TCSADRAIN, ["public attributes"])

    def test_resize_setup_failure_restores_host_terminal(self):
        owner = self.public_terminal()
        owner.interactive = True
        owner._resize_terminal = mock.Mock(side_effect=OSError("public resize failure"))
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        stdin.fileno.return_value = 102
        with (
            mock.patch.object(supervision.sys, "stdin", stdin),
            mock.patch.object(
                supervision.termios, "tcgetattr", return_value=["public attributes"]
            ),
            mock.patch.object(supervision.termios, "tcsetattr") as restore,
            mock.patch.object(supervision.tty, "setraw"),
        ):
            with self.assertRaisesRegex(OSError, "resize failure"):
                owner.wait(timeout=1)
        restore.assert_called_once_with(102, termios.TCSADRAIN, ["public attributes"])


class GuardParserTests(unittest.TestCase):
    def test_private_terminal_identity_rejects_regular_files(self):
        master, slave = pty.openpty()
        try:
            identity = supervision._terminal_identity(slave)
            self.assertEqual(len(identity), 3)
            with tempfile.TemporaryFile() as regular:
                with self.assertRaisesRegex(ValueError, "terminal character device"):
                    supervision._terminal_identity(regular.fileno())
        finally:
            os.close(master)
            os.close(slave)

    def test_exact_transport_and_credential_denials(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable for the public transport parser fixture")
        module = Path(supervision.__file__).parent / "runtime/macos-process-guard.mjs"
        script = r"""
import assert from 'node:assert/strict';
const { guardedArguments, DENIALS } = await import(process.argv[1]);
const original = "printf '%s\\n' 'literal ; $() argument'";
const base = ['env','-u','HOME','PUBLIC=1','/usr/bin/sandbox-exec','-p','(version 1)\n(deny default (with message "public"))\n(allow process*)','/bin/sh','-c',original];
const parse = () => [...base];
const quote = () => 'canonical-public-transport';
const result = guardedArguments('canonical-public-transport',{shell:true},parse,quote);
assert.equal(result.at(-1), original);
assert.equal(result.at(-3), '/bin/sh');
assert(result.includes('NODE_OPTIONS'));
assert(result.some(value => value.includes('(deny system-audit)')));
assert(DENIALS.includes('(deny job-creation)'));
assert(DENIALS.includes('(deny ipc-posix-shm)'));
assert(DENIALS.includes('(deny ipc-posix-sem)'));
assert(DENIALS.includes('com.apple.securityd.xpc'));
const metadata = guardedArguments('canonical-public-transport',{shell:true},parse,quote,['/private/etc/codex']);
const profile = metadata.find(value => value.startsWith('(version 1)'));
assert(profile.endsWith('(allow file-read-metadata (literal "/private/etc/codex"))\n'));
assert(!profile.includes('(allow file-read-data'));
assert(!profile.includes('(subpath "/private/etc/codex")'));
assert(metadata.includes('VAULTLENS_PROCESS_GUARD_METADATA'));
const terminal = guardedArguments('canonical-public-transport',{shell:true},parse,quote,[], '/dev/ttys042');
const terminalProfile = terminal.find(value => value.startsWith('(version 1)'));
assert(terminalProfile.includes('(literal "/dev/ttys042")'));
assert(terminalProfile.includes('(vnode-type CHARACTER-DEVICE)'));
assert(!terminalProfile.includes('(allow pseudo-tty)'));
assert(!terminalProfile.includes('(regex #"^/dev/ttys")'));
assert(!result.find(value => value.startsWith('(version 1)')).includes('/dev/ttys042'));
assert(terminal.includes('VAULTLENS_PROCESS_GUARD_PTY'));
for (const invalid of ['', '/dev/tty', '/dev/ttys*', '/dev/pts/42', '/dev/ttys042/../043', 42]) {
  assert.throws(() => guardedArguments('canonical-public-transport',{shell:true},parse,quote,[],invalid));
}
for (const invalid of [null, ['relative'], ['/private/etc/*'], [0], Array(17).fill('/public')]) {
  assert.throws(() => guardedArguments('canonical-public-transport',{shell:true},parse,quote,invalid));
}
assert.throws(() => guardedArguments('other',{shell:true},parse,quote));
assert.throws(() => guardedArguments('canonical-public-transport',{shell:false},parse,quote));
assert.throws(() => guardedArguments('canonical-public-transport',{shell:true},() => [...base,'extra'],quote));
assert.throws(() => guardedArguments('canonical-public-transport',{shell:true},() => base.map(value => value === '/usr/bin/sandbox-exec' ? '/public/unknown' : value),quote));
console.log('public parser contracts passed');
"""
        result = subprocess.run(
            [node, "--input-type=module", "-e", script, module.as_uri()],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "public parser contracts passed")


if __name__ == "__main__":
    unittest.main()
