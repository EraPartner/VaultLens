#!/usr/bin/env python3
"""Review and deploy an explicit native-runtime tools bundle without touching notes.

The normal apply command writes only below the destination's tools directory.
Protected instructions and provider adapters are staged as inert candidates. The
operator-apply command is a separate manual action, never an agent launch step.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

# Deployment plans, journals and manifests are JSON documents that round-trip through
# files and are re-validated field by field (_validate_plan, _operator_apply_locked)
# before use, so their values are typed Any at this boundary only.
JsonObject = dict[str, Any]

ROOT = Path(__file__).resolve().parents[2]
SHELL_FILES = (
    "__brain_launch.fish",
    "__brain_project_root.fish",
    "brain-agent.fish",
    "brain-claude.fish",
    "brain-codex.fish",
    "brain-cos.fish",
    "brain-provider.fish",
    "brain-shell.fish",
    "brain-wiki.fish",
)
# Explicit entry points and their local Python dependencies. Runtime state,
# local preferences, raw material, notes, host configuration and tests are absent.
DEPLOY_FILES = (
    "tools/agenda.py",
    "tools/agent_capabilities.py",
    "tools/agent_profiles.py",
    "tools/agents/generate-adapters.py",
    "tools/agents/wiki-agent.py",
    "tools/brain_launch.py",
    "tools/context_budget.py",
    "tools/context_sources.py",
    "tools/llm_provider.py",
    "tools/local_access.py",
    "tools/local_runtime.py",
    "tools/runtime_maintenance.py",
    "tools/process_control.py",
    "tools/macos_processes.py",
    "tools/runtime_verification.py",
    "tools/runtime_probe_checks.py",
    "tools/run_reports.py",
    "tools/project_state.py",
    "tools/provider_commands.py",
    "tools/scoped_search.py",
    "tools/access-profiles.json",
    "tools/model-profiles.json",
    "tools/schedule/dispatch.py",
    "tools/schedule/render_plist.py",
    "tools/schedule/restore_project.py",
    "tools/schedule/install.sh",
    "tools/schedule/com.brain.schedule.plist",
    "tools/schedule/brain-schedule.sudoers",
    "tools/schedule/SPEC.md",
    "tools/runtime/install.sh",
    "tools/runtime/maintain.py",
    "tools/runtime/check_boundary.py",
    "tools/runtime/macos-process-guard.mjs",
    "tools/runtime/probe.py",
    "tools/runtime/deploy.py",
    "tools/runtime/README.md",
    "tools/scripts/authenticate-provider.sh",
    "tools/scripts/provider-smoke.py",
    "tools/scripts/repair-provider-host.py",
    "tools/wiki.py",
    "tools/wiki_archive.py",
    "tools/wiki_index.py",
    "tools/wiki_ingest.py",
    "tools/wiki_init.py",
    "tools/wiki_inventory.py",
    "tools/wiki_links.py",
    "tools/wiki_lint.py",
    "tools/wiki_log.py",
    "tools/wiki_projects.py",
    "tools/wiki_query.py",
    *("tools/shell/" + name for name in SHELL_FILES),
)
ROLE_NAMES = (
    "wiki-challenge",
    "wiki-connect",
    "wiki-contradiction-detector",
    "wiki-cos",
    "wiki-emerge",
    "wiki-enhancer",
    "wiki-idea-discovery",
    "wiki-ingest",
    "wiki-project-runner",
    "wiki-quality-reviewer",
    "wiki-search",
    "wiki-source-verifier",
)
INSTRUCTION_EXPORTS = {
    "AGENTS.migration.md": "AGENTS.md",
    "wiki-agents.migration.md": ".agents/skills/wiki-agents/SKILL.md",
    "project-agents.migration.md": "projects/AGENTS.md",
    "wiki-project-clarify.migration.md": ".agents/skills/wiki-project-clarify/SKILL.md",
    **{f"roles/{name}.md": f".agents/roles/{name}.md" for name in ROLE_NAMES},
}
ADAPTER_EXPORTS = (
    *(f".claude/agents/{name}.md" for name in ROLE_NAMES),
    *(f".codex/agents/{name}.toml" for name in ROLE_NAMES),
)
PROTECTED_TARGETS = frozenset((*INSTRUCTION_EXPORTS.values(), *ADAPTER_EXPORTS))
RETIRE_FILES = frozenset(
    ".devcontainer/" + name
    for name in (
        ".dockerignore",
        "Dockerfile",
        "allowlist.extra.txt",
        "allowlist.txt",
        "bin/claude",
        "bin/codex",
        "bin/doctor",
        "bin/verify-pins",
        "claude-explicit-format.sh",
        "claude-guard.mjs",
        "claude-policy-install.sh",
        "claude-policy-verify.sh",
        "claude-post-edit.mjs",
        "claude-stage-items.txt",
        "clienthello-policy.py",
        "entrypoint.sh",
        "init-firewall.sh",
        "launcher-common.sh",
        "perms-fix.sh",
        "post-create.sh",
        "post-start.sh",
        "session-launch.py",
        "squid.conf",
    )
)
PRIVATE_LAUNCHER = ".devcontainer/bin/agent"


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal is not allowed: {path}")
    return path.expanduser().absolute()


def _no_alias(path: Path, *, regular: bool = False) -> Path:
    path = _absolute(path)
    for ancestor in (*reversed(path.parents), path):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Symbolic links are not allowed: {ancestor}")
        if ancestor != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"A path ancestor is not a directory: {ancestor}")
    if regular:
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(f"A regular source file is required: {path}")
    elif path.exists() and not (path.is_file() or path.is_dir()):
        raise ValueError(f"A regular file or directory is required: {path}")
    return path


def _root(path: Path) -> Path:
    path = _no_alias(path)
    if not path.is_dir():
        raise ValueError(f"A directory is required: {path}")
    return path


def _target(root: Path, relative: str) -> Path:
    value = Path(relative)
    if (
        value.is_absolute()
        or not value.parts
        or any(part in {".", ".."} for part in value.parts)
    ):
        raise ValueError(f"Invalid relative deployment target: {relative!r}")
    result = _no_alias(root / value)
    if result.exists() and not result.is_file():
        raise ValueError(f"Deployment cannot replace a directory: {result}")
    return result


def _digest(path: Path) -> str:
    _no_alias(path, regular=True)
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _existing(path: Path) -> str | None:
    _no_alias(path)
    return _digest(path) if path.exists() else None


def _entry(source: Path, target: str, destination: Path | None) -> JsonObject:
    _no_alias(source, regular=True)
    source_hash = _digest(source)
    old_hash = _existing(_target(destination, target)) if destination else None
    return {
        "source": str(source),
        "target": target,
        "sha256": source_hash,
        "size": source.stat().st_size,
        "mode": stat.S_IMODE(source.stat().st_mode) & 0o755,
        "previous_sha256": old_hash,
        "action": "pending"
        if destination is None
        else "unchanged"
        if old_hash == source_hash
        else "replace"
        if old_hash is not None
        else "create",
    }


def _retirement_plan(manifest_path: Path, destination: Path) -> list[JsonObject]:
    manifest_path = _no_alias(manifest_path, regular=True)
    loaded: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Invalid source retirement manifest")
    # isinstance narrows to dict[Unknown, Unknown]; JSON object keys are always str.
    manifest = cast(JsonObject, loaded)
    if not isinstance(manifest.get("records"), list):
        raise ValueError("Invalid source retirement manifest")
    entries: list[JsonObject] = []
    for raw_record in manifest["records"]:
        if not isinstance(raw_record, dict):
            raise ValueError("Retirement records must be objects")
        record = cast(JsonObject, raw_record)  # keys are str, as above
        target: str = record.get("path", "")
        if record.get("action") != "retire" or not target.startswith(".devcontainer/"):
            continue
        if target not in RETIRE_FILES or any(
            entry["target"] == target for entry in entries
        ):
            raise ValueError(f"Unknown or duplicate retirement target: {target}")
        source = _no_alias(manifest_path.parent / "source" / target, regular=True)
        if _digest(source) != record.get("sha256"):
            raise ValueError(f"Retirement source hash mismatch: {target}")
        entries.append(
            {
                "source": str(source),
                "target": target,
                "source_sha256": record["sha256"],
                "previous_sha256": _existing(_target(destination, target)),
            }
        )
    if {entry["target"] for entry in entries} != set(RETIRE_FILES):
        raise ValueError(
            "Retirement source manifest must cover the exact known container bundle"
        )
    launcher = _target(destination, PRIVATE_LAUNCHER)
    if launcher.exists():
        entries.append(
            {
                "source": None,
                "target": PRIVATE_LAUNCHER,
                "source_sha256": None,
                "previous_sha256": _digest(launcher),
            }
        )
    return entries


def plan_deployment(
    source: Path,
    destination: Path,
    *,
    instruction_candidates: Path,
    adapter_candidates: Path,
    retirement_manifest: Path | None = None,
) -> JsonObject:
    source, destination = _root(source), _root(destination)
    if (
        source == destination
        or source.is_relative_to(destination)
        or destination.is_relative_to(source)
    ):
        raise ValueError("Source and destination checkouts must be separate")
    instructions, adapters = _root(instruction_candidates), _root(adapter_candidates)
    files = [_entry(source / name, name, destination) for name in DEPLOY_FILES]
    pending = [
        _entry(instructions / name, target, None)
        for name, target in INSTRUCTION_EXPORTS.items()
    ]
    pending.extend(_entry(adapters / name, name, None) for name in ADAPTER_EXPORTS)
    return {
        "version": 1,
        "source": str(source),
        "destination": str(destination),
        "instruction_candidates": str(instructions),
        "adapter_candidates": str(adapters),
        "files": files,
        "protected_candidates": pending,
        "fish_functions_pending": list(SHELL_FILES),
        "retirement_manifest": str(_no_alias(retirement_manifest, regular=True))
        if retirement_manifest
        else None,
        "retirement_manifest_sha256": _digest(retirement_manifest)
        if retirement_manifest
        else None,
        "retirement": _retirement_plan(retirement_manifest, destination)
        if retirement_manifest
        else [],
        "preserved": [
            "notes",
            "raw",
            "tools/llm.local.json",
            "tools/access.local.json",
            "provider state",
            "host configuration",
        ],
    }


def _validate_plan(plan: JsonObject) -> None:
    # The plan is parsed from a file the operator reviewed; re-check its shape at runtime.
    if (
        not isinstance(plan, dict)  # pyright: ignore[reportUnnecessaryIsInstance] -- runtime validation
        or plan.get("version") != 1
    ):
        raise ValueError("Unsupported deployment plan")
    if not isinstance(plan.get("files"), list) or not isinstance(
        plan.get("protected_candidates"), list
    ):
        raise ValueError("A plan requires exact files and protected candidates")
    if any(
        not isinstance(entry, dict)
        for entry in (*plan["files"], *plan["protected_candidates"])
    ):
        raise ValueError("Deployment entries must be objects")
    if {entry.get("target") for entry in plan["files"]} != set(DEPLOY_FILES) or len(
        plan["files"]
    ) != len(DEPLOY_FILES):
        raise ValueError("Deployment files must match the explicit tools allowlist")
    if {
        entry.get("target") for entry in plan["protected_candidates"]
    } != set(PROTECTED_TARGETS) or len(plan["protected_candidates"]) != len(
        PROTECTED_TARGETS
    ):
        raise ValueError(
            "Protected candidates must match the explicit migration allowlist"
        )
    source, destination = _root(Path(plan["source"])), _root(Path(plan["destination"]))
    instructions, adapters = (
        _root(Path(plan["instruction_candidates"])),
        _root(Path(plan["adapter_candidates"])),
    )
    if (
        source == destination
        or source.is_relative_to(destination)
        or destination.is_relative_to(source)
    ):
        raise ValueError("Source and destination checkouts must be separate")
    export_sources = {
        target: instructions / name for name, target in INSTRUCTION_EXPORTS.items()
    }
    export_sources.update({name: adapters / name for name in ADAPTER_EXPORTS})
    if plan.get("retirement_manifest"):
        manifest_path = Path(plan["retirement_manifest"])
        if _digest(manifest_path) != plan.get(
            "retirement_manifest_sha256"
        ) or _retirement_plan(manifest_path, destination) != plan.get("retirement"):
            raise ValueError("Container retirement changed after review")
    elif plan.get("retirement"):
        raise ValueError("Retirement requires an exact reviewed source manifest")
    for entry in (*plan["files"], *plan["protected_candidates"]):
        expected_source = (
            source / entry["target"]
            if entry["target"] in DEPLOY_FILES
            else export_sources[entry["target"]]
        )
        if entry.get("source") != str(expected_source) or _digest(
            expected_source
        ) != entry.get("sha256"):
            raise ValueError(
                f"Deployment source changed after review: {entry['target']}"
            )
        if (
            entry.get("mode") != stat.S_IMODE(expected_source.stat().st_mode) & 0o755
            or entry.get("size") != expected_source.stat().st_size
        ):
            raise ValueError(
                f"Deployment source metadata changed after review: {entry['target']}"
            )
        if entry["target"] in DEPLOY_FILES:
            target = _target(destination, entry["target"])
            if _existing(target) != entry.get("previous_sha256"):
                raise ValueError(
                    f"Deployment destination changed after review: {entry['target']}"
                )
            old_hash = entry["previous_sha256"]
            expected_action = (
                "unchanged"
                if old_hash == entry["sha256"]
                else "replace"
                if old_hash is not None
                else "create"
            )
            if entry.get("action") != expected_action:
                raise ValueError(f"Invalid deployment action: {entry['target']}")


def _atomic_copy(source: Path, target: Path, *, expected: str) -> None:
    _no_alias(source, regular=True)
    _no_alias(target)
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Source changed while copying: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    _no_alias(target.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".vaultlens-deploy-", dir=target.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Keep execute/read bits without propagating group/other write access.
        temporary_path.chmod(stat.S_IMODE(source.stat().st_mode) & 0o755)
        _no_alias(target)
        os.replace(temporary_path, target)
        if _digest(target) != expected:
            raise ValueError(f"Deployment hash readback failed: {target}")
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json(path: Path, data: JsonObject) -> None:
    _no_alias(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".vaultlens-manifest-", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _no_alias(path)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _new_id() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        + uuid.uuid4().hex[:12]
    )


def _replace_entries(
    entries: list[JsonObject], snapshot: Path, journal: JsonObject
) -> None:
    completed: list[JsonObject] = []
    try:
        for entry in entries:
            target = Path(entry["destination"])
            if _existing(target) != entry["previous_sha256"]:
                raise ValueError(f"Destination changed during deployment: {target}")
            if entry["previous_sha256"] is not None:
                backup = snapshot / "replaced" / entry["backup_name"]
                _atomic_copy(target, backup, expected=entry["previous_sha256"])
            completed.append(entry)
            if entry.get("action") == "retire":
                target.unlink()
            else:
                _atomic_copy(Path(entry["source"]), target, expected=entry["sha256"])
            journal["applied"].append(entry["target"])
            _write_json(snapshot / "manifest.json", journal)
    except Exception as exc:
        conflicts: list[str] = []
        for entry in reversed(completed):
            target = Path(entry["destination"])
            current = _existing(target)
            if current not in {entry["sha256"], entry["previous_sha256"]}:
                conflicts.append(entry["target"])
                continue
            if entry["previous_sha256"] is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_copy(
                    snapshot / "replaced" / entry["backup_name"],
                    target,
                    expected=entry["previous_sha256"],
                )
        journal.update(status="failed", error=str(exc), rollback_conflicts=conflicts)
        _write_json(snapshot / "manifest.json", journal)
        raise


@contextlib.contextmanager
def _deployment_lock(destination: Path) -> Generator[None, None, None]:
    lock_path = _no_alias(
        destination / "tools/runtime-state/deployments/.deployment.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another deployment is active") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def apply_deployment(plan: JsonObject) -> JsonObject:
    if os.environ.get("VAULTLENS_RUNTIME_MANIFEST"):
        raise ValueError("Deployment is an operator action outside agent runs")
    _validate_plan(plan)
    with _deployment_lock(Path(plan["destination"])):
        _validate_plan(plan)
        return _apply_validated(plan)


def _apply_validated(plan: JsonObject) -> JsonObject:
    destination = Path(plan["destination"])
    deployment_id = _new_id()
    snapshot = _no_alias(
        destination / "tools/runtime-state/deployments" / deployment_id
    )
    migration = _no_alias(destination / "tools/runtime/migration" / deployment_id)
    snapshot.mkdir(parents=True, mode=0o700)
    migration.mkdir(parents=True, mode=0o700)
    journal: JsonObject = {
        **plan,
        "deployment_id": deployment_id,
        "status": "applying",
        "applied": [],
        "migration": str(migration),
    }
    _write_json(snapshot / "manifest.json", journal)
    staged: list[JsonObject] = []
    retirement: list[JsonObject] = []
    try:
        for index, entry in enumerate(plan["protected_candidates"]):
            relative = f"candidates/{index:02d}-{Path(entry['target']).name}.candidate"
            target = _target(migration, relative)
            _atomic_copy(Path(entry["source"]), target, expected=entry["sha256"])
            staged.append({**entry, "staged": relative})
        for index, entry in enumerate(plan.get("retirement", [])):
            if entry["source"] is not None:
                relative = f"retired-sources/{index:02d}-{Path(entry['target']).name}.candidate"
                _atomic_copy(
                    Path(entry["source"]),
                    _target(migration, relative),
                    expected=entry["source_sha256"],
                )
                retirement.append({**entry, "staged": relative})
            else:
                retirement.append({**entry, "staged": None})
    except (ValueError, OSError) as exc:
        journal.update(
            status="failed_staging", error=str(exc), protected_candidates=staged
        )
        _write_json(snapshot / "manifest.json", journal)
        raise
    files = [
        {
            **entry,
            "destination": str(_target(destination, entry["target"])),
            "backup_name": entry["target"],
        }
        for entry in plan["files"]
        if entry["action"] != "unchanged"
    ]
    _replace_entries(files, snapshot, journal)
    migration_manifest = {
        "version": 1,
        "deployment_id": deployment_id,
        "destination": str(destination),
        "protected_candidates": staged,
        "fish_files": [
            entry
            for entry in plan["files"]
            if entry["target"].startswith("tools/shell/")
        ],
        "status": "pending_operator_apply",
        "retirement": retirement,
    }
    _write_json(migration / "manifest.json", migration_manifest)
    journal.update(
        status="tools_deployed_protected_pending", protected_candidates=staged
    )
    _write_json(snapshot / "manifest.json", journal)
    return {
        "deployment_id": deployment_id,
        "replaced": len(files),
        "snapshot": str(snapshot),
        "migration": str(migration),
        "protected_pending": len(staged),
        "retirement_pending": len(retirement),
        "fish_functions_pending": list(SHELL_FILES),
        "operator_command": [
            sys.executable,
            str(destination / "tools/runtime/deploy.py"),
            "operator-apply",
            "--migration",
            str(migration),
        ],
    }


def operator_apply(
    migration: Path,
    *,
    fish_functions: Path | None = None,
    retire_containers: bool = False,
) -> JsonObject:
    """Manually install reviewed candidates; never called by the agent runtime."""
    if os.environ.get("VAULTLENS_RUNTIME_MANIFEST"):
        raise ValueError("Protected migration is an operator action outside agent runs")
    migration = _root(migration)
    if tuple(parent.name for parent in migration.parents[:3]) != (
        "migration",
        "runtime",
        "tools",
    ):
        raise ValueError(
            "A destination tools/runtime/migration/<id> directory is required"
        )
    destination = _root(migration.parents[3])
    with _deployment_lock(destination):
        return _operator_apply_locked(
            migration,
            destination,
            fish_functions=fish_functions,
            retire_containers=retire_containers,
        )


def _operator_apply_locked(
    migration: Path,
    destination: Path,
    *,
    fish_functions: Path | None,
    retire_containers: bool,
) -> JsonObject:
    manifest_path = _no_alias(migration / "manifest.json", regular=True)
    loaded: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("A protected migration manifest must be an object")
    # isinstance narrows to dict[Unknown, Unknown]; JSON object keys are always str.
    manifest = cast(JsonObject, loaded)
    if (
        manifest.get("version") != 1
        or manifest.get("destination") != str(destination)
        or manifest.get("deployment_id") != migration.name
    ):
        raise ValueError("Invalid protected migration manifest")
    pending = manifest.get("protected_candidates", [])
    if (
        len(pending) != len(PROTECTED_TARGETS)
        or {entry.get("target") for entry in pending} != set(PROTECTED_TARGETS)
    ):
        raise ValueError(
            "Protected migration must match the exact instruction and adapter allowlist"
        )
    entries: list[JsonObject] = []
    for index, entry in enumerate(pending):
        expected_staged = (
            f"candidates/{index:02d}-{Path(entry['target']).name}.candidate"
        )
        if entry.get("staged") != expected_staged:
            raise ValueError(f"Invalid staged migration target: {entry['target']}")
        staged = _target(migration, entry["staged"])
        if _digest(staged) != entry["sha256"]:
            raise ValueError(f"Protected candidate changed: {entry['target']}")
        target = _target(destination, entry["target"])
        if _existing(target) != entry["sha256"]:
            entries.append(
                {
                    **entry,
                    "source": str(staged),
                    "destination": str(target),
                    "previous_sha256": _existing(target),
                    "backup_name": entry["target"],
                }
            )
    if fish_functions is not None:
        fish_functions = _no_alias(fish_functions)
        if fish_functions.name != "functions" or fish_functions.parent.name != "fish":
            raise ValueError("Choose an explicit fish/functions directory")
        if fish_functions.exists() and not fish_functions.is_dir():
            raise ValueError("The fish functions target must be a directory")
        fish_entries = manifest.get("fish_files", [])
        if {entry.get("target") for entry in fish_entries} != {
            "tools/shell/" + name for name in SHELL_FILES
        } or len(fish_entries) != len(SHELL_FILES):
            raise ValueError("Fish migration must match the exact wrapper allowlist")
        for entry in fish_entries:
            source = _target(destination, entry["target"])
            if _digest(source) != entry["sha256"]:
                raise ValueError(f"Deployed fish wrapper changed: {entry['target']}")
            target = _target(fish_functions, source.name)
            if _existing(target) != entry["sha256"]:
                entries.append(
                    {
                        **entry,
                        "source": str(source),
                        "destination": str(target),
                        "previous_sha256": _existing(target),
                        "backup_name": "fish/" + source.name,
                    }
                )
    if retire_containers:
        retirement = manifest.get("retirement", [])
        if not retirement or any(not isinstance(entry, dict) for entry in retirement):
            raise ValueError("Container retirement requires an exact staged manifest")
        targets = [entry.get("target") for entry in retirement]
        if (
            not set(targets).issubset(RETIRE_FILES | {PRIVATE_LAUNCHER})
            or not RETIRE_FILES.issubset(targets)
            or len(targets) != len(set(targets))
        ):
            raise ValueError("Invalid container retirement allowlist")
        for index, entry in enumerate(retirement):
            if entry["target"] != PRIVATE_LAUNCHER:
                expected_staged = f"retired-sources/{index:02d}-{Path(entry['target']).name}.candidate"
                if entry.get("staged") != expected_staged or _digest(
                    _target(migration, expected_staged)
                ) != entry.get("source_sha256"):
                    raise ValueError(f"Retirement source changed: {entry['target']}")
            target = _target(destination, entry["target"])
            current = _existing(target)
            if current != entry["previous_sha256"]:
                raise ValueError(
                    f"Container target changed after review: {entry['target']}"
                )
            if current is not None:
                entries.append(
                    {
                        **entry,
                        "sha256": None,
                        "source": None,
                        "destination": str(target),
                        "backup_name": entry["target"],
                        "action": "retire",
                    }
                )
    snapshot = _no_alias(
        destination / "tools/runtime-state/deployments" / (_new_id() + "-operator")
    )
    snapshot.mkdir(parents=True, mode=0o700)
    journal: JsonObject = {
        "version": 1,
        "migration": str(migration),
        "status": "applying",
        "files": entries,
        "applied": [],
    }
    _write_json(snapshot / "manifest.json", journal)
    _replace_entries(entries, snapshot, journal)
    journal.update(
        status="operator_applied",
        fish_functions=str(fish_functions) if fish_functions else None,
    )
    _write_json(snapshot / "manifest.json", journal)
    return {
        "applied": len(entries),
        "snapshot": str(snapshot),
        "fish_functions_applied": fish_functions is not None,
        "container_files_retired": sum(
            entry.get("action") == "retire" for entry in entries
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser(
        "plan", help="print an exact deployment plan without changing files"
    )
    preview.add_argument("--source", type=Path, default=ROOT)
    preview.add_argument("--destination", type=Path, required=True)
    preview.add_argument("--instruction-candidates", type=Path, required=True)
    preview.add_argument("--adapter-candidates", type=Path, required=True)
    preview.add_argument("--retirement-manifest", type=Path)
    apply = commands.add_parser(
        "apply", help="apply a reviewed plan to destination tools only"
    )
    apply.add_argument("--plan", type=Path, required=True)
    operator = commands.add_parser(
        "operator-apply",
        help="MANUAL: install staged protected files and optional fish wrappers",
    )
    operator.add_argument("--migration", type=Path, required=True)
    operator.add_argument("--fish-functions", type=Path)
    operator.add_argument("--retire-containers", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_deployment(
                args.source,
                args.destination,
                instruction_candidates=args.instruction_candidates,
                adapter_candidates=args.adapter_candidates,
                retirement_manifest=args.retirement_manifest,
            )
        elif args.command == "apply":
            plan_path = _no_alias(args.plan, regular=True)
            result = apply_deployment(json.loads(plan_path.read_text(encoding="utf-8")))
        else:
            result = operator_apply(
                args.migration,
                fish_functions=args.fish_functions,
                retire_containers=args.retire_containers,
            )
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Deployment blocked: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
