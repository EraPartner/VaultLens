"""Versioned, provider-independent filesystem and privacy profiles.

Profiles are operator configuration, never permissions supplied by document text.
Resolved scopes contain concrete paths, not unconstrained workspace grants.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# Policy files are arbitrary JSON from disk; every field is validated at runtime in
# load_policy before use, so values are typed Any at this parse boundary only.
JsonObject = dict[str, Any]  # pyright: ignore[reportExplicitAny] - JSON parse boundary, validated in load_policy

REPORT_SUBTREE = Path("wiki/reports/agents")

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
FORBIDDEN_DIRS = {
    ".git",
    ".claude",
    ".codex",
    ".agents",
    ".obsidian",
    ".ssh",
    ".aws",
    ".kube",
    ".gnupg",
    "node_modules",
    "__pycache__",
}
SECRET_NAMES = (
    ".env",
    ".env.*",
    ".credentials.json*",
    ".claude.json*",
    ".pypirc",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "auth.json*",
    "credentials.*",
    "secrets.*",
    "token.json*",
    "tokens.json*",
    ".netrc",
    ".npmrc",
    ".git-credentials",
)
PROTECTED_NAMES = {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md", ".mcp.json"}
# Obsidian Templater executes code from these templates on the host, outside the
# sandbox, so no write grant may reach them.
PROTECTED_SUBTREES = ("wiki/_templates",)
_PROTECTED_FOLDED = {name.casefold() for name in PROTECTED_NAMES}


def protected_name(name: str) -> bool:
    """True for instruction files, in any letter case."""
    return name.casefold() in _PROTECTED_FOLDED

PROFILE_KEYS = {
    "extends",
    "description",
    "read",
    "write",
    "deny_read",
    "research_domains",
    "reports",
    "review_queue_metadata",
}


def _json(path: Path, *, optional: bool = False) -> JsonObject:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if optional:
            return {}
        raise ValueError(f"Missing access policy: {path}") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read access policy {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: access policy must be an object")
    # json.loads yields dict[Unknown, Unknown] after the isinstance narrowing; keys are
    # str by the JSON grammar.
    return cast(JsonObject, data)


def load_policy(root: Path = ROOT) -> JsonObject:
    """Local profiles replace matching tracked profiles; inheritance is explicit."""
    data = _json(root / "tools/access-profiles.json")
    local = _json(root / "tools/access.local.json", optional=True)
    for source in (data, local):
        if not source:
            continue
        if (
            set(source) - {"version", "profiles", "defaults"}
            or source.get("version") != 1
        ):
            raise ValueError("Access policy requires version 1, profiles and defaults")
        if not isinstance(source.get("profiles", {}), dict) or not isinstance(
            source.get("defaults", {}), dict
        ):
            raise ValueError("Access profiles and defaults must be objects")
    merged: JsonObject = {
        "version": 1,
        "profiles": {**data.get("profiles", {}), **local.get("profiles", {})},
        "defaults": {**data.get("defaults", {}), **local.get("defaults", {})},
    }
    for name, raw_profile in merged["profiles"].items():
        if (
            not isinstance(name, str)
            or not NAME.fullmatch(name)
            or not isinstance(raw_profile, dict)
            or set(cast(JsonObject, raw_profile)) - PROFILE_KEYS
        ):
            raise ValueError(f"Invalid access profile {name!r}")
        # Validated as an object above; narrowing leaves dict[Unknown, Unknown].
        profile = cast(JsonObject, raw_profile)
        for key in ("read", "write", "deny_read", "research_domains"):
            value = profile.get(key, [])
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item or "\0" in item
                for item in cast(list[object], value)
            ):
                raise ValueError(f"{name}: {key} must be a list of nonempty strings")
        if "review_queue_metadata" in profile and not isinstance(
            profile["review_queue_metadata"], bool
        ):
            raise ValueError(f"{name}: review_queue_metadata must be boolean")
        if "extends" in profile and not isinstance(profile["extends"], str):
            raise ValueError(f"{name}: extends must name one profile")
        _inherit(merged, name)
    for role, name in merged["defaults"].items():
        if (
            not isinstance(role, str)
            or not isinstance(name, str)
            or name not in merged["profiles"]
        ):
            raise ValueError(f"Invalid default access profile for {role!r}")
    return merged


def _inherit(
    policy: JsonObject, name: str, stack: tuple[str, ...] = ()
) -> JsonObject:
    if name in stack or name not in policy["profiles"]:
        raise ValueError(
            f"Unknown or cyclic access profile: {' -> '.join((*stack, name))}"
        )
    own = policy["profiles"][name]
    parent = (
        _inherit(policy, own["extends"], (*stack, name)) if own.get("extends") else {}
    )
    result = {**parent, **own}
    for key in ("read", "write", "deny_read", "research_domains"):
        result[key] = list(dict.fromkeys([*parent.get(key, []), *own.get(key, [])]))
    return result


def default_profile(role: str, root: Path = ROOT) -> str:
    try:
        return load_policy(root)["defaults"][role]
    except KeyError as exc:
        raise ValueError(f"No default access profile for {role!r}") from exc


def _relative(value: str, project: str | None) -> str:
    if "{project}" in value:
        if not project or not SLUG.fullmatch(project):
            raise ValueError("This access profile requires one valid project slug")
        value = value.replace("{project}", project)
    if "{" in value or "}" in value or "\\" in value:
        raise ValueError(f"Unsupported access path: {value!r}")
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ValueError(f"Access paths must be relative descendants: {value!r}")
    if path.parts[0] not in {"wiki", "raw", "projects"}:
        raise ValueError(
            f"Only wiki, raw and projects may contain note selections: {value!r}"
        )
    return value


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _research_domain(domain: str) -> bool:
    """A named public HTTPS host: no IP literal, local name, or TLD-wide wildcard."""
    labels = domain.casefold().removesuffix(":443").removeprefix("*.").split(".")
    return (
        len(labels) >= 2
        and all(_LABEL.fullmatch(label) for label in labels)
        # A numeric last label is an IPv4 literal or a decimal/octal IP alias.
        and any(character.isalpha() for character in labels[-1])
        and labels[-1] not in {"localhost", "local", "internal", "localdomain"}
        and "localhost" not in labels
    )


def _folded(path: Path) -> Path:
    """Compare paths as APFS does by default: letter case is not significant."""
    return Path(str(path).casefold())


def forbidden(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    parts = tuple(part.casefold() for part in relative.parts)
    name = path.name.casefold()
    return (
        any(part in FORBIDDEN_DIRS for part in parts)
        or parts[:2] == ("raw", "review-inbox")
        or any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_NAMES)
    )


def _inside(path: Path, root: Path) -> Path:
    """Reject aliases, including a symlink in a not-yet-created write path."""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Access selections cannot contain symbolic links: {path}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or forbidden(path, root):
        raise ValueError(f"Protected or external access path: {path}")
    if path.is_file() and path.stat().st_nlink > 1:
        raise ValueError(f"Access selections cannot contain hard links: {path}")
    return resolved


def _expand(root: Path, value: str, project: str | None) -> tuple[Path, ...]:
    relative = _relative(value, project)
    if any(character in relative for character in "*?["):
        found: list[Path] = []
        for path in sorted(root.glob(relative)):
            try:
                found.append(_inside(path, root))
            except ValueError:
                # A glob never grants an alias, protected or external match; one
                # bad match must not fail every launch. Explicit selections still raise.
                continue
        return tuple(found)
    return (_inside(root / relative, root),)


@dataclass(frozen=True)
class RunScope:
    root: Path
    name: str
    read_paths: tuple[Path, ...]
    write_paths: tuple[Path, ...]
    denied_paths: tuple[Path, ...]
    research_domains: tuple[str, ...]
    reports: Path
    review_queue_metadata: bool = False

    def readable(self, path: Path) -> bool:
        path = Path(path)
        try:
            concrete = _inside(path, self.root)
        except (ValueError, OSError):
            return False
        folded = _folded(concrete)
        if any(folded.is_relative_to(_folded(deny)) for deny in self.denied_paths):
            return False
        return any(
            concrete == grant or concrete.is_relative_to(grant)
            for grant in self.read_paths
        )

    def writable(self, path: Path) -> bool:
        path = Path(path)
        # readable() resolves the path but the grant comparison below is lexical,
        # so wiki/../raw/x would pass both checks. Reject traversal outright.
        if ".." in path.parts:
            return False
        if protected_name(path.name) or not self.readable(path):
            return False
        folded = _folded(path)
        if any(
            folded.is_relative_to(_folded(self.root / subtree))
            for subtree in PROTECTED_SUBTREES
        ):
            return False
        return any(
            path == grant or path.is_relative_to(grant) for grant in self.write_paths
        )

    def document_paths(self) -> list[Path]:
        result: set[Path] = set()
        for grant in self.read_paths:
            candidates: list[Path]
            if grant.is_file():
                candidates = [grant]
            elif grant.is_dir():
                # walk without following any directory or file symlinks
                import os

                candidates = []
                for directory, children, files in os.walk(grant, followlinks=False):
                    children[:] = [
                        child
                        for child in children
                        if child not in FORBIDDEN_DIRS
                        and not (Path(directory) / child).is_symlink()
                    ]
                    candidates.extend(
                        Path(directory) / name
                        for name in files
                        if name.endswith((".md", ".txt"))
                    )
            else:
                candidates = []
            result.update(
                path
                for path in candidates
                if self.readable(path) and not protected_name(path.name)
            )
        return sorted(result)

    def manifest(self) -> JsonObject:
        return {
            "version": 1,
            "profile": self.name,
            "root": str(self.root),
            "read": [str(path) for path in self.read_paths],
            "write": [str(path) for path in self.write_paths],
            "deny_read": [str(path) for path in self.denied_paths],
            "research_domains": list(self.research_domains),
            "reports": str(self.reports),
            "review_queue_metadata": self.review_queue_metadata,
        }

    def project_directories(self) -> list[Path]:
        """Discover selected metadata without listing an unapproved parent folder."""
        found: set[Path] = set()
        for grant in self.read_paths:
            relative = grant.relative_to(self.root)
            if relative.parts[0] != "projects":
                continue
            if len(relative.parts) >= 2:
                found.add(self.root / "projects" / relative.parts[1])
            elif grant.is_dir():
                found.update(path for path in grant.iterdir() if path.is_dir())
        return sorted(path for path in found if self.readable(path / "project.md"))


def resolve_scope(
    root: Path,
    name: str,
    *,
    project: str | None = None,
    read_paths: tuple[str, ...] = (),
    capability: str | None = None,
) -> RunScope:
    root = root.resolve()
    policy = load_policy(root)
    profile = _inherit(policy, name)
    reads = tuple(
        path
        for value in profile.get("read", [])
        for path in _expand(root, value, project)
    )
    if any(
        any(character in value for character in "*?[")
        for value in profile.get("write", [])
    ):
        raise ValueError("Wildcard write roots are forbidden")
    writes = tuple(
        path
        for value in profile.get("write", [])
        for path in _expand(root, value, project)
    )
    for path in writes:
        relative = path.relative_to(root)
        if relative.parts[0] == "raw" or any(
            character in str(relative) for character in "*?["
        ):
            raise ValueError("Source writes and wildcard write roots are forbidden")
        if relative.parts[0] == "projects" and (
            len(relative.parts) != 2 or not project or relative.parts[1] != project
        ):
            raise ValueError("Project writes require exactly the selected project")
    if capability in {"read", "read-shell"} and writes:
        raise ValueError("Reader roles cannot select writer profiles")
    if capability == "wiki-write" and (
        not writes or any(not path.is_relative_to(root / "wiki") for path in writes)
    ):
        raise ValueError("Wiki writer roles require wiki-only write roots")
    if capability == "project-write" and (
        not writes or any(path != root / "projects" / str(project) for path in writes)
    ):
        raise ValueError("Project writer roles require exactly one project write root")
    if any(
        any(character in value for character in "*?[")
        for value in profile.get("deny_read", [])
    ):
        raise ValueError(
            "Denied paths must name exact files or subtrees, never snapshot globs"
        )
    denies = tuple(
        path
        for value in profile.get("deny_read", [])
        for path in _expand(root, value, project)
    )
    extra: list[Path] = []
    for value in read_paths:
        path = Path(value)
        if path.is_absolute():
            try:
                value = str(path.relative_to(root))
            except ValueError as exc:
                raise ValueError(
                    "Explicit note reads must stay within this vault"
                ) from exc
        extra.extend(_expand(root, value, project))
    domains = tuple(profile.get("research_domains", []))
    for domain in domains:
        if not isinstance(domain, str) or not _research_domain(domain):
            raise ValueError(f"Research must name explicit HTTPS domains: {domain!r}")
    report_value = profile.get("reports", "wiki/reports/agents")
    if not isinstance(report_value, str) or any(c in report_value for c in "*?["):
        raise ValueError("Reports require one exact directory")
    reports = _inside(root / _relative(report_value, project), root)
    if not reports.is_relative_to(root / REPORT_SUBTREE):
        raise ValueError(
            "Reports must stay in the reserved wiki/reports/agents subtree"
        )
    scope = RunScope(
        root,
        name,
        tuple(dict.fromkeys((*reads, *writes, *extra))),
        writes,
        tuple(dict.fromkeys((root / REPORT_SUBTREE, *denies))),
        domains,
        reports,
        profile.get("review_queue_metadata", False),
    )
    for path in (*writes, *extra):
        if not scope.readable(path):
            raise ValueError(f"Selection conflicts with a denied path: {path}")
    return scope
