#!/usr/bin/env python3
"""Local runtime contracts tested without native agents, credentials or SRT."""

import contextlib
import dataclasses
import json
import os
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import IO, Unpack
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import local_access as access
import local_runtime as runtime
import process_control
import provider_commands
from process_control import SupervisedOptions, SupervisedProcess

# Tests exercise runtime internals directly; bind each private name once, justified here.
_absent_provider_config_reads = runtime._absent_provider_config_reads  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_auth_directory = runtime._auth_directory  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_disposable_run = runtime._disposable_run  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_execute_prepared = runtime._execute_prepared  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_prepare_auth_store = runtime._prepare_auth_store  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_prepare_workspace = runtime._prepare_workspace  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_private_directory = runtime._private_directory  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_run = runtime._run  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_snapshot = runtime._snapshot  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_transfer_auth = runtime._transfer_auth  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals
_verify_preflight = runtime._verify_preflight  # pyright: ignore[reportPrivateUsage] - unit tests drive runtime internals


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        # These unit fixtures exercise preparation, not operating-system evidence.
        verification = mock.patch.object(runtime, "require_verified_runtime")
        verification.start()
        self.addCleanup(verification.stop)

        def synthetic_launch(
            command: list[str],
            *,
            run: Path,
            interactive: bool = False,
            **options: Unpack[SupervisedOptions],
        ) -> SupervisedProcess:
            # A public unit fixture must never bootstrap a real launchd job.
            if interactive:
                return runtime.subprocess.Popen(
                    command, start_new_session=False, process_group=0, **options
                )
            return runtime.subprocess.Popen(command, start_new_session=True, **options)

        supervisor = mock.patch.object(
            runtime, "launch_supervised", side_effect=synthetic_launch
        )
        supervisor.start()
        self.addCleanup(supervisor.stop)
        temporary = tempfile.TemporaryDirectory(prefix="vaultlens-runtime-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        auth_home = tempfile.TemporaryDirectory(prefix="vaultlens-auth-home-test-")
        self.addCleanup(auth_home.cleanup)
        self.auth_home = Path(auth_home.name).resolve()
        account = mock.patch.object(
            runtime.pwd,
            "getpwuid",
            return_value=mock.Mock(pw_dir=str(self.auth_home)),
        )
        account.start()
        self.addCleanup(account.stop)
        self.run_dir = self.root / "public-run-fixture"
        self.run_dir.mkdir()
        (self.run_dir / "mcp.json").write_text('{"mcpServers":{}}')
        (self.root / "tools").mkdir()
        policy = {
            "version": 1,
            "profiles": {
                "reader": {
                    "read": ["wiki/concepts"],
                    "deny_read": ["wiki/concepts/private"],
                },
                "writer": {
                    "read": ["wiki/concepts"],
                    "write": ["wiki/concepts"],
                    "deny_read": ["wiki/concepts/private"],
                },
                "wiki-write": {
                    "read": ["wiki"],
                    "write": ["wiki"],
                    "deny_read": ["wiki/private"],
                },
                "project-write": {
                    "read": ["wiki/concepts", "projects/{project}"],
                    "write": ["projects/{project}"],
                },
            },
            "defaults": {"search": "reader"},
        }
        (self.root / "tools/access-profiles.json").write_text(json.dumps(policy))
        for relative in (
            "wiki/concepts/public.md",
            "wiki/concepts/private/hidden.md",
            "projects/alpha/project.md",
            "projects/alpha/notes/public.md",
            "projects/beta/notes/sibling.md",
            "tools/public-script.py",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Public synthetic fixture\n")
        self.scope = access.resolve_scope(self.root, "reader", capability="read")
        self.env = {
            "HOME": str(self.run_dir / "home"),
            "VAULTLENS_RUNTIME_MANIFEST": str(self.run_dir / "scope.json"),
        }

    def prepared(self) -> contextlib.nullcontext[tuple[Path, Path, dict[str, str]]]:
        return contextlib.nullcontext(
            (Path("/public-runtime/srt"), self.run_dir, self.env)
        )

    def prepared_any(
        self, *_args: object, **_kwargs: object
    ) -> contextlib.nullcontext[tuple[Path, Path, dict[str, str]]]:
        """Stand-in for ``prepared_run`` that ignores its arguments."""
        return self.prepared()

    def test_private_run_is_removed_after_confirmed_success(self) -> None:
        with _disposable_run() as run:
            (run / "public-protocol.json").write_text('{"public":true}')
            self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        self.assertFalse(run.exists())

    def test_preparation_failure_removes_private_run(self) -> None:
        run = Path("/nonexistent-public-run")
        with self.assertRaisesRegex(ValueError, "public preparation failure"):
            with _disposable_run() as run:
                raise ValueError("public preparation failure")
        self.assertFalse(run.exists())

    def test_private_cwd_preserves_vault_identity_and_required_preflight(self) -> None:
        with (
            mock.patch.object(
                runtime, "prepared_run", return_value=self.prepared()
            ) as prepared,
            mock.patch.object(runtime, "_verify_preflight") as preflight,
            mock.patch.object(runtime, "_execute_prepared", return_value=0) as execute,
        ):
            result = _run(
                ["public-status-command"], scope=self.scope, cli=None, private_cwd=True
            )
        self.assertEqual(result, 0)
        self.assertEqual(prepared.call_args.args, (self.scope, None))
        self.assertEqual(preflight.call_args.args[-1], self.root)
        self.assertEqual(execute.call_args.args[4], self.run_dir / "home")

    def test_private_workspace_exposes_selected_paths_without_host_provider_config(
        self,
    ) -> None:
        selected = self.root / "wiki/concepts/public.md"
        scope = dataclasses.replace(self.scope, read_paths=(selected,))
        ambient = self.root / ".codex/config.toml"
        ambient.parent.mkdir()
        ambient.write_text("PUBLIC_AMBIENT_CONFIG_FIXTURE")
        workspace = _prepare_workspace(scope, self.run_dir)
        self.assertFalse(workspace.is_symlink())
        self.assertEqual((workspace / "wiki/concepts/public.md").resolve(), selected)
        self.assertFalse((workspace / "wiki/concepts/private").exists())
        self.assertFalse((workspace / ".codex").exists())
        self.assertEqual((workspace / "tools").resolve(), self.root / "tools")
        settings = runtime.compile_settings(scope, self.run_dir, None)
        self.assertIn(str(workspace), settings["filesystem"]["denyWrite"])
        self.assertNotIn(str(workspace), settings["filesystem"]["allowWrite"])

    def test_private_project_workspace_preserves_original_write_boundary(self) -> None:
        scope = access.resolve_scope(self.root, "project-write", project="alpha")
        workspace = _prepare_workspace(scope, self.run_dir)
        self.assertEqual(
            (workspace / "projects/alpha").resolve(), self.root / "projects/alpha"
        )
        self.assertFalse((workspace / "projects/beta").exists())
        self.assertEqual(scope.write_paths, (self.root / "projects/alpha",))
        with mock.patch.dict(
            os.environ, {"VAULTLENS_RUNTIME_MANIFEST": str(self.run_dir / "scope.json")}
        ):
            self.assertEqual(runtime.active_working_directory(), workspace)

    def test_native_config_probes_grant_only_missing_exact_filenames(self) -> None:
        path = self.root / "native-policy/requirements.toml"
        with mock.patch.object(
            runtime, "PROVIDER_CONFIG_PROBES", {"codex": (str(path),)}
        ):
            settings = runtime.compile_settings(self.scope, self.run_dir, "codex")
            self.assertEqual(_absent_provider_config_reads("claude"), ())
        self.assertIn(str(path), settings["filesystem"]["allowRead"])
        self.assertNotIn(str(path.parent), settings["filesystem"]["allowRead"])
        self.assertNotIn(
            str(path.parent / "other.toml"), settings["filesystem"]["allowRead"]
        )

    def test_existing_native_config_and_parent_aliases_stop_before_content_read(
        self,
    ) -> None:
        path = self.root / "native-policy/requirements.toml"
        path.parent.mkdir()
        path.write_text("Public administrator fixture\n")
        with mock.patch.object(
            runtime, "PROVIDER_CONFIG_PROBES", {"codex": (str(path),)}
        ):
            with self.assertRaisesRegex(ValueError, "requires review"):
                runtime.compile_settings(self.scope, self.run_dir, "codex")
        path.unlink()
        alias = self.root / "native-policy-alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        with mock.patch.object(
            runtime, "PROVIDER_CONFIG_PROBES", {"codex": (str(alias / path.name),)}
        ):
            with self.assertRaisesRegex(ValueError, "cannot follow aliases"):
                runtime.compile_settings(self.scope, self.run_dir, "codex")

    def test_macos_native_probes_include_exact_system_alias_metadata(self) -> None:
        missing = (
            "/etc/codex/requirements.toml",
            "/private/etc/codex/requirements.toml",
        )
        with (
            mock.patch.object(runtime.sys, "platform", "darwin"),
            mock.patch.object(
                runtime, "_absent_provider_config_reads", return_value=missing
            ),
        ):
            env = runtime.clean_environment(self.run_dir, self.root, "codex")
        self.assertEqual(
            json.loads(env["VAULTLENS_PROVIDER_METADATA"]),
            ["/etc/codex", "/private/etc/codex", "/etc"],
        )

    def test_macos_codex_uses_public_pem_roots_without_inherited_ca_settings(
        self,
    ) -> None:
        with (
            mock.patch.object(runtime.sys, "platform", "darwin"),
            mock.patch.dict(
                os.environ,
                {
                    "SSL_CERT_FILE": "/public-unapproved/private-roots.pem",
                    "CODEX_CA_CERTIFICATE": "/public-unapproved/codex-roots.pem",
                    "SSL_CERT_DIR": "/public-unapproved/certificates",
                },
            ),
        ):
            env = runtime.clean_environment(self.run_dir, self.root, "codex")
            self.assertEqual(env["SSL_CERT_FILE"], "/private/etc/ssl/cert.pem")
            self.assertNotIn("SSL_CERT_DIR", env)
            self.assertNotIn("CODEX_CA_CERTIFICATE", env)
            settings = runtime.compile_settings(self.scope, self.run_dir, "codex")
            self.assertIn("/private/etc/ssl", settings["filesystem"]["allowRead"])
            self.assertNotIn("/Library/Keychains", settings["filesystem"]["allowRead"])
            self.assertNotIn("enableWeakerNetworkIsolation", settings["network"])

    def test_missing_runtime_never_starts_child_or_gathers_notes(self) -> None:
        with (
            mock.patch.object(
                runtime, "runtime_executable", side_effect=ValueError("runtime missing")
            ),
            mock.patch.object(runtime, "clean_environment") as environment,
            mock.patch.object(runtime, "_snapshot") as snapshot,
            mock.patch.object(access.RunScope, "document_paths") as documents,
            mock.patch.object(runtime.subprocess, "Popen") as child,
        ):
            with self.assertRaisesRegex(ValueError, "runtime missing"):
                _run(["public-workload"], scope=self.scope, cli=None)
            child.assert_not_called()
            documents.assert_not_called()
            snapshot.assert_not_called()
            environment.assert_not_called()

    def test_missing_verification_blocks_before_auth_snapshot_or_private_environment(
        self,
    ) -> None:
        with (
            mock.patch.object(
                runtime, "runtime_executable", return_value=Path("/public-runtime/srt")
            ),
            mock.patch.object(
                runtime,
                "require_verified_runtime",
                side_effect=ValueError("Verified host receipt missing"),
            ),
            mock.patch.object(runtime, "clean_environment") as environment,
            mock.patch.object(runtime, "_snapshot") as snapshot,
            mock.patch.object(runtime, "_transfer_auth") as authentication,
            mock.patch.object(runtime.subprocess, "Popen") as execute,
        ):
            with self.assertRaisesRegex(ValueError, "receipt missing"):
                _run(["public-workload"], scope=self.scope, cli="codex")
        environment.assert_not_called()
        snapshot.assert_not_called()
        authentication.assert_not_called()
        execute.assert_not_called()

    def test_private_environment_has_no_ambient_provider_or_ssh_access(self) -> None:
        inherited = {
            "HOME": "/public-host-home",
            "PATH": "/public-unapproved-bin",
            "OPENAI_API_KEY": "public-openai-fixture",
            "ANTHROPIC_API_KEY": "public-anthropic-fixture",
            "SSH_AUTH_SOCK": "/public-ssh-agent",
            "CODEX_HOME": "/public-prior-codex",
            "CLAUDE_CONFIG_DIR": "/public-prior-claude",
            "HTTP_PROXY": "https://public-proxy.invalid",
            "ANTHROPIC_BASE_URL": "https://public-model.invalid",
            "OPENAI_BASE_URL": "https://public-model.invalid",
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "TZ": "UTC",
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            for cli, key in (("codex", "CODEX_HOME"), ("claude", "CLAUDE_CONFIG_DIR")):
                with self.subTest(cli=cli):
                    run = self.run_dir / cli
                    run.mkdir()
                    env = runtime.clean_environment(
                        run,
                        self.root,
                        cli,
                        provider_executable=Path("/public-native/bin/agent"),
                    )
                    self.assertEqual(env["HOME"], str(run / "home"))
                    self.assertEqual(env[key], str(run / "provider"))
                    self.assertTrue(Path(env[key]).is_relative_to(run))
                    self.assertEqual(stat.S_IMODE(Path(env[key]).stat().st_mode), 0o700)
                    self.assertEqual(
                        stat.S_IMODE(Path(env["HOME"]).stat().st_mode), 0o700
                    )
                    self.assertNotIn("/public-unapproved-bin", env["PATH"])
                    self.assertNotIn("/public-native/bin", env["PATH"])
                    self.assertEqual(env["VAULTLENS_PROVIDER_CLI"], cli)
                    self.assertEqual(
                        env["VAULTLENS_PROVIDER_EXECUTABLE"], "/public-native/bin/agent"
                    )
                    self.assertNotIn("SSH_AUTH_SOCK", env)
                    for secret in (
                        "OPENAI_API_KEY",
                        "ANTHROPIC_API_KEY",
                        "HTTP_PROXY",
                        "ANTHROPIC_BASE_URL",
                        "OPENAI_BASE_URL",
                    ):
                        self.assertNotIn(secret, env)
                    self.assertNotIn(
                        "CODEX_HOME" if cli == "claude" else "CLAUDE_CONFIG_DIR", env
                    )
                    for xdg in (
                        "XDG_CONFIG_HOME",
                        "XDG_CACHE_HOME",
                        "XDG_DATA_HOME",
                        "XDG_STATE_HOME",
                    ):
                        self.assertTrue(Path(env[xdg]).is_relative_to(run))
                    self.assertEqual(env["TMPDIR"], str(run / "scratch"))
                    self.assertEqual(env["VAULTLENS_SCOPED_MCP"], str(run / "mcp.json"))

    def test_settings_compile_exact_notes_and_private_provider_state(self) -> None:
        scope = access.resolve_scope(self.root, "writer", capability="wiki-write")
        settings = runtime.compile_settings(
            scope, self.run_dir, "codex", executables=(Path(sys.executable),)
        )
        filesystem = settings["filesystem"]
        self.assertIn(str(self.root / "wiki/concepts"), filesystem["allowRead"])
        self.assertNotIn(str(self.root), filesystem["allowRead"])
        self.assertIn(str(self.root / "wiki/concepts/private"), filesystem["denyRead"])
        self.assertIn(str(self.root / "wiki/concepts/private"), filesystem["denyWrite"])
        self.assertIn(str(self.root / "raw/review-inbox"), filesystem["denyRead"])
        self.assertIn(str(self.root / "raw"), filesystem["denyWrite"])
        self.assertIn(str(self.root / "tools/runtime-state"), filesystem["denyRead"])
        self.assertIn(
            str(self.root / "tools/public-script.py"), filesystem["denyWrite"]
        )
        self.assertIn(str(self.run_dir / "provider"), filesystem["allowWrite"])
        self.assertFalse(
            any("runtime-state/providers" in grant for grant in filesystem["allowRead"])
        )
        for path in (
            "scope.json",
            "settings.json",
            "mcp.json",
            "bin",
            "read-canary",
            "write-canary",
        ):
            self.assertIn(str(self.run_dir / path), filesystem["denyWrite"])
        self.assertIn(str(self.run_dir / "read-canary"), filesystem["denyRead"])
        for pattern in access.SECRET_NAMES:
            self.assertIn(
                str(self.root / "wiki" / "**" / pattern), filesystem["denyRead"]
            )
            self.assertIn(
                str(self.root / "wiki/concepts" / "**" / pattern),
                filesystem["denyWrite"],
            )
        for name in access.PROTECTED_NAMES:
            self.assertIn(
                str(self.root / "wiki/concepts" / "**" / name), filesystem["denyWrite"]
            )
        self.assertEqual(
            settings["network"]["allowedDomains"],
            list(runtime.PROVIDER_DOMAINS["codex"]),
        )
        self.assertFalse(settings["network"]["allowAllUnixSockets"])
        self.assertFalse(settings["network"]["allowLocalBinding"])
        self.assertEqual(settings["network"]["allowUnixSockets"], [])
        self.assertFalse(settings["enableWeakerNestedSandbox"])
        self.assertFalse(settings["enableWeakerNetworkIsolation"])
        self.assertFalse(settings["allowAppleEvents"])

    def test_native_discovery_finds_standard_user_install_without_login_shell(
        self,
    ) -> None:
        home = self.root / "public-user-home"
        native = home / ".local/share/claude/versions/public-version"
        native.parent.mkdir(parents=True)
        native.write_text("PUBLIC_NATIVE_FIXTURE")
        native.chmod(0o700)
        link = home / ".local/bin/claude"
        link.parent.mkdir(parents=True)
        link.symlink_to(native)
        with (
            mock.patch.object(runtime.shutil, "which", return_value=None),
            mock.patch.object(runtime.Path, "home", return_value=home),
        ):
            self.assertEqual(runtime.native_executable("claude"), native)

    def test_native_discovery_refuses_retired_launcher_paths(self) -> None:
        legacy = self.root / ".devcontainer/bin/claude"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("PUBLIC_RETIRED_LAUNCHER_FIXTURE")
        legacy.chmod(0o700)
        with mock.patch.object(runtime.shutil, "which", return_value=str(legacy)):
            with self.assertRaisesRegex(ValueError, "retired container launcher"):
                runtime.native_executable("claude")

    def test_headless_report_captures_public_output_after_cleanup(self) -> None:
        import io
        from run_reports import Recorder

        child = mock.Mock(pid=12345)
        child.wait.return_value = 0
        child.stdout = io.BytesIO(b"PUBLIC_REPORT_SENTINEL\n")
        recorder = Recorder(self.scope, "quality", "codex")
        echo = io.StringIO()
        with (
            mock.patch.object(
                runtime.subprocess, "Popen", return_value=child
            ) as execute,
            mock.patch.object(runtime, "terminate_group") as cleanup,
            contextlib.redirect_stdout(echo),
        ):
            result = _execute_prepared(
                ["public-workload"],
                Path("/public-runtime/srt"),
                self.run_dir,
                self.env,
                self.root,
                recorder=recorder,
            )
        self.assertEqual(result, 0)
        self.assertEqual(echo.getvalue(), "PUBLIC_REPORT_SENTINEL\n")
        self.assertEqual(execute.call_args.kwargs["stdout"], subprocess.PIPE)
        cleanup.assert_called_once_with(child, grace=6.0)
        report = self.scope.reports / recorder.filename
        self.assertIn("PUBLIC_REPORT_SENTINEL", report.read_text())
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)

    def test_incomplete_report_pipe_quarantines_instead_of_success(self) -> None:
        from run_reports import ReportCaptureError

        child = mock.Mock(pid=12345)
        child.wait.return_value = 0
        recorder = mock.Mock()
        recorder.finish.side_effect = ReportCaptureError(
            "Public stdout EOF unconfirmed", cleanup_unconfirmed=True
        )
        with (
            mock.patch.object(runtime.subprocess, "Popen", return_value=child),
            mock.patch.object(runtime, "terminate_group"),
        ):
            with self.assertRaisesRegex(runtime.ProcessCleanupError, "EOF unconfirmed"):
                _execute_prepared(
                    ["public-workload"],
                    Path("/public-runtime/srt"),
                    self.run_dir,
                    self.env,
                    self.root,
                    recorder=recorder,
                )

    def test_research_domains_need_explicit_profile_and_no_provider_means_no_model_egress(
        self,
    ) -> None:
        self.assertEqual(
            runtime.compile_settings(self.scope, self.run_dir, None)["network"][
                "allowedDomains"
            ],
            [],
        )
        scope = access.RunScope(
            self.root,
            "approved-research",
            self.scope.read_paths,
            (),
            (),
            ("example.org:443",),
            self.scope.reports,
        )
        settings = runtime.compile_settings(scope, self.run_dir, "claude")
        self.assertEqual(
            settings["network"]["allowedDomains"],
            [*runtime.PROVIDER_DOMAINS["claude"], "example.org:443"],
        )

    def test_authentication_transfer_only_copies_public_whitelisted_fixtures(
        self,
    ) -> None:
        source, destination = (
            self.root / "public-login-source",
            self.root / "public-login-destination",
        )
        source.mkdir()
        destination.mkdir()
        (source / "public-login.fixture").write_text("PUBLIC_LOGIN_SENTINEL")
        (source / "public-session.fixture").write_text("PUBLIC_SESSION_SENTINEL")
        (source / "public-config.fixture").write_text("PUBLIC_CONFIG_SENTINEL")
        with mock.patch.object(
            runtime, "AUTH_FILES", {"codex": ("public-login.fixture",)}
        ):
            _transfer_auth(source, destination, "codex")
        self.assertEqual(
            sorted(path.name for path in destination.iterdir()),
            ["public-login.fixture"],
        )
        self.assertEqual(
            (destination / "public-login.fixture").read_text(), "PUBLIC_LOGIN_SENTINEL"
        )
        self.assertEqual(
            stat.S_IMODE((destination / "public-login.fixture").stat().st_mode), 0o600
        )

    def test_auth_store_is_outside_vault_and_cloud_sync_on_both_platforms(self) -> None:
        vault = (
            self.root / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Brain"
        )
        vault.mkdir(parents=True)
        with mock.patch.dict(
            os.environ,
            {"HOME": str(vault), "XDG_STATE_HOME": str(vault / "ambient-state")},
        ):
            for platform, relative in (
                ("darwin", "Library/Application Support/VaultLens/agent-state"),
                ("linux", ".local/state/vaultlens/agent-state"),
            ):
                with (
                    self.subTest(platform=platform),
                    mock.patch.object(runtime.sys, "platform", platform),
                ):
                    store = runtime.auth_store_path(vault, "codex")
                    self.assertTrue(store.is_relative_to(self.auth_home / relative))
                    self.assertFalse(store.is_relative_to(vault))
                    self.assertNotIn("Mobile Documents", store.parts)
                    self.assertEqual(store.parts[-2:], ("providers", "codex"))
                    self.assertRegex(store.parent.parent.name, r"^[0-9a-f]{64}$")
                    self.assertFalse(store.exists())

    def test_auth_store_separates_vaults_and_providers_with_canonical_identity(
        self,
    ) -> None:
        first = runtime.auth_store_path(self.root, "codex")
        second = runtime.auth_store_path(self.root / "projects/alpha", "codex")
        claude = runtime.auth_store_path(self.root, "claude")
        alias = self.auth_home / "public-vault-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, claude)
        self.assertEqual(first.parent, claude.parent)
        self.assertEqual(runtime.auth_store_path(alias, "codex"), first)
        with self.assertRaisesRegex(ValueError, "supported native CLI"):
            runtime.auth_store_path(self.root, "../claude")
        with self.assertRaisesRegex(ValueError, "outside the vault"):
            runtime.auth_store_path(self.auth_home, "codex")

    def test_auth_store_preparation_keeps_dedicated_directories_private(self) -> None:
        library = self.auth_home / "Library"
        support = library / "Application Support"
        support.mkdir(parents=True)
        library.chmod(0o755)
        support.chmod(0o755)
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            store = _prepare_auth_store(self.root, "codex")
        self.assertEqual(stat.S_IMODE(library.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(support.stat().st_mode), 0o755)
        for directory in (store, *store.parents):
            if directory == support:
                break
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(directory.stat().st_uid, os.getuid())
        self.assertEqual(list(store.iterdir()), [])

    def test_auth_store_preparation_refuses_parent_and_leaf_symlinks(self) -> None:
        for index, alias_kind in enumerate(
            ("general-parent", "dedicated-parent", "leaf")
        ):
            with self.subTest(alias_kind=alias_kind):
                home = self.auth_home / f"public-home-{index}"
                outside = self.root / f"public-unrelated-storage-{index}"
                home.mkdir(mode=0o700)
                outside.mkdir(mode=0o700)
                with (
                    mock.patch.object(runtime, "_operator_home", return_value=home),
                    mock.patch.object(runtime.sys, "platform", "darwin"),
                ):
                    store = runtime.auth_store_path(self.root, "codex")
                    selected = {
                        "general-parent": home / "Library",
                        "dedicated-parent": home
                        / "Library/Application Support/VaultLens",
                        "leaf": store,
                    }[alias_kind]
                    selected.parent.mkdir(parents=True, exist_ok=True)
                    selected.symlink_to(outside, target_is_directory=True)
                    with self.assertRaisesRegex(ValueError, "symbolic links"):
                        _prepare_auth_store(self.root, "codex")
                self.assertEqual(list(outside.iterdir()), [])

    def test_auth_store_preparation_refuses_shared_writable_parent(self) -> None:
        parent = self.auth_home / "Library"
        parent.mkdir(mode=0o700)
        parent.chmod(0o777)
        with mock.patch.object(runtime.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "writable by other accounts"):
                _prepare_auth_store(self.root, "codex")
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o777)
        parent.chmod(0o700)
        self.auth_home.chmod(0o777)
        try:
            with mock.patch.object(runtime.sys, "platform", "darwin"):
                with self.assertRaisesRegex(ValueError, "Operator home.*writable"):
                    _prepare_auth_store(self.root, "codex")
        finally:
            self.auth_home.chmod(0o700)

    def test_prepared_run_never_imports_ambient_or_legacy_login_state(self) -> None:
        host = self.auth_home / ".codex"
        legacy = self.root / "tools/runtime-state/providers/codex"
        for directory in (host, legacy):
            directory.mkdir(parents=True)
            (directory / "public-login.fixture").write_text("PUBLIC_PRIOR_LOGIN")
        with (
            mock.patch.object(
                runtime, "AUTH_FILES", {"codex": ("public-login.fixture",)}
            ),
            mock.patch.object(
                runtime, "runtime_executable", return_value=Path("/public-runtime/srt")
            ),
            mock.patch.object(
                runtime, "native_executable", return_value=Path(sys.executable)
            ),
            mock.patch.object(
                runtime, "_transfer_auth", wraps=_transfer_auth
            ) as transfer,
            mock.patch.dict(os.environ, {"CODEX_HOME": str(host)}),
        ):
            store = runtime.auth_store_path(self.root, "codex")
            with runtime.prepared_run(self.scope, "codex", snapshot=False) as (
                _,
                run,
                env,
            ):
                self.assertEqual(env["CODEX_HOME"], str(run / "provider"))
                self.assertFalse((run / "provider/public-login.fixture").exists())
                self.assertEqual(list(store.iterdir()), [])
            self.assertEqual(
                [(call.args[0], call.args[1]) for call in transfer.call_args_list],
                [(store, run / "provider"), (run / "provider", store)],
            )
        for directory in (host, legacy):
            self.assertEqual(
                (directory / "public-login.fixture").read_text(), "PUBLIC_PRIOR_LOGIN"
            )

    def test_authentication_transfer_rejects_aliases_large_files_and_destination_links(
        self,
    ) -> None:
        source, destination = (
            self.root / "public-source",
            self.root / "public-destination",
        )
        source.mkdir()
        destination.mkdir()
        original = self.root / "public-original.fixture"
        original.write_text("public login fixture")
        selected = source / "public-login.fixture"
        with mock.patch.object(runtime, "AUTH_FILES", {"codex": (selected.name,)}):
            selected.symlink_to(original)
            with self.assertRaisesRegex(ValueError, "regular file"):
                _transfer_auth(source, destination, "codex")
            selected.unlink()
            os.link(original, selected)
            with self.assertRaisesRegex(ValueError, "regular file"):
                _transfer_auth(source, destination, "codex")
            selected.unlink()
            selected.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, "regular file"):
                _transfer_auth(source, destination, "codex")
            selected.write_text("public allowed fixture")
            (destination / selected.name).symlink_to(original)
            with self.assertRaisesRegex(ValueError, "destination.*symbolic link"):
                _transfer_auth(source, destination, "codex")
        self.assertEqual(original.read_text(), "public login fixture")

    def test_runtime_state_directory_cannot_be_aliased_into_unrelated_storage(
        self,
    ) -> None:
        (self.root / "tools/runtime-state").symlink_to(
            self.run_dir, target_is_directory=True
        )
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            _private_directory(
                self.root / "tools/runtime-state/providers/codex", self.root
            )
        self.assertFalse((self.run_dir / "providers").exists())

    def test_authentication_transfer_rejects_links_in_directory_chain(self) -> None:
        outside = self.root / "public-outside"
        outside.mkdir()
        (outside / "public-login.fixture").write_text("PUBLIC_OUTSIDE_SENTINEL")
        safe = self.root / "public-safe"
        safe.mkdir()
        (safe / "public-login.fixture").write_text("PUBLIC_APPROVED_SENTINEL")
        alias = self.root / "public-alias"
        alias.symlink_to(outside, target_is_directory=True)
        with mock.patch.object(
            runtime, "AUTH_FILES", {"codex": ("public-login.fixture",)}
        ):
            for source, destination in ((alias, safe), (safe, alias)):
                with self.subTest(source=source, destination=destination):
                    with self.assertRaisesRegex(
                        ValueError, "directory.*symbolic links"
                    ):
                        _transfer_auth(source, destination, "codex")
        self.assertEqual(
            (safe / "public-login.fixture").read_text(), "PUBLIC_APPROVED_SENTINEL"
        )
        self.assertEqual(
            (outside / "public-login.fixture").read_text(), "PUBLIC_OUTSIDE_SENTINEL"
        )

    def test_retained_auth_directory_refuses_replacement_before_refresh(self) -> None:
        source, destination = (
            self.root / "public-source",
            self.root / "public-destination",
        )
        source.mkdir()
        destination.mkdir()
        (source / "public-login.fixture").write_text("PUBLIC_ORIGINAL_SENTINEL")
        with (
            mock.patch.object(
                runtime, "AUTH_FILES", {"codex": ("public-login.fixture",)}
            ),
            _auth_directory(source) as retained,
        ):
            source.rename(self.root / "public-old-source")
            source.mkdir()
            (source / "public-login.fixture").write_text("PUBLIC_REPLACEMENT_SENTINEL")
            with self.assertRaisesRegex(ValueError, "directory changed"):
                _transfer_auth(source, destination, "codex", source_fd=retained)
        self.assertEqual(list(destination.iterdir()), [])

    def test_prepared_run_auth_roundtrip_excludes_prior_configuration_and_sessions(
        self,
    ) -> None:
        store = runtime.auth_store_path(self.root, "codex")
        store.mkdir(parents=True)
        (store / "public-login.fixture").write_text("public initial login")
        (store / "public-config.fixture").write_text("public prior configuration")
        with (
            mock.patch.object(
                runtime, "AUTH_FILES", {"codex": ("public-login.fixture",)}
            ),
            mock.patch.object(
                runtime, "runtime_executable", return_value=Path("/public-runtime/srt")
            ),
            mock.patch.object(runtime.shutil, "which", return_value=sys.executable),
        ):
            with runtime.prepared_run(self.scope, "codex", snapshot=False) as (
                executable,
                run,
                env,
            ):
                self.assertEqual(executable, Path("/public-runtime/srt"))
                state = Path(env["CODEX_HOME"])
                self.assertEqual(state, run / "provider")
                self.assertEqual(
                    (state / "public-login.fixture").read_text(), "public initial login"
                )
                self.assertFalse((state / "public-config.fixture").exists())
                (state / "public-login.fixture").write_text("public refreshed login")
                (state / "public-history.fixture").write_text("public run history")
                self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
                self.assertEqual(
                    json.loads((run / "scope.json").read_text())["read"],
                    [str(self.root / "wiki/concepts")],
                )
                mcp = json.loads((run / "mcp.json").read_text())["mcpServers"]
                self.assertEqual(set(mcp), {"qmd"})
                self.assertIn(str(run / "scope.json"), mcp["qmd"]["args"])
                self.assertTrue((run / "bin/qmd").is_file())
            self.assertFalse(run.exists())
        self.assertEqual(
            (store / "public-login.fixture").read_text(), "public refreshed login"
        )
        self.assertFalse((store / "public-history.fixture").exists())
        self.assertEqual(
            (store / "public-config.fixture").read_text(), "public prior configuration"
        )

    def test_unfinished_run_discards_disposable_state_and_releases_provider_lock(
        self,
    ) -> None:
        store = runtime.auth_store_path(self.root, "codex")
        store.mkdir(parents=True)
        (store / "public-login.fixture").write_text("public original login")
        with (
            mock.patch.object(
                runtime, "AUTH_FILES", {"codex": ("public-login.fixture",)}
            ),
            mock.patch.object(
                runtime, "runtime_executable", return_value=Path("/public-runtime/srt")
            ),
            mock.patch.object(runtime.shutil, "which", return_value=sys.executable),
        ):
            run = Path("/nonexistent-public-run")
            with self.assertRaisesRegex(RuntimeError, "public cancellation"):
                with runtime.prepared_run(self.scope, "codex", snapshot=False) as (
                    _executable,
                    run,
                    _env,
                ):
                    (run / "provider/public-login.fixture").write_text(
                        "public unreviewed refresh"
                    )
                    raise RuntimeError("public cancellation")
            self.assertFalse(run.exists())
            with runtime.prepared_run(self.scope, "codex", snapshot=False):
                pass
        self.assertEqual(
            (store / "public-login.fixture").read_text(), "public original login"
        )

    def test_writer_lock_serializes_overlapping_scopes_and_releases_after_exit(
        self,
    ) -> None:
        scope = access.resolve_scope(self.root, "writer", capability="wiki-write")
        with mock.patch.object(
            runtime, "runtime_executable", return_value=Path("/public-runtime/srt")
        ):
            with runtime.prepared_run(scope, None, snapshot=False):
                with self.assertRaisesRegex(ValueError, "Another scoped writer"):
                    with runtime.prepared_run(scope, None, snapshot=False):
                        self.fail("Concurrent writer unexpectedly obtained the lock")
            with runtime.prepared_run(scope, None, snapshot=False):
                pass

    def test_writer_snapshot_contains_only_selected_project_and_is_recoverable(
        self,
    ) -> None:
        scope = access.resolve_scope(
            self.root, "project-write", project="alpha", capability="project-write"
        )
        backup = _snapshot(scope, "public-snapshot")
        assert backup is not None  # narrows Optional for the type checker
        original = self.root / "projects/alpha/notes/public.md"
        original.write_text("public changed contents")
        self.assertEqual(
            (backup / "projects/alpha/notes/public.md").read_text(),
            "Public synthetic fixture\n",
        )
        self.assertFalse((backup / "projects/beta").exists())
        self.assertFalse((backup / "wiki").exists())
        self.assertEqual(
            json.loads((backup / "scope.json").read_text())["write"],
            [str(self.root / "projects/alpha")],
        )
        self.assertIsNone(_snapshot(self.scope, "public-reader"))

    def test_ambient_marker_cannot_forge_a_whole_process_boundary(self) -> None:
        (self.run_dir / "scope.json").write_text(json.dumps(self.scope.manifest()))
        (self.run_dir / "read-canary").write_text("public readable canary")
        (self.run_dir / "write-canary").write_text("public untouched canary")
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(access.RunScope, "document_paths") as documents,
        ):
            with self.assertRaisesRegex(ValueError, "read confinement is absent"):
                runtime.verify_active_boundary()
            documents.assert_not_called()
        self.assertEqual(
            (self.run_dir / "write-canary").read_text(), "public untouched canary"
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "No whole-process runtime"):
                runtime.verify_active_boundary()

    def test_boundary_requires_both_read_and_write_denial(self) -> None:
        (self.run_dir / "scope.json").write_text(json.dumps(self.scope.manifest()))
        original_open = Path.open

        def confined_open(
            path: Path,
            mode: str = "r",
            buffering: int = -1,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ) -> IO[str]:
            if path in {self.run_dir / "read-canary", self.run_dir / "write-canary"}:
                raise PermissionError("public simulated OS denial")
            return original_open(path, mode, buffering, encoding, errors, newline)

        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(Path, "open", confined_open),
        ):
            self.assertEqual(runtime.verify_active_boundary(), self.scope)

    def test_invalid_manifest_fails_before_any_note_context(self) -> None:
        (self.run_dir / "scope.json").write_text('{"root":"/public-forged-root"}')
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(ValueError, "Invalid active runtime manifest"):
                runtime.verify_active_boundary()

    def test_runtime_command_preserves_argv_through_the_runtime_quoting_layer(
        self,
    ) -> None:
        arguments = [
            "/public executable path/agent",
            "--prompt",
            "Literal 'quotes', $variables and $(substitution)",
            "first line\nsecond line",
            "",
        ]
        wrapped = runtime.runtime_command(
            Path("/public runtime path/srt"),
            self.run_dir / "settings with spaces.json",
            arguments,
        )
        self.assertEqual(wrapped[4:], arguments)
        self.assertEqual(shlex.split(shlex.join(wrapped[4:])), arguments)
        self.assertEqual(wrapped[2], str(self.run_dir / "settings with spaces.json"))
        invalid_argvs: tuple[object, ...] = (
            [],
            "single shell string",
            [""],
            ["agent", None],
            ["agent", "null\0byte"],
        )
        for invalid in invalid_argvs:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    runtime.runtime_command(
                        Path("/runtime/srt"),
                        self.run_dir / "settings.json",
                        invalid,  # pyright: ignore[reportArgumentType] - deliberately malformed argv
                    )

    def test_preflight_uses_exact_workload_settings_and_reports_failure(self) -> None:
        child = mock.Mock(pid=12345, returncode=0)
        child.communicate.return_value = ("", "")
        with (
            mock.patch.object(
                runtime.subprocess, "Popen", return_value=child
            ) as execute,
            mock.patch.object(runtime, "terminate_group") as cleanup,
        ):
            _verify_preflight(
                Path("/public-runtime/srt"), self.run_dir, self.env, self.root
            )
        argv = execute.call_args.args[0]
        self.assertEqual(
            argv[:4],
            [
                "/public-runtime/srt",
                "--settings",
                str(self.run_dir / "settings.json"),
                "--",
            ],
        )
        self.assertEqual(
            argv[4:],
            [
                str(Path(sys.executable).resolve()),
                str(self.root / "tools/runtime/check_boundary.py"),
            ],
        )
        self.assertEqual(execute.call_args.kwargs["env"], self.env)
        self.assertEqual(execute.call_args.kwargs["cwd"], self.run_dir / "workspace")
        self.assertTrue(execute.call_args.kwargs["start_new_session"])
        child.communicate.assert_called_once_with(timeout=30)
        cleanup.assert_called_once_with(child)
        child.returncode = 1
        child.communicate.return_value = ("", "public confinement failure")
        with (
            mock.patch.object(runtime.subprocess, "Popen", return_value=child),
            mock.patch.object(runtime, "terminate_group"),
        ):
            with self.assertRaisesRegex(
                ValueError, "preflight failed.*public confinement failure"
            ):
                _verify_preflight(
                    Path("/public-runtime/srt"), self.run_dir, self.env, self.root
                )

    def test_preflight_timeout_cleans_the_entire_group_and_restores_handlers(
        self,
    ) -> None:
        child = mock.Mock(pid=12345)
        child.communicate.side_effect = subprocess.TimeoutExpired(
            "public preflight", 30
        )
        previous = {
            sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        with (
            mock.patch.object(runtime.subprocess, "Popen", return_value=child),
            mock.patch.object(runtime, "terminate_group") as cleanup,
        ):
            with self.assertRaisesRegex(ValueError, "preflight.*timed out"):
                _verify_preflight(
                    Path("/public-runtime/srt"), self.run_dir, self.env, self.root
                )
        cleanup.assert_called_once_with(child)
        self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)

    def test_preflight_precedes_workload_and_failed_preflight_never_starts_child(
        self,
    ) -> None:
        events: list[str] = []
        child = mock.Mock(pid=12345)
        child.wait.return_value = 23
        child.poll.return_value = 23

        def record_preflight(*_args: object) -> None:
            events.append("preflight")

        def start(*_args: object, **_kwargs: object) -> mock.Mock:
            events.append("workload")
            return child

        with (
            mock.patch.object(runtime, "prepared_run", side_effect=self.prepared_any),
            mock.patch.object(
                runtime,
                "_verify_preflight",
                side_effect=record_preflight,
            ),
            mock.patch.object(
                runtime.subprocess, "Popen", side_effect=start
            ) as execute,
            mock.patch.object(runtime.signal, "signal"),
            mock.patch.object(runtime, "terminate_group") as cleanup,
        ):
            self.assertEqual(
                _run(
                    ["public-workload", "argument with spaces"],
                    scope=self.scope,
                    cli=None,
                ),
                23,
            )
        self.assertEqual(events, ["preflight", "workload"])
        cleanup.assert_called_once_with(child, grace=6.0)
        argv = execute.call_args.args[0]
        self.assertEqual(
            argv[:4],
            [
                "/public-runtime/srt",
                "--settings",
                str(self.run_dir / "settings.json"),
                "--",
            ],
        )
        self.assertEqual(argv[4:], ["public-workload", "argument with spaces"])
        self.assertTrue(execute.call_args.kwargs["start_new_session"])
        with (
            mock.patch.object(runtime, "prepared_run", side_effect=self.prepared_any),
            mock.patch.object(
                runtime,
                "_verify_preflight",
                side_effect=ValueError("public rejected preflight"),
            ),
            mock.patch.object(runtime.subprocess, "Popen") as execute,
        ):
            with self.assertRaisesRegex(ValueError, "public rejected preflight"):
                _run(["public-workload"], scope=self.scope, cli=None)
            execute.assert_not_called()

    def test_cancellation_terminates_process_group_and_restores_signal_handlers(
        self,
    ) -> None:
        child = mock.Mock(pid=12345)
        child.wait.side_effect = KeyboardInterrupt()
        child.poll.return_value = None
        handlers: dict[signal.Signals, object] = {}
        previous: dict[signal.Signals, object] = {
            signal.SIGTERM: object(),
            signal.SIGINT: object(),
        }

        def install(signum: signal.Signals, handler: object) -> object:
            handlers[signum] = handler
            return previous[signum]

        with (
            mock.patch.object(runtime, "prepared_run", side_effect=self.prepared_any),
            mock.patch.object(runtime, "_verify_preflight"),
            mock.patch.object(runtime.subprocess, "Popen", return_value=child),
            mock.patch.object(runtime.signal, "signal", side_effect=install),
            mock.patch.object(runtime, "terminate_group") as cleanup,
        ):
            with self.assertRaisesRegex(
                runtime.ProcessCleanupError, "Outer runtime interrupted"
            ):
                _run(["public-workload"], scope=self.scope, cli=None)
        cleanup.assert_called_once_with(child, grace=6.0)
        self.assertEqual(handlers, previous)

    def test_group_cleanup_terminates_descendants_after_leader_exits(self) -> None:
        child = mock.Mock(pid=12345)
        child.poll.return_value = 0
        with (
            mock.patch.object(process_control.os, "killpg") as terminate,
            mock.patch.object(process_control.time, "sleep") as pause,
        ):
            process_control.terminate_group(child, grace=0.01)
        self.assertEqual(
            terminate.call_args_list,
            [mock.call(12345, signal.SIGTERM), mock.call(12345, signal.SIGKILL)],
        )
        pause.assert_called_once_with(0.01)
        child.wait.assert_called_once()

    def test_unconfirmed_live_group_cleanup_raises_and_quarantines_vault(self) -> None:
        child = mock.Mock(pid=12345)
        with (
            mock.patch.object(
                process_control.os,
                "killpg",
                side_effect=PermissionError("public denied signal"),
            ),
            mock.patch.object(
                process_control.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "12345 S\n", ""),
            ),
        ):
            with self.assertRaises(runtime.ProcessCleanupError):
                process_control.signal_group(child, signal.SIGTERM)
        run = Path("/nonexistent-public-run")
        with mock.patch.object(
            runtime, "runtime_executable", return_value=Path("/public-runtime/srt")
        ):
            with self.assertRaises(runtime.ProcessCleanupError):
                with runtime.prepared_run(self.scope, None, snapshot=False) as (
                    _executable,
                    run,
                    _env,
                ):
                    raise runtime.ProcessCleanupError(
                        "public cancellation incomplete", group_id=12345
                    )
            self.assertTrue(run.exists())
            self.addCleanup(shutil.rmtree, run)
            self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
            gate = self.root / "tools/runtime-state/cancellation-unconfirmed.json"
            self.assertEqual(json.loads(gate.read_text())["group_id"], 12345)
            self.assertEqual(json.loads(gate.read_text())["run_directory"], str(run))
            self.assertTrue((run / "scope.json").is_file())
            self.assertEqual(stat.S_IMODE(gate.stat().st_mode), 0o600)
            with self.assertRaisesRegex(ValueError, "Previous tool cancellation"):
                with runtime.prepared_run(self.scope, None, snapshot=False):
                    self.fail("Unconfirmed cleanup did not quarantine the vault")

    def test_inner_cancellation_exit_preserves_reported_process_group(self) -> None:
        evidence = self.run_dir / "scratch/inner-cancellation.json"
        evidence.parent.mkdir()
        evidence.write_text('{"group_id":12345}')
        child = mock.Mock(pid=23456)
        child.wait.return_value = 125
        with (
            mock.patch.object(runtime.subprocess, "Popen", return_value=child),
            mock.patch.object(runtime.signal, "signal"),
            mock.patch.object(runtime, "terminate_group") as cleanup,
        ):
            with self.assertRaises(runtime.ProcessCleanupError) as failure:
                _execute_prepared(
                    ["public-workload"],
                    Path("/public-runtime/srt"),
                    self.run_dir,
                    self.env,
                    self.root,
                )
        self.assertEqual(failure.exception.group_id, 12345)
        cleanup.assert_called_once_with(child, grace=6.0)

    def test_interactive_provider_uses_model_effort_project_cwd_and_prompt_delimiter(
        self,
    ) -> None:
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                child = mock.Mock(pid=12345)
                child.wait.return_value = 31
                with (
                    mock.patch.object(
                        runtime,
                        "native_executable",
                        return_value=Path("/public-native/agent"),
                    ),
                    mock.patch.object(
                        runtime,
                        "prepared_run",
                        side_effect=self.prepared_any,
                    ),
                    mock.patch.object(runtime, "_verify_preflight") as preflight,
                    mock.patch.object(
                        provider_commands,
                        "build_provider_command",
                        wraps=provider_commands.build_provider_command,
                    ) as build,
                    mock.patch.object(
                        runtime.subprocess, "Popen", return_value=child
                    ) as execute,
                    mock.patch.object(runtime.signal, "signal"),
                    mock.patch.object(runtime.sys.stdin, "isatty", return_value=False),
                    mock.patch.object(runtime, "terminate_group") as cleanup,
                ):
                    result = runtime.launch_interactive(
                        self.root,
                        provider,
                        [
                            "--model",
                            "future/provider/model",
                            "--effort",
                            "provider-flex",
                            "Review",
                            "selected",
                            "notes",
                        ],
                        project="alpha",
                    )
                self.assertEqual(result, 31)
                cleanup.assert_called_once_with(child, grace=6.0)
                request = build.call_args.args[1]
                self.assertEqual(request.model, "future/provider/model")
                self.assertEqual(request.effort, "provider-flex")
                self.assertEqual(request.cwd, self.run_dir / "workspace")
                self.assertIn("Work inside projects/alpha", request.role_prompt)
                self.assertEqual(request.task_prompt, "Review selected notes")
                self.assertEqual(
                    request.writable_roots, (self.root / "projects/alpha",)
                )
                self.assertTrue(request.interactive)
                self.assertFalse(request.web_search)
                self.assertEqual(
                    execute.call_args.kwargs["cwd"], self.run_dir / "workspace"
                )
                native = execute.call_args.args[0][4:]
                self.assertIn("future/provider/model", native)
                self.assertTrue(any("provider-flex" in item for item in native))
                self.assertIn("--", native)
                self.assertIn("Review selected notes", native[-1])
                preflight.assert_called_once()

    def test_interactive_policy_escape_flags_fail_before_preparation(self) -> None:
        for flag in (
            "--yolo",
            "--dangerously-bypass-approvals-and-sandbox",
            "--config=x",
            "-c",
            "--settings",
            "--add-dir",
            "--mcp-config",
            "--sandbox",
            "--resume",
            "--plugin-dir",
        ):
            with self.subTest(flag=flag):
                with (
                    mock.patch.object(
                        runtime.shutil, "which", return_value="/public-native/agent"
                    ),
                    mock.patch.object(runtime, "prepared_run") as prepare,
                    mock.patch.object(runtime.subprocess, "Popen") as execute,
                ):
                    with self.assertRaises(ValueError):
                        runtime.launch_interactive(self.root, "codex", [flag])
                    prepare.assert_not_called()
                    execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
