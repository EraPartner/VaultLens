"""Scheduler preview and exact project recovery regressions; fixtures only."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import json
import importlib.util
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "schedule"))
import dispatch  # noqa: E402
from restore_project import restore_project  # noqa: E402


class SchedulerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 3, 3, tzinfo=timezone.utc)

    def test_invalid_model_policy_preserves_status_maintenance_and_recovery(self):
        import llm_provider

        load_config = llm_provider.load_config
        load_profiles = llm_provider.load_profile_models
        for failure in (
            "environment",
            "configuration",
            "profiles",
            "role",
            "enhancement",
        ):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                schedule = root / "tools" / "schedule"
                schedule.mkdir(parents=True)
                source = Path(dispatch.__file__)
                shutil.copy2(source, schedule / "dispatch.py")
                shutil.copy2(
                    dispatch.ROOT / "tools" / "model-profiles.json",
                    root / "tools" / "model-profiles.json",
                )
                shutil.copytree(
                    dispatch.ROOT / ".agents" / "roles", root / ".agents" / "roles"
                )
                config = root / "tools" / "llm.local.json"
                config.write_text("{" if failure == "configuration" else "{}")
                if failure == "profiles":
                    (root / "tools" / "model-profiles.json").write_text("{} invalid")
                if failure == "role":
                    (root / ".agents" / "roles" / "wiki-search.md").write_text(
                        "missing frontmatter"
                    )
                spec = importlib.util.spec_from_file_location(
                    "schedule_policy_fixture", schedule / "dispatch.py"
                )
                fixture = importlib.util.module_from_spec(spec)
                # dataclasses resolve annotations through the module registry.
                with (
                    patch.dict(sys.modules, {spec.name: fixture}),
                    patch.dict(
                        os.environ,
                        {
                            "VAULTLENS_LLM_CLI": "invalid"
                            if failure == "environment"
                            else "claude",
                            "VAULTLENS_SCHEDULE_ENHANCE": "invalid"
                            if failure == "enhancement"
                            else "0",
                        },
                    ),
                    patch.object(
                        llm_provider,
                        "load_config",
                        side_effect=lambda: load_config(config),
                    ),
                    patch.object(
                        llm_provider,
                        "load_profile_models",
                        side_effect=lambda: load_profiles(
                            root / "tools" / "model-profiles.json"
                        ),
                    ),
                ):
                    spec.loader.exec_module(fixture)
                self.assertTrue(fixture._PROVIDER_ERROR)
                self.assertEqual(fixture.ACCOUNTS, [])
                with self.assertRaisesRegex(ValueError, "LLM configuration invalid"):
                    fixture.build_brain_wiki_args(["search"], "low")
                model_builder = Mock(
                    side_effect=AssertionError("model builder must stay blocked")
                )
                steps = [
                    fixture.Step(
                        "maintenance",
                        "host",
                        "daily",
                        (1, 11),
                        [],
                        lambda: [["index", "--rebuild"]],
                    ),
                    fixture.Step(
                        "model",
                        "llm",
                        "daily",
                        (1, 11),
                        ["online", "runtime"],
                        model_builder,
                    ),
                ]
                ledger = {"jobs": {}, "accounts": {}}
                with (
                    patch.object(fixture, "load_ledger", return_value=ledger),
                    patch.object(fixture, "now_local", return_value=self.now),
                    patch.object(fixture, "build_steps", return_value=steps),
                    patch.object(fixture, "make_logger", return_value=lambda _: None),
                    patch.object(fixture, "acquire_lock", return_value=Mock()),
                    patch.object(fixture.fcntl, "flock"),
                    patch.object(fixture, "save_ledger"),
                    patch.object(fixture, "keepawake_off") as recover,
                    patch.object(fixture, "keepawake_on") as engage,
                    patch.object(fixture, "lid_closed", return_value=True),
                    patch.object(fixture.Gates, "get", return_value=True) as gate,
                    patch.object(fixture, "run_host", return_value=(0, "done")) as host,
                    patch.object(fixture, "run_llm") as model,
                    patch.object(fixture, "prune_reports", return_value=[]),
                    patch.object(fixture, "_prune_snapshots", return_value=0),
                    patch.object(
                        fixture.subprocess, "run", return_value=Mock(stdout="")
                    ),
                    patch("builtins.print") as output,
                ):
                    self.assertEqual(fixture.cmd_status(), 0)
                    self.assertIn(
                        "LLM CONFIGURATION INVALID", str(output.call_args_list)
                    )
                    self.assertEqual(fixture.cmd_run(), 0)
                    self.assertEqual(fixture.acknowledge_cancellation(), 0)
                recover.assert_called_once()
                engage.assert_not_called()
                host.assert_called_once()
                model.assert_not_called()
                model_builder.assert_not_called()
                self.assertNotIn(
                    "online", [call.args[0] for call in gate.call_args_list]
                )
                self.assertNotIn(
                    "runtime", [call.args[0] for call in gate.call_args_list]
                )
                self.assertEqual(ledger["jobs"]["maintenance"]["last_result"], "ok")
                self.assertNotIn("model", ledger["jobs"])
                self.assertIn(
                    "LLM configuration invalid",
                    (fixture.REPORTS_DIR / "schedule-status.md").read_text(),
                )

    def test_preview_has_no_mutating_gates_or_ledger_writes(self):
        ledger = {"jobs": {}, "accounts": {}}
        steps = [dispatch.Step("empty", "llm", "daily", (1, 11), [], lambda: [])]
        with (
            patch.object(dispatch, "load_ledger", return_value=ledger),
            patch.object(dispatch, "now_local", return_value=self.now),
            patch.object(dispatch, "build_steps", return_value=steps),
            patch.object(dispatch, "make_logger") as logger,
            patch.object(dispatch, "acquire_lock") as lock,
            patch.object(dispatch, "save_ledger") as save,
            patch.object(dispatch, "lid_closed", return_value=False),
            patch.object(dispatch.Gates, "_pmset_batt", return_value="AC Power"),
            patch("builtins.print"),
        ):
            self.assertEqual(dispatch.cmd_run(dry_run=True), 0)
        self.assertEqual(ledger, {"jobs": {}, "accounts": {}})
        logger.assert_not_called()
        lock.assert_not_called()
        save.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "wiki" / "reports").mkdir(parents=True)
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "REPORTS_DIR", root / "wiki" / "reports"),
                patch.object(
                    dispatch, "runtime_available", return_value=False
                ) as probe,
                patch.object(dispatch.subprocess, "run") as run,
            ):
                gates = dispatch.Gates(lambda _: None, read_only=True)
                self.assertFalse(gates.get("runtime"))
                self.assertTrue(gates.get("icloud"))
            run.assert_not_called()
            probe.assert_called_once_with(root=root, cli=dispatch.CLI)

    def test_native_runtime_gate_never_starts_or_installs_a_service(self):
        for available in (False, True):
            with (
                self.subTest(available=available),
                patch.object(
                    dispatch, "runtime_available", return_value=available
                ) as probe,
                patch.object(dispatch.subprocess, "run") as start,
            ):
                gates = dispatch.Gates(lambda _: None)
                self.assertEqual(gates.get("runtime"), available)
                self.assertEqual(gates.get("runtime"), available)
            probe.assert_called_once_with(root=dispatch.ROOT, cli=dispatch.CLI)
            start.assert_not_called()

    def test_preprocessing_does_not_mark_pdf_ingested(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / "raw" / "sources"
            extracted = root / "raw" / "sources-text"
            wiki_sources = root / "wiki" / "sources"
            for directory in (sources, extracted, wiki_sources):
                directory.mkdir(parents=True)
            pdf = sources / "Foo Bar.pdf"
            pdf.write_bytes(b"fixture")
            for stem in ("Foo Bar", "foo-bar"):
                (extracted / f"{stem}.md").write_text("preprocessed input")
            with patch.object(dispatch, "ROOT", root):
                self.assertEqual(
                    dispatch._ingest_targets(), [["ingest", "--source", str(pdf)]]
                )
                (wiki_sources / "foo-bar.md").write_text(
                    "Source: [PDF](<../../raw/sources/Foo Bar.pdf>)"
                )
                self.assertEqual(dispatch._ingest_targets(), [])

    def test_scheduled_ingest_never_follows_source_or_inbox_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inbox = root / "raw" / "inbox"
            sources = root / "raw" / "sources"
            review = root / "raw" / "review-inbox"
            for directory in (inbox, sources, review):
                directory.mkdir(parents=True)
            restricted = review / "private.pdf"
            restricted.write_bytes(b"restricted fixture")
            (inbox / "private.md").symlink_to(restricted)
            (sources / "private.pdf").symlink_to(restricted)
            approved = inbox / "approved.md"
            approved.write_text("approved fixture")
            with patch.object(dispatch, "ROOT", root):
                self.assertEqual(
                    dispatch._ingest_targets(), [["ingest", "--source", str(approved)]]
                )
                approved.unlink()
                (inbox / "private.md").unlink()
                inbox.rmdir()
                inbox.symlink_to(review, target_is_directory=True)
                self.assertEqual(dispatch._ingest_targets(), [])

    def test_cited_inbox_inputs_remain_immutable_without_reingestion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inbox = root / "raw" / "inbox"
            sources = root / "wiki" / "sources"
            inbox.mkdir(parents=True)
            sources.mkdir(parents=True)
            files = {
                "Approved Source.pdf": b"PDF fixture",
                "Approved Note.md": b"Markdown fixture",
                "Encoded Source.pdf": b"PDF fixture",
                "Next Source.pdf": b"PDF fixture",
            }
            for name, data in files.items():
                (inbox / name).write_bytes(data)
            (sources / "pdf.md").write_text(
                "Source: [PDF](<../../raw/inbox/Approved Source.pdf>)\n"
            )
            (sources / "note.md").write_text(
                "Source: [[raw/inbox/Approved Note.md|Approved note]]\n"
            )
            (sources / "encoded.md").write_text(
                "Source: [PDF](../../raw/inbox/Encoded%20Source.pdf)\n"
            )
            with patch.object(dispatch, "ROOT", root):
                self.assertEqual(
                    dispatch._ingest_targets(),
                    [["ingest", "--source", str(inbox / "Next Source.pdf")]],
                )
            for name, data in files.items():
                self.assertEqual((inbox / name).read_bytes(), data)

    def test_handoff_routes_only_to_real_opted_in_projects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            enabled = projects / "enabled"
            dormant = projects / "dormant"
            outside = root / "outside"
            for project in (enabled, dormant, outside):
                project.mkdir(parents=True)
                (project / "project.md").write_text("---\nstatus: active\n---\n")
                (project / "AGENDA.md").write_text(
                    "---\nenabled: true\n---\n\n## Inbox\n"
                    if project != dormant
                    else "---\nenabled: false\n---\n\n## Inbox\n"
                )
            (projects / "linked").symlink_to(outside, target_is_directory=True)
            for target in (
                "dormant",
                "linked",
                "../outside",
                str(outside),
                "enabled/..",
                "missing",
            ):
                with self.subTest(target=target):
                    self.assertIsNone(dispatch.resolve_proposal_dest(target, projects))
            handoffs = (
                "handoff:: enabled | Review the result | notes/result.md\n"
                "handoff:: dormant | Edit anyway | notes/result.md\n"
                "handoff:: ../outside | Escape the scope | notes/result.md\n"
                "handoff:: linked | Follow a link | notes/result.md\n"
            )
            with patch.object(dispatch, "notify"):
                self.assertEqual(
                    dispatch.route_handoffs(
                        handoffs,
                        "source",
                        self.now,
                        lambda _: None,
                        projects_dir=projects,
                    ),
                    1,
                )
            self.assertIn(
                "[from:source] Review the result", (enabled / "AGENDA.md").read_text()
            )
            self.assertNotIn("Edit anyway", (dormant / "AGENDA.md").read_text())
            self.assertNotIn("Escape", (outside / "AGENDA.md").read_text())
            (enabled / "AGENDA.md").unlink()
            (enabled / "AGENDA.md").symlink_to(outside / "AGENDA.md")
            self.assertIsNone(dispatch.resolve_proposal_dest("enabled", projects))

    def test_project_selection_checks_links_before_reading_agendas(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            review = root / "raw" / "review-inbox"
            review.mkdir(parents=True)
            (review / "AGENDA.md").write_text("restricted fixture")
            (review / "project.md").write_text("restricted fixture")
            enabled = projects / "enabled"
            linked_agenda = projects / "linked-agenda"
            linked_metadata = projects / "linked-metadata"
            for project in (enabled, linked_agenda, linked_metadata):
                project.mkdir(parents=True)
                (project / "project.md").write_text("---\nstatus: active\n---\n")
                (project / "AGENDA.md").write_text(
                    "---\nenabled: true\n---\n\n## Inbox\n- Review the fixture\n"
                )
            (linked_agenda / "AGENDA.md").unlink()
            (linked_agenda / "AGENDA.md").symlink_to(review / "AGENDA.md")
            (linked_metadata / "project.md").unlink()
            (linked_metadata / "project.md").symlink_to(review / "project.md")
            (projects / "linked").symlink_to(review, target_is_directory=True)
            original_read = Path.read_text

            def allowed_read(path, *args, **kwargs):
                if path.resolve().is_relative_to(review.resolve()):
                    raise AssertionError("Consent queue content must not be opened")
                return original_read(path, *args, **kwargs)

            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "now_local", return_value=self.now),
                patch.object(
                    dispatch.agenda, "is_paused_for_review", return_value=False
                ),
                patch.object(Path, "read_text", allowed_read),
            ):
                self.assertEqual(
                    dispatch._project_runner_targets(),
                    [["project-run", "--project", "enabled"]],
                )

    def test_host_reports_cannot_follow_directory_or_file_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wiki = root / "wiki"
            wiki.mkdir()
            outside = root / "outside"
            outside.mkdir()
            reports = wiki / "reports"
            reports.symlink_to(outside, target_is_directory=True)
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "REPORTS_DIR", reports),
            ):
                with self.assertRaisesRegex(ValueError, "Report destination"):
                    dispatch.write_report("cos-brief", "fixture", self.now)
                self.assertEqual(list(outside.iterdir()), [])
                reports.unlink()
                reports.mkdir()
                target = outside / "keep.md"
                target.write_text("original")
                (reports / "schedule-status.md").symlink_to(target)
                with self.assertRaisesRegex(ValueError, "Report target is a link"):
                    dispatch.write_schedule_status(
                        {"jobs": {}, "accounts": {}}, [], self.now
                    )
                self.assertEqual(target.read_text(), "original")
                with self.assertRaisesRegex(ValueError, "simple job name"):
                    dispatch.write_report("../escaped", "fixture", self.now)

    def test_content_reports_are_excluded_from_all_profiles_and_scoped_search(self):
        from local_access import resolve_scope
        from scoped_search import ScopedSearch

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "tools").mkdir()
            shutil.copy2(
                Path(dispatch.__file__).resolve().parents[1] / "access-profiles.json",
                root / "tools/access-profiles.json",
            )
            (root / "projects/alpha").mkdir(parents=True)
            (root / "projects/alpha/project.md").write_text("# Fixture project\n")
            reports = root / "wiki/reports"
            private = "SCHEDULER_PRIVATE_OUTPUT_SENTINEL_839145"
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "REPORTS_DIR", reports),
                patch.object(dispatch, "_PROVIDER_ERROR", ""),
            ):
                report = dispatch.write_report("cos-brief", private, self.now)
                status = dispatch.write_schedule_status(
                    {"jobs": {}, "accounts": {}}, [], self.now
                )
            self.assertEqual(report.parent, reports / "agents/scheduled")
            self.assertEqual(status, reports / "schedule-status.md")
            self.assertIn(private, report.read_text())
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            profiles = (
                ("selected-read", "read"),
                ("wiki-read", "read"),
                ("source-read", "read"),
                ("cos-read", "read"),
                ("wiki-write", "wiki-write"),
                ("project-write", "project-write"),
            )
            for profile, capability in profiles:
                with self.subTest(profile=profile):
                    scope = resolve_scope(
                        root,
                        profile,
                        project="alpha" if profile == "project-write" else None,
                        capability=capability,
                        read_paths=["wiki/reports/schedule-status.md"]
                        if profile == "selected-read"
                        else (),
                    )
                    self.assertFalse(scope.readable(report))
                    self.assertFalse(scope.writable(report))
                    self.assertNotIn(report, scope.document_paths())
                    self.assertTrue(scope.readable(status))
                    search = ScopedSearch(scope)
                    self.assertEqual(search.search({"query": private})["results"], [])
                    with self.assertRaisesRegex(ValueError, "unavailable"):
                        search.get({"path": str(report)})
                    health = search.get({"path": "wiki/reports/schedule-status.md"})
                    self.assertIn("all scheduled jobs healthy", health["text"])
                    self.assertNotIn(private, health["text"])

    def test_readable_status_never_copies_raw_errors_results_or_backend_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sentinel = "SCHEDULER_PRIVATE_DIAGNOSTIC_SENTINEL_731"
            ledger = {
                "jobs": {
                    "cos-brief": {
                        "last_result": sentinel,
                        "fail_streak": sentinel,
                        "output": sentinel,
                    }
                },
                "accounts": {
                    sentinel: {
                        "limited_until": dispatch.iso(
                            self.now + dispatch.timedelta(hours=1)
                        ),
                        "last_error": sentinel,
                    }
                },
                "cancellation_pending": {"detail": sentinel},
            }
            steps = [
                dispatch.Step("cos-brief", "llm", "daily", (1, 11), [], lambda: [])
            ]
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "REPORTS_DIR", root / "wiki/reports"),
                patch.object(dispatch, "_PROVIDER_ERROR", sentinel),
            ):
                status = dispatch.write_schedule_status(ledger, steps, self.now)
            text = status.read_text()
            self.assertNotIn(sentinel, text)
            self.assertIn("LLM configuration invalid", text)
            self.assertIn("inner termination unconfirmed", text)
            self.assertIn("FAIL (unknown)", text)
            self.assertIn("1 backend(s)", text)
            self.assertEqual(ledger["cancellation_pending"]["detail"], sentinel)

    def test_private_report_directories_reject_links_during_writes_and_retention(self):
        for relative in (
            "wiki",
            "wiki/reports",
            "wiki/reports/agents",
            "wiki/reports/agents/scheduled",
        ):
            with (
                self.subTest(directory=relative),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                outside = root / "outside"
                outside.mkdir()
                sentinel = outside / "scheduled-cos-brief-2026-01-01.md"
                sentinel.write_text("Unrelated fixture\n")
                link = root / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                with (
                    patch.object(dispatch, "ROOT", root),
                    patch.object(dispatch, "REPORTS_DIR", root / "wiki/reports"),
                ):
                    with self.assertRaisesRegex(ValueError, "Report destination"):
                        dispatch.write_report("cos-brief", "Public fixture", self.now)
                    with self.assertRaisesRegex(ValueError, "Report destination"):
                        dispatch.prune_reports(retention=0)
                self.assertEqual(sentinel.read_text(), "Unrelated fixture\n")
                self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_private_report_target_links_and_directory_swaps_cannot_redirect_output(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            reports = root / "wiki/reports"
            scheduled = reports / "agents/scheduled"
            scheduled.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "keep.md"
            sentinel.write_text("Unrelated fixture\n")
            target = scheduled / f"scheduled-cos-brief-{self.now:%Y-%m-%d}.md"
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "REPORTS_DIR", reports),
            ):
                target.symlink_to(sentinel)
                with self.assertRaisesRegex(ValueError, "Report target is a link"):
                    dispatch.write_report("cos-brief", "Public fixture", self.now)
                target.unlink()
                os.link(sentinel, target)
                with self.assertRaisesRegex(ValueError, "without aliases"):
                    dispatch.write_report("cos-brief", "Public fixture", self.now)
                target.unlink()
                original_open = os.open
                renamed = scheduled.with_name("held-scheduled")

                def swapped_open(path, flags, *args, **kwargs):
                    if isinstance(path, str) and path.startswith(".schedule-report-"):
                        scheduled.rename(renamed)
                        scheduled.symlink_to(outside, target_is_directory=True)
                    return original_open(path, flags, *args, **kwargs)

                with patch.object(dispatch.os, "open", side_effect=swapped_open):
                    with self.assertRaisesRegex(ValueError, "Report destination"):
                        dispatch.write_report("cos-brief", "Public fixture", self.now)
                self.assertEqual(list(renamed.iterdir()), [])
                self.assertEqual(sentinel.read_text(), "Unrelated fixture\n")
                self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_retention_prunes_only_real_private_reports_and_keeps_public_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            reports = root / "wiki/reports"
            scheduled = reports / "agents/scheduled"
            scheduled.mkdir(parents=True)
            public = reports / "scheduled-cos-brief-2026-01-01.md"
            public.write_text("Historical fixture, handled by migration\n")
            status = reports / "schedule-status.md"
            status.write_text("Metadata only\n")
            first = scheduled / "scheduled-cos-brief-2026-01-01.md"
            newest = scheduled / "scheduled-cos-brief-2026-01-02.md"
            first.write_text("Old private fixture\n")
            newest.write_text("Current private fixture\n")
            alias = scheduled / "scheduled-contradict-2026-01-01.md"
            alias.symlink_to(public)
            hand_written = scheduled / "hand-written.md"
            hand_written.write_text("Preserved fixture\n")
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "REPORTS_DIR", reports),
            ):
                self.assertEqual(dispatch.prune_reports(), [first.name])
                self.assertEqual(dispatch.prune_reports(retention=0), [])
            self.assertFalse(first.exists())
            self.assertEqual(newest.read_text(), "Current private fixture\n")
            self.assertTrue(alias.is_symlink())
            self.assertEqual(
                public.read_text(), "Historical fixture, handled by migration\n"
            )
            self.assertEqual(status.read_text(), "Metadata only\n")
            self.assertEqual(hand_written.read_text(), "Preserved fixture\n")

    def test_maintenance_operational_failures_remain_due(self):
        findings = json.dumps(
            {
                "error_count": 2,
                "errors": {"broken_links": ["example"]},
                "pages_checked": 1,
            }
        )
        cases = [
            ("lint", "host", ["lint", "--json"], 1, findings, True),
            ("lint", "host", ["lint", "--json"], 1, "Traceback: crashed", False),
            ("lint", "host", ["lint", "--json"], 2, findings, False),
            ("index", "host", ["index", "--rebuild"], 1, "failed", False),
            ("qmd-update", "qmd", ["update"], 1, "failed", False),
            ("qmd-cleanup", "qmd", ["cleanup"], 124, "timeout", False),
            ("qmd-update", "qmd", ["update"], 0, "updated", True),
        ]
        for name, kind, args, rc, output, completed in cases:
            with self.subTest(name=name, rc=rc, output=output):
                step = dispatch.Step(name, kind, "daily", (1, 11), [], lambda: [args])
                ledger = {"jobs": {name: {"fail_streak": 2}}, "accounts": {}}
                runner = "run_host" if kind == "host" else "run_qmd"
                with (
                    patch.object(dispatch, runner, return_value=(rc, output)),
                    patch.object(dispatch, "save_ledger"),
                    patch.object(dispatch, "notify"),
                ):
                    dispatch._run_steps(
                        [step],
                        ledger,
                        dispatch.Gates(lambda _: None),
                        self.now,
                        False,
                        lambda _: None,
                    )
                record = ledger["jobs"][name]
                self.assertEqual("last_ok" in record, completed)
                self.assertEqual(record["fail_streak"], 0 if completed else 3)
                self.assertEqual(
                    dispatch.step_due(step, ledger, self.now), not completed
                )

    def test_missing_maintenance_executable_is_recordable_failure(self):
        for runner in (dispatch.run_host, dispatch.run_qmd):
            with patch.object(
                dispatch.subprocess, "run", side_effect=FileNotFoundError("absent")
            ):
                self.assertEqual(runner(["update"], 1), (127, "absent"))

    def test_dispatcher_does_not_start_or_stop_an_external_runtime(self):
        # An unrelated session can appear at any time; a failed baseline list
        # likewise conveys no ownership. Neither warrants any global listing.
        with (
            patch.object(dispatch, "acquire_lock", return_value=Mock()),
            patch.object(dispatch.fcntl, "flock"),
            patch.object(
                dispatch, "load_ledger", return_value={"jobs": {}, "accounts": {}}
            ),
            patch.object(dispatch, "now_local", return_value=self.now),
            patch.object(dispatch, "build_steps", return_value=[]),
            patch.object(dispatch, "make_logger", return_value=lambda _: None),
            patch.object(dispatch.Gates, "get", return_value=False),
            patch.object(dispatch, "lid_closed", return_value=False),
            patch.object(dispatch, "keepawake_off"),
            patch.object(dispatch, "save_ledger"),
            patch.object(dispatch, "write_schedule_status"),
            patch.object(dispatch, "prune_reports", return_value=[]),
            patch.object(dispatch, "_prune_snapshots", return_value=0),
            patch.object(
                dispatch.subprocess,
                "run",
                side_effect=AssertionError("unexpected runtime operation"),
            ) as run,
        ):
            self.assertEqual(dispatch.cmd_run(), 0)
        run.assert_not_called()

    def test_native_launch_discards_legacy_session_reuse_setting(self):
        with (
            patch.dict(dispatch.os.environ, {"BRAIN_KEEP_WARM": "1"}),
            patch.object(
                dispatch, "_run_agent_process", return_value=(0, "done")
            ) as run,
        ):
            dispatch.exec_brain_wiki(["cos"], "claude", "low", 5)
        self.assertNotIn("BRAIN_KEEP_WARM", run.call_args.args[2])

    def test_claude_limit_defers_following_invocations(self):
        for response in (
            "You've hit your session limit",
            "You've hit your limit · resets 6pm",
            "Monthly spend limit reached",
        ):
            with self.subTest(response=response):
                ledger = {"jobs": {}, "accounts": {}}
                with (
                    patch.object(dispatch, "ACCOUNTS", ["claude"]),
                    patch.object(dispatch, "_PROVIDER_ERROR", ""),
                    patch.object(dispatch, "save_ledger"),
                    patch.object(
                        dispatch, "exec_brain_wiki", return_value=(1, response)
                    ) as launch,
                ):
                    first = dispatch.run_llm(
                        ["cos"], "low", 5, ledger, self.now, lambda _: None
                    )
                    second = dispatch.run_llm(
                        ["search"], "low", 5, ledger, self.now, lambda _: None
                    )
                self.assertEqual(first, ("deferred", "claude", response))
                self.assertEqual(second, ("deferred", "claude", ""))
                launch.assert_called_once()
                self.assertEqual(ledger["accounts"]["claude"]["last_error"], "quota")

    def test_scheduled_launch_is_bound_to_dispatcher_checkout(self):
        with tempfile.TemporaryDirectory(prefix="schedule checkout ") as temporary:
            base = Path(temporary).resolve()
            root = base / "alternate vault"
            root.mkdir()
            fallback = base / "other private vault"
            for checkout in (root, fallback):
                launcher = checkout / "tools" / "agents" / "wiki-agent.py"
                launcher.parent.mkdir(parents=True)
                launcher.write_text(
                    "from pathlib import Path\n"
                    "import os\n"
                    "print(Path(__file__).resolve().parents[2])\n"
                    "print(os.getcwd())\n"
                )
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "LOG_DIR", base / "logs"),
                patch.dict(
                    os.environ,
                    {"BRAIN_HOME": str(fallback), "BRAIN_PROJECT_ROOT": str(fallback)},
                ),
                patch.object(
                    dispatch, "default_access_profile", return_value="cos-read"
                ),
                patch.object(
                    dispatch.subprocess, "Popen", wraps=subprocess.Popen
                ) as popen,
            ):
                rc, output = dispatch.exec_brain_wiki(["cos"], "claude", "low", 5)
                self.assertEqual(rc, 0, output)
                self.assertEqual(output.splitlines(), [str(root), str(root)])
                self.assertEqual(popen.call_args.kwargs["cwd"], str(root))
                self.assertEqual(
                    popen.call_args.args[0][:2],
                    [dispatch.PYTHON, str(root / "tools" / "agents" / "wiki-agent.py")],
                )
                # Missing launchers fail closed, even when another valid vault
                # is present in the caller's original fallback setting.
                (root / "tools" / "agents" / "wiki-agent.py").unlink()
                rc, output = dispatch.exec_brain_wiki(["cos"], "claude", "low", 5)
                self.assertNotEqual(rc, 0)
                self.assertIn(str(root), output)
                self.assertNotIn(str(fallback), output)

    def test_failed_snapshot_blocks_the_project_writer(self):
        step = dispatch.Step(
            "project-runner",
            "llm",
            "daily",
            (1, 11),
            [],
            lambda: [["project-run", "--project", "demo"]],
        )
        ledger = {"jobs": {}, "accounts": {}}
        with (
            patch.object(dispatch, "_snapshot_project", return_value=None),
            patch.object(dispatch, "run_llm") as run,
            patch.object(dispatch, "save_ledger"),
        ):
            dispatch._run_steps(
                [step],
                ledger,
                dispatch.Gates(lambda _: None),
                self.now,
                False,
                lambda _: None,
            )
        run.assert_not_called()
        self.assertEqual(
            ledger["jobs"]["project-runner"]["last_result"], "snapshot-failed"
        )
        self.assertNotIn("last_ok", ledger["jobs"]["project-runner"])

    def test_partial_failed_snapshot_never_becomes_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            snapshots = root / "snapshots"

            def failed_copy(command, **kwargs):
                destination = Path(command[-1])
                destination.mkdir()
                (destination / "partial").write_text("incomplete")
                raise subprocess.CalledProcessError(1, command)

            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "SNAPSHOT_DIR", snapshots),
                patch.object(
                    dispatch.subprocess, "run", side_effect=failed_copy
                ) as copy,
            ):
                self.assertIsNone(
                    dispatch._snapshot_project("demo", self.now, lambda _: None)
                )
            self.assertEqual(copy.call_count, 2)
            self.assertFalse((snapshots / "2026-10-03" / "demo").exists())
            self.assertEqual(list((snapshots / "2026-10-03").iterdir()), [])

    def test_complete_snapshot_is_reused_without_losing_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            (project / "note").write_text("original")
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "SNAPSHOT_DIR", root / "snapshots"),
            ):
                snapshot = dispatch._snapshot_project("demo", self.now, lambda _: None)
                self.assertIsNotNone(snapshot)
                (project / "note").write_text("changed")
                self.assertEqual(
                    dispatch._snapshot_project("demo", self.now, lambda _: None),
                    snapshot,
                )
            self.assertEqual((snapshot / "note").read_text(), "original")
            self.assertTrue((snapshot.parent / ".demo.complete.json").is_file())
            self.assertEqual(list(snapshot.iterdir()), [snapshot / "note"])

    def test_legacy_incomplete_snapshot_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            snapshot = root / "snapshots" / "2026-10-03" / "demo"
            snapshot.mkdir(parents=True)
            (snapshot / "partial").write_text("legacy incomplete copy")
            logs = []
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "SNAPSHOT_DIR", root / "snapshots"),
                patch.object(dispatch.subprocess, "run") as copy,
            ):
                self.assertIsNone(
                    dispatch._snapshot_project("demo", self.now, logs.append)
                )
            copy.assert_not_called()
            self.assertEqual(
                (snapshot / "partial").read_text(), "legacy incomplete copy"
            )
            self.assertIn("preserved for operator review", logs[0])
            self.assertFalse((snapshot.parent / ".demo.complete.json").exists())

    def test_marker_publication_failure_never_authorizes_snapshot_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            (project / "note").write_text("original")
            rename = Path.rename

            def fail_marker(path, target):
                if Path(target).name == ".demo.complete.json":
                    raise OSError("marker failed")
                return rename(path, target)

            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "SNAPSHOT_DIR", root / "snapshots"),
                patch.object(Path, "rename", fail_marker),
            ):
                self.assertIsNone(
                    dispatch._snapshot_project("demo", self.now, lambda _: None)
                )
            snapshot = root / "snapshots" / "2026-10-03" / "demo"
            self.assertEqual((snapshot / "note").read_text(), "original")
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "SNAPSHOT_DIR", root / "snapshots"),
                patch.object(dispatch.subprocess, "run") as copy,
            ):
                self.assertIsNone(
                    dispatch._snapshot_project("demo", self.now, lambda _: None)
                )
            copy.assert_not_called()
            self.assertFalse((snapshot.parent / ".demo.complete.json").exists())

    def test_completion_marker_must_match_current_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            (project / "note").write_text("original")
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "SNAPSHOT_DIR", root / "snapshots"),
            ):
                snapshot = dispatch._snapshot_project("demo", self.now, lambda _: None)
                self.assertIsNotNone(snapshot)
                snapshot.rename(snapshot.with_name("retained-original"))
                snapshot.mkdir()
                (snapshot / "partial").write_text("replacement")
                self.assertIsNone(
                    dispatch._snapshot_project("demo", self.now, lambda _: None)
                )
            self.assertEqual((snapshot / "partial").read_text(), "replacement")

    def fixture(self, root):
        snapshot = root / "snapshots" / "demo"
        project = root / "projects" / "demo"
        snapshot.mkdir(parents=True)
        project.mkdir(parents=True)
        (snapshot / "note").write_text("original")
        (snapshot / "deleted-by-run").write_text("restore me")
        (project / "note").write_text("operator edits")
        (project / "created-by-run").write_text("keep in backup")
        return snapshot, project

    def test_restore_exact_tree_and_preserve_operator_edits(self):
        with tempfile.TemporaryDirectory(prefix="restore spaces ") as temporary:
            snapshot, project = self.fixture(Path(temporary))
            external = Path(temporary) / "external"
            external.write_text("external")
            (snapshot / "link").symlink_to(external)
            backup = restore_project(snapshot, project)
            self.assertEqual((project / "note").read_text(), "original")
            self.assertTrue((project / "deleted-by-run").is_file())
            self.assertFalse((project / "created-by-run").exists())
            self.assertFalse((project / "demo").exists())
            self.assertTrue((project / "link").is_symlink())
            self.assertEqual((backup / "note").read_text(), "operator edits")
            self.assertEqual((backup / "created-by-run").read_text(), "keep in backup")
            self.assertEqual((snapshot / "note").read_text(), "original")
            self.assertEqual(external.read_text(), "external")

    def test_generated_restore_command_restores_the_project(self):
        with tempfile.TemporaryDirectory(prefix="restore command spaces ") as temporary:
            root = Path(temporary)
            snapshot, project = self.fixture(root)
            dated_snapshot = root / "snapshots" / "2026-10-03" / "demo"
            dated_snapshot.parent.mkdir()
            snapshot.rename(dated_snapshot)
            helper = root / "tools" / "schedule" / "restore_project.py"
            helper.parent.mkdir(parents=True)
            shutil.copyfile(
                Path(__file__).resolve().parents[1] / "schedule" / "restore_project.py",
                helper,
            )
            with (
                patch.object(dispatch, "ROOT", root),
                patch.object(dispatch, "SNAPSHOT_DIR", root / "snapshots"),
            ):
                header = dispatch._project_runner_header(["demo"], self.now)
            command = next(
                line for line in header.splitlines() if line.startswith("- `demo`:")
            )
            arguments = shlex.split(command.split("`: `", 1)[1].removesuffix("`"))
            result = subprocess.run(
                [sys.executable, *arguments[1:]], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((project / "note").read_text(), "original")
            self.assertFalse((project / "created-by-run").exists())
            self.assertIn("previous contents preserved", result.stdout)

    def test_restore_copy_failure_keeps_current_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, project = self.fixture(Path(temporary))
            with patch(
                "restore_project.shutil.copytree", side_effect=OSError("copy failed")
            ):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    restore_project(snapshot, project)
            self.assertEqual((project / "note").read_text(), "operator edits")
            self.assertTrue((project / "created-by-run").is_file())

    def test_failed_install_rolls_back_current_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, project = self.fixture(Path(temporary))
            rename = Path.rename

            def fail_install(path, target):
                if (
                    path.parent.name.startswith(".restore-")
                    and path.parent.name != ".restore-backups"
                ):
                    raise OSError("install failed")
                return rename(path, target)

            with patch.object(Path, "rename", fail_install):
                with self.assertRaisesRegex(OSError, "install failed"):
                    restore_project(snapshot, project)
            self.assertEqual((project / "note").read_text(), "operator edits")
            self.assertTrue((project / "created-by-run").is_file())

    def test_reject_symlink_and_nested_restore_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot, project = self.fixture(root)
            link = root / "link"
            link.symlink_to(project, target_is_directory=True)
            for source, destination in (
                (snapshot, link),
                (project, project),
                (root, project),
            ):
                with self.subTest(source=source, destination=destination):
                    with self.assertRaises(ValueError):
                        restore_project(source, destination)
            (project.parent / ".restore-backups").symlink_to(
                snapshot, target_is_directory=True
            )
            with self.assertRaises(ValueError):
                restore_project(snapshot, project)


if __name__ == "__main__":
    unittest.main()
