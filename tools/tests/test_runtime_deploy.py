#!/usr/bin/env python3
"""Verify deployment review, confinement, recovery, and staged operator migration."""

from __future__ import annotations

import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "tests"))

from _loader import load_module  # noqa: E402
from local_access import JsonObject  # noqa: E402

deploy = load_module("runtime_deployment", TOOLS / "runtime/deploy.py")


class DeploymentTests(unittest.TestCase):
    fixture: tempfile.TemporaryDirectory[str]
    root: Path
    source: Path
    destination: Path
    instructions: Path
    adapters: Path
    old_tool: Path

    def setUp(self) -> None:
        self.fixture = tempfile.TemporaryDirectory(prefix="native deployment ")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name).resolve()
        self.source = self.root / "checkout"
        self.destination = self.root / "Brain"
        self.instructions = self.root / "instructions"
        self.adapters = self.root / "adapters"
        for directory in (
            self.source,
            self.destination,
            self.instructions,
            self.adapters,
        ):
            directory.mkdir()
        for relative in deploy.DEPLOY_FILES:
            self.write(self.source / relative, "NEW " + relative)
        for name in deploy.INSTRUCTION_EXPORTS:
            self.write(self.instructions / name, "NEW " + name)
        for relative in deploy.ADAPTER_EXPORTS:
            self.write(self.adapters / relative, "NEW " + relative)
        self.old_tool = self.destination / "tools/agenda.py"
        self.write(self.old_tool, "PRESERVED PREVIOUS LIVE TOOL")
        self.write(self.destination / "AGENTS.md", "OLD SCHEMA")
        self.write(self.destination / "tools/llm.local.json", "PRIVATE PREFERENCE")
        self.write(
            self.destination / "tools/access.local.json", "PRIVATE ACCESS CHOICE"
        )
        self.write(
            self.destination / "tools/runtime-state/providers/claude/local-state",
            "PRIVATE AUTH FIXTURE",
        )
        self.write(
            self.destination / "tools/unrelated.py", "UNRELATED LOCAL IMPLEMENTATION"
        )
        self.write(self.destination / "wiki/note.md", "PRIVATE NOTE FIXTURE")
        self.write(self.destination / "raw/source.md", "IMMUTABLE SOURCE FIXTURE")

    def write(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")

    def plan(self) -> JsonObject:
        return deploy.plan_deployment(
            self.source,
            self.destination,
            instruction_candidates=self.instructions,
            adapter_candidates=self.adapters,
        )

    def deployed(self) -> JsonObject:
        return deploy.apply_deployment(self.plan())

    def tree(self, directory: Path) -> JsonObject:
        return {
            str(path.relative_to(directory)): (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
            for path in directory.rglob("*")
            if path.is_file()
        }

    def test_instructions_the_launcher_requires_are_exported(self) -> None:
        # wiki-agent.py exits 1 without the policy, so a deploy must carry it.
        self.assertIn(".agents/context-policy.md", deploy.PROTECTED_TARGETS)

    def test_plan_lists_exact_hashes_and_pending_candidates_without_writes(
        self,
    ) -> None:
        before = self.tree(self.destination)
        plan = self.plan()
        self.assertEqual(self.tree(self.destination), before)
        self.assertEqual(
            {entry["target"] for entry in plan["files"]}, set(deploy.DEPLOY_FILES)
        )
        self.assertEqual(
            {entry["target"] for entry in plan["protected_candidates"]},
            deploy.PROTECTED_TARGETS,
        )
        entry = next(
            item for item in plan["files"] if item["target"] == "tools/agenda.py"
        )
        self.assertEqual(entry["sha256"], deploy._digest(self.source / entry["target"]))
        self.assertEqual(entry["previous_sha256"], deploy._digest(self.old_tool))
        self.assertEqual(entry["action"], "replace")
        self.assertFalse(
            (self.destination / "tools/runtime-state/deployments").exists()
        )

    def test_apply_preserves_local_data_and_snapshots_replaced_regular_files(
        self,
    ) -> None:
        protected = {
            name: (self.destination / name).read_bytes()
            for name in (
                "AGENTS.md",
                "wiki/note.md",
                "raw/source.md",
                "tools/llm.local.json",
                "tools/access.local.json",
                "tools/unrelated.py",
                "tools/runtime-state/providers/claude/local-state",
            )
        }
        old_tool = self.old_tool.read_bytes()
        result = self.deployed()
        for relative in deploy.DEPLOY_FILES:
            self.assertEqual(
                (self.destination / relative).read_bytes(),
                (self.source / relative).read_bytes(),
            )
        for name, data in protected.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
        snapshot = Path(result["snapshot"])
        self.assertEqual((snapshot / "replaced/tools/agenda.py").read_bytes(), old_tool)
        manifest = json.loads((snapshot / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "tools_deployed_protected_pending")
        self.assertEqual(result["protected_pending"], len(deploy.PROTECTED_TARGETS))
        migration = Path(result["migration"])
        staged = json.loads((migration / "manifest.json").read_text())[
            "protected_candidates"
        ]
        for entry in staged:
            self.assertEqual(
                deploy._digest(migration / entry["staged"]), entry["sha256"]
            )
        self.assertEqual(stat.S_IMODE(migration.stat().st_mode), 0o700)

    def test_unchanged_files_keep_their_inode_and_are_not_backed_up(self) -> None:
        unchanged = self.destination / "tools/local_runtime.py"
        self.write(unchanged, "NEW tools/local_runtime.py")
        inode = unchanged.stat().st_ino
        result = self.deployed()
        self.assertEqual(unchanged.stat().st_ino, inode)
        self.assertFalse(
            (Path(result["snapshot"]) / "replaced/tools/local_runtime.py").exists()
        )

    def test_changed_source_invalidates_review_before_any_replacements(self) -> None:
        plan = self.plan()
        self.write(self.source / "tools/local_runtime.py", "CHANGED AFTER REVIEW")
        before = self.tree(self.destination)
        with self.assertRaisesRegex(ValueError, "source changed after review"):
            deploy.apply_deployment(plan)
        self.assertEqual(self.tree(self.destination), before)

    def test_changed_destination_invalidates_review_before_any_replacements(
        self,
    ) -> None:
        plan = self.plan()
        self.write(self.old_tool, "NEW UNRELATED LIVE EDIT")
        before = self.tree(self.destination)
        with self.assertRaisesRegex(ValueError, "destination changed after review"):
            deploy.apply_deployment(plan)
        self.assertEqual(self.tree(self.destination), before)

    def test_plan_cannot_add_traversal_or_local_preferences_to_the_allowlist(
        self,
    ) -> None:
        for target in (
            "tools/../../outside",
            "tools/llm.local.json",
            "tools/access.local.json",
            "/tmp/outside",
        ):
            plan = self.plan()
            plan["files"][0]["target"] = target
            before = self.tree(self.destination)
            with self.subTest(target=target), self.assertRaises(ValueError):
                deploy.apply_deployment(plan)
            self.assertEqual(self.tree(self.destination), before)

    def test_review_cannot_be_changed_to_skip_required_replacements(self) -> None:
        plan = self.plan()
        plan["files"][0]["action"] = "unchanged"
        with self.assertRaisesRegex(ValueError, "Invalid deployment action"):
            deploy.apply_deployment(plan)
        self.assertIn("PREVIOUS LIVE", self.old_tool.read_text())

    def test_source_and_target_symlinks_are_rejected(self) -> None:
        outside = self.root / "outside.txt"
        self.write(outside, "OUTSIDE")
        for kind in ("source", "target", "candidate"):
            if kind == "source":
                path = self.source / "tools/local_runtime.py"
            elif kind == "target":
                path = self.destination / "tools/local_runtime.py"
            else:
                path = self.instructions / "AGENTS.migration.md"
            previous = path.read_bytes() if path.exists() else None
            path.unlink(missing_ok=True)
            path.symlink_to(outside)
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(ValueError, "Symbolic links"),
            ):
                self.plan()
            self.assertEqual(outside.read_text(), "OUTSIDE\n")
            path.unlink()
            if previous is not None:
                path.write_bytes(previous)

    def test_symlink_ancestors_and_traversal_roots_are_rejected(self) -> None:
        alias = self.root / "alias"
        alias.symlink_to(self.destination, target_is_directory=True)
        for destination in (alias, self.root / "sub/../Brain"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                deploy.plan_deployment(
                    self.source,
                    destination,
                    instruction_candidates=self.instructions,
                    adapter_candidates=self.adapters,
                )

    def test_symlink_snapshot_directory_is_rejected_without_following_it(self) -> None:
        outside = self.root / "outside-directory"
        outside.mkdir()
        snapshots = self.destination / "tools/runtime-state/deployments"
        snapshots.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            self.deployed()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn("PREVIOUS LIVE", self.old_tool.read_text())

    def test_partial_copy_failure_restores_previous_files_and_removes_creations(
        self,
    ) -> None:
        fail_target = self.destination / "tools/agent_profiles.py"
        self.write(fail_target, "OLDER LIVE PROFILE")
        before = self.tree(self.destination)
        original = deploy._atomic_copy
        failed = False

        def copy_once(source: Path, target: Path, *, expected: str) -> None:
            nonlocal failed
            if target == fail_target and not failed:
                failed = True
                raise OSError("synthetic write failure")
            original(source, target, expected=expected)

        with mock.patch.object(deploy, "_atomic_copy", side_effect=copy_once):
            with self.assertRaisesRegex(OSError, "synthetic write failure"):
                self.deployed()
        for relative, (contents, _) in before.items():
            self.assertEqual((self.destination / relative).read_bytes(), contents)
        self.assertFalse((self.destination / "tools/agent_capabilities.py").exists())
        snapshots = self.destination / "tools/runtime-state/deployments"
        manifest = next(snapshots.glob("*/manifest.json"))
        result = json.loads(manifest.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["rollback_conflicts"], [])

    def test_concurrent_deployment_and_active_agent_are_blocked(self) -> None:
        plan = self.plan()
        with deploy._deployment_lock(self.destination):
            with self.assertRaisesRegex(ValueError, "Another deployment"):
                deploy.apply_deployment(plan)
        with mock.patch.dict(os.environ, {"VAULTLENS_RUNTIME_MANIFEST": "synthetic"}):
            with self.assertRaisesRegex(ValueError, "operator action"):
                deploy.apply_deployment(plan)

    def test_operator_migration_uses_only_staged_files_and_selected_fish_functions(
        self,
    ) -> None:
        result = self.deployed()
        migration = Path(result["migration"])
        functions = self.root / "operator-home/.config/fish/functions"
        self.write(functions / "brain-agent.fish", "OLD HOST FUNCTION")
        self.write(functions / "unrelated.fish", "UNRELATED HOST FUNCTION")
        applied = deploy.operator_apply(migration, fish_functions=functions)
        self.assertEqual(
            (self.destination / "AGENTS.md").read_bytes(),
            (self.instructions / "AGENTS.migration.md").read_bytes(),
        )
        for relative in deploy.ADAPTER_EXPORTS:
            self.assertEqual(
                (self.destination / relative).read_bytes(),
                (self.adapters / relative).read_bytes(),
            )
        for name in deploy.SHELL_FILES:
            self.assertEqual(
                (functions / name).read_bytes(),
                (self.destination / "tools/shell" / name).read_bytes(),
            )
        self.assertEqual(
            (functions / "unrelated.fish").read_text(), "UNRELATED HOST FUNCTION\n"
        )
        snapshot = Path(applied["snapshot"])
        self.assertEqual((snapshot / "replaced/AGENTS.md").read_text(), "OLD SCHEMA\n")
        self.assertEqual(
            (snapshot / "replaced/fish/brain-agent.fish").read_text(),
            "OLD HOST FUNCTION\n",
        )

    def test_operator_refuses_candidate_hash_or_manifest_target_changes(self) -> None:
        for kind in ("hash", "target"):
            result = self.deployed()
            migration = Path(result["migration"])
            manifest_path = migration / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            if kind == "hash":
                self.write(
                    migration / manifest["protected_candidates"][0]["staged"],
                    "TAMPERED CANDIDATE",
                )
            else:
                manifest["protected_candidates"][0]["target"] = "raw/source.md"
                manifest_path.write_text(json.dumps(manifest))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                deploy.operator_apply(migration)
            self.assertEqual(
                (self.destination / "AGENTS.md").read_text(), "OLD SCHEMA\n"
            )

    def test_operator_refuses_symlink_fish_target_before_instruction_changes(
        self,
    ) -> None:
        result = self.deployed()
        real = self.root / "real-functions"
        real.mkdir()
        functions = self.root / "operator-home/.config/fish/functions"
        functions.parent.mkdir(parents=True)
        functions.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            deploy.operator_apply(Path(result["migration"]), fish_functions=functions)
        self.assertEqual((self.destination / "AGENTS.md").read_text(), "OLD SCHEMA\n")
        self.assertEqual(list(real.iterdir()), [])

    def test_missing_or_duplicate_bundle_entries_fail_closed(self) -> None:
        plan = self.plan()
        for files in (
            plan["files"][:-1],
            [*plan["files"], copy.deepcopy(plan["files"][0])],
        ):
            invalid = {**plan, "files": files}
            with self.assertRaisesRegex(ValueError, "allowlist"):
                deploy.apply_deployment(invalid)
        (self.source / "tools/local_runtime.py").unlink()
        with self.assertRaisesRegex(ValueError, "regular source"):
            self.plan()

    def test_removed_container_retirement_is_rejected_not_ignored(self) -> None:
        for key, value in (
            ("retirement_manifest", "/old/manifest.json"),
            ("retirement", [{"target": ".devcontainer/Dockerfile"}]),
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "retirement was removed"):
                    deploy.apply_deployment({**self.plan(), key: value})
        self.assertFalse(hasattr(deploy, "RETIRE_FILES"))
        with self.assertRaises(SystemExit):
            deploy.main(["operator-apply", "--migration", "x", "--retire-containers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
