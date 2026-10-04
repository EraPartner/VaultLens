#!/usr/bin/env python3
"""Behavioral privacy profile tests over public synthetic note fixtures."""

import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import local_access as access
from run_reports import Recorder
from scoped_search import ScopedSearch


class AccessProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vaultlens-access-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "tools").mkdir()
        self.policy = {
            "version": 1,
            "profiles": {
                "selected-read": {"reports": "wiki/reports/agents"},
                "wiki-read": {
                    "extends": "selected-read",
                    "read": ["wiki"],
                    "deny_read": ["wiki/private", "wiki/reports/agents"],
                },
                "source-read": {"extends": "wiki-read", "read": ["raw/sources"]},
                "wiki-write": {"extends": "source-read", "write": ["wiki"]},
                "project-write": {
                    "extends": "wiki-read",
                    "read": ["projects/{project}"],
                    "write": ["projects/{project}"],
                },
            },
            "defaults": {"search": "wiki-read", "ingest": "wiki-write"},
        }
        self.write_policy()
        for relative in (
            "wiki/concepts/approved.md",
            "wiki/private/hidden.md",
            "wiki/reports/agents/prior.md",
            "wiki/AGENTS.md",
            "raw/sources/approved.txt",
            "raw/review-inbox/consent.md",
            "projects/alpha/project.md",
            "projects/alpha/notes/approved.md",
            "projects/beta/project.md",
            "projects/beta/notes/sibling.md",
        ):
            self.note(relative)

    def note(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Public synthetic fixture: " + relative + "\n")
        return path

    def write_policy(self, policy=None, *, local=False):
        path = (
            self.root
            / "tools"
            / ("access.local.json" if local else "access-profiles.json")
        )
        path.write_text(json.dumps(self.policy if policy is None else policy))

    def resolve(self, name="wiki-read", **kwargs):
        return access.resolve_scope(self.root, name, **kwargs)

    def test_inheritance_combines_grants_and_keeps_denied_descendants(self):
        self.policy["profiles"]["source-read"]["read"].append("wiki")
        self.write_policy()
        scope = self.resolve("source-read", capability="read")
        self.assertTrue(scope.readable(self.root / "raw/sources/approved.txt"))
        self.assertTrue(scope.readable(self.root / "wiki/concepts/approved.md"))
        self.assertFalse(scope.readable(self.root / "wiki/private/hidden.md"))
        self.assertFalse(scope.readable(self.root / "wiki/reports/agents/prior.md"))
        self.assertEqual(scope.read_paths.count(self.root / "wiki"), 1)
        self.assertEqual(scope.write_paths, ())

    def test_local_profile_replacement_can_narrow_tracked_defaults(self):
        self.write_policy(
            {
                "version": 1,
                "profiles": {"wiki-read": {"read": ["wiki/concepts"]}},
                "defaults": {"ingest": "wiki-read"},
            },
            local=True,
        )
        scope = self.resolve()
        self.assertTrue(scope.readable(self.root / "wiki/concepts/approved.md"))
        self.assertFalse(scope.readable(self.root / "wiki/private/hidden.md"))
        self.assertEqual(access.default_profile("search", self.root), "wiki-read")
        self.assertEqual(access.default_profile("ingest", self.root), "wiki-read")

    def test_policy_schema_and_version_reject_unknown_authority(self):
        malformed = (
            {"version": 2, "profiles": {}, "defaults": {}},
            {"version": 1, "profiles": [], "defaults": {}},
            {"version": 1, "profiles": {}, "defaults": {"search": "absent"}},
            {"version": 1, "profiles": {"base": {"allow_all": True}}},
            {"version": 1, "profiles": {"base": {"read": "wiki"}}},
            {"version": 1, "profiles": {"base": {"read": ["wiki\0/private"]}}},
            {"version": 1, "profiles": {"base": {"review_queue_metadata": "yes"}}},
            {"version": 1, "profiles": {"Bad Name": {}}},
        )
        for data in malformed:
            with self.subTest(data=data):
                self.write_policy(data)
                with self.assertRaises(ValueError):
                    access.load_policy(self.root)

    def test_unknown_parent_and_cycles_fail_before_scope_creation(self):
        for profiles in (
            {"base": {"extends": "missing"}},
            {"base": {"extends": "child"}, "child": {"extends": "base"}},
        ):
            with self.subTest(profiles=profiles):
                self.write_policy({"version": 1, "profiles": profiles})
                with self.assertRaisesRegex(ValueError, "Unknown or cyclic"):
                    access.load_policy(self.root)

    def test_invalid_note_paths_cannot_grant_tools_external_or_consent_data(self):
        for selection in (
            "..",
            "/etc",
            "wiki/../raw",
            "wiki\\private",
            "tools",
            "wiki/{unknown}",
            "raw/review-inbox",
            "wiki/.git/public.md",
        ):
            with self.subTest(selection=selection):
                policy = copy.deepcopy(self.policy)
                policy["profiles"]["invalid"] = {"read": [selection]}
                self.write_policy(policy)
                with self.assertRaises(ValueError):
                    self.resolve("invalid")

    def test_explicit_absolute_read_stays_selected_and_inside_vault(self):
        selected = self.root / "projects/alpha/notes/approved.md"
        scope = self.resolve("selected-read", read_paths=(str(selected),))
        self.assertTrue(scope.readable(selected))
        self.assertFalse(scope.readable(self.root / "projects/alpha/project.md"))
        with self.assertRaisesRegex(ValueError, "within this vault"):
            self.resolve(
                "selected-read", read_paths=(str(self.root.parent / "outside.md"),)
            )
        with self.assertRaisesRegex(ValueError, "denied path"):
            self.resolve(read_paths=("wiki/private/hidden.md",))

    def test_read_roles_cannot_elevate_to_writers(self):
        # Use a profile whose deny list permits its writer root so only the role
        # compatibility check decides whether an elevation is allowed.
        self.policy["profiles"]["writer"] = {"write": ["wiki/concepts"]}
        self.write_policy()
        for capability in ("read", "read-shell"):
            with self.subTest(capability=capability):
                with self.assertRaisesRegex(ValueError, "Reader roles"):
                    self.resolve("writer", capability=capability)
        scope = self.resolve("writer", capability="wiki-write")
        self.assertTrue(scope.writable(self.root / "wiki/concepts/new.md"))

    def test_writers_are_restricted_to_the_selected_layer_and_project(self):
        scope = self.resolve(
            "project-write", project="alpha", capability="project-write"
        )
        self.assertTrue(scope.writable(self.root / "projects/alpha/notes/new.md"))
        self.assertTrue(scope.readable(self.root / "wiki/concepts/approved.md"))
        self.assertFalse(scope.writable(self.root / "wiki/concepts/approved.md"))
        self.assertFalse(scope.readable(self.root / "projects/beta/notes/sibling.md"))
        self.assertFalse(scope.writable(self.root / "projects/beta/notes/new.md"))
        with self.assertRaisesRegex(ValueError, "wiki-only"):
            self.resolve("project-write", project="alpha", capability="wiki-write")
        with self.assertRaisesRegex(ValueError, "exactly one project"):
            self.resolve("wiki-write", project="alpha", capability="project-write")

    def test_write_root_must_be_exact_and_never_raw(self):
        for write in (
            "raw/sources",
            "projects",
            "projects/beta",
            "projects/alpha/notes",
            "wiki/*",
        ):
            with self.subTest(write=write):
                policy = copy.deepcopy(self.policy)
                policy["profiles"]["bad-write"] = {"write": [write]}
                self.write_policy(policy)
                with self.assertRaises(ValueError):
                    self.resolve("bad-write", project="alpha")

    def test_project_template_requires_a_single_valid_slug(self):
        for project in (None, "../alpha", "alpha/beta", "", ".", "alpha\\beta"):
            with self.subTest(project=project):
                with self.assertRaisesRegex(ValueError, "valid project slug"):
                    self.resolve("project-write", project=project)

    def test_read_globs_select_only_matching_metadata(self):
        self.policy["profiles"]["metadata"] = {"read": ["projects/*/project.md"]}
        self.write_policy()
        scope = self.resolve("metadata")
        self.assertEqual(
            scope.project_directories(),
            [self.root / "projects/alpha", self.root / "projects/beta"],
        )
        self.assertEqual(
            scope.document_paths(),
            [
                self.root / "projects/alpha/project.md",
                self.root / "projects/beta/project.md",
            ],
        )
        self.assertFalse(scope.readable(self.root / "projects/alpha/notes/approved.md"))
        self.assertFalse(scope.readable(self.root / "projects"))

    def test_denied_globs_cannot_freeze_exclusions_to_existing_matches(self):
        self.policy["profiles"]["filtered-writer"] = {
            "read": ["wiki"],
            "write": ["wiki"],
            "deny_read": ["wiki/private/*.md"],
        }
        self.write_policy()
        with self.assertRaisesRegex(ValueError, "never snapshot globs"):
            self.resolve("filtered-writer", capability="wiki-write")
        self.policy["profiles"]["filtered-writer"]["deny_read"] = ["wiki/private"]
        self.write_policy()
        scope = self.resolve("filtered-writer", capability="wiki-write")
        self.note("wiki/private/future.md")
        self.assertNotIn(self.root / "wiki/private/hidden.md", scope.document_paths())
        self.assertNotIn(self.root / "wiki/private/future.md", scope.document_paths())
        self.assertFalse(scope.writable(self.root / "wiki/private/hidden.md"))
        self.assertTrue(scope.writable(self.root / "wiki/concepts/approved.md"))

    def test_document_selection_excludes_instructions_and_symlink_aliases(self):
        (self.root / "wiki/concepts/linked.md").symlink_to(
            self.root / "raw/review-inbox/consent.md"
        )
        (self.root / "wiki/concepts/linked-directory").symlink_to(
            self.root / "projects/beta", target_is_directory=True
        )
        scope = self.resolve()
        paths = scope.document_paths()
        self.assertIn(self.root / "wiki/concepts/approved.md", paths)
        self.assertNotIn(self.root / "wiki/AGENTS.md", paths)
        self.assertNotIn(self.root / "wiki/concepts/linked.md", paths)
        self.assertFalse(
            scope.readable(self.root / "wiki/concepts/linked-directory/project.md")
        )
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.resolve("selected-read", read_paths=("wiki/concepts/linked.md",))

    def test_future_write_paths_cannot_cross_a_symlink_parent(self):
        (self.root / "projects/alias").symlink_to(
            self.root / "projects/alpha", target_is_directory=True
        )
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.resolve("project-write", project="alias", capability="project-write")

    def test_denied_children_and_protected_instruction_files_remain_closed(self):
        self.policy["profiles"]["writer"] = {
            "read": ["wiki"],
            "write": ["wiki"],
            "deny_read": ["wiki/private"],
        }
        self.write_policy()
        scope = self.resolve("writer", capability="wiki-write")
        self.assertFalse(scope.readable(self.root / "wiki/private/new/child.md"))
        self.assertFalse(scope.writable(self.root / "wiki/private/new/child.md"))
        for name in access.PROTECTED_NAMES:
            self.assertFalse(scope.writable(self.root / "wiki/concepts" / name), name)

    def test_secret_path_rules_cover_credentials_and_backups_without_reading_them(self):
        # These are path objects only. Never materialize or open any credential
        # file; the values verify matching logic and compiled runtime policy.
        for name in (
            ".env",
            ".env.production",
            "signing.key",
            "tls.pem",
            "store.p12",
            "store.pfx",
            "store.jks",
            "store.keystore",
            "auth.json",
            "auth.json.backup",
            ".credentials.json",
            "credentials.json",
            "credentials.toml",
            "secrets.json",
            "token.json",
            "tokens.json",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
            ".claude.json",
            "id_rsa",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    access.forbidden(self.root / "wiki/concepts" / name, self.root)
                )

    def test_forbidden_directories_and_consent_queue_apply_to_broad_raw_reads(self):
        self.policy["profiles"]["raw-read"] = {"read": ["raw"]}
        self.write_policy()
        scope = self.resolve("raw-read", capability="read")
        self.assertTrue(scope.readable(self.root / "raw/sources/approved.txt"))
        self.assertFalse(scope.readable(self.root / "raw/review-inbox/consent.md"))
        for directory in access.FORBIDDEN_DIRS:
            self.assertFalse(
                scope.readable(self.root / "raw" / directory / "public.md"), directory
            )

    def test_research_domains_are_opt_in_and_reports_stay_dedicated(self):
        self.assertEqual(self.resolve().research_domains, ())
        for domain in (
            "*",
            "localhost",
            "127.0.0.1",
            "https://example.org",
            "example.org:80",
            "../example.org",
        ):
            with self.subTest(domain=domain):
                policy = copy.deepcopy(self.policy)
                policy["profiles"]["research"] = {"research_domains": [domain]}
                self.write_policy(policy)
                with self.assertRaisesRegex(ValueError, "explicit HTTPS"):
                    self.resolve("research")
        self.policy["profiles"]["research"] = {"research_domains": ["example.org:443"]}
        self.write_policy()
        self.assertEqual(
            self.resolve("research").research_domains, ("example.org:443",)
        )
        for reports in (
            "projects/alpha",
            "wiki/private",
            "wiki/reports/*",
            "wiki/reports",
            "wiki/reports/isolated-agent",
        ):
            with self.subTest(reports=reports):
                policy = copy.deepcopy(self.policy)
                policy["profiles"]["bad-reports"] = {"reports": reports}
                self.write_policy(policy)
                with self.assertRaises(ValueError):
                    self.resolve("bad-reports")

    def test_private_report_output_cannot_enter_another_agents_notes_or_search(self):
        self.policy["profiles"]["isolated-read"] = {
            "read": ["raw/sources/approved.txt"],
            "reports": "wiki/reports/agents/isolated",
        }
        self.policy["profiles"]["whole-wiki"] = {"read": ["wiki"]}
        self.write_policy()
        producer = self.resolve("isolated-read")
        recorder = Recorder(producer, "search", "codex")
        recorder.consume(io.StringIO("PublicDerivedReportCanary\n"), io.StringIO())
        report = recorder.finish(0)
        self.assertEqual(report.parent, self.root / "wiki/reports/agents/isolated")
        for profile in ("wiki-read", "whole-wiki"):
            with self.subTest(profile=profile):
                reader = self.resolve(profile)
                self.assertFalse(
                    reader.readable(self.root / "raw/sources/approved.txt")
                )
                self.assertFalse(reader.readable(report))
                self.assertNotIn(report, reader.document_paths())
                self.assertEqual(
                    ScopedSearch(reader).search({"query": "PublicDerivedReportCanary"})[
                        "results"
                    ],
                    [],
                )
        with self.assertRaisesRegex(ValueError, "denied path"):
            self.resolve("selected-read", read_paths=(str(report),))


if __name__ == "__main__":
    unittest.main()
