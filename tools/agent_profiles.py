"""Shared canonical role metadata and provider model selection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from llm_provider import MODEL_PROFILES, ROOT, LlmConfig, Provider, resolve_provider

EFFORTS = {"low", "medium", "high", "xhigh"}

AGENT_FILES = {
    "quality": "wiki-quality-reviewer.md",
    "verify": "wiki-source-verifier.md",
    "ingest": "wiki-ingest.md",
    "contradict": "wiki-contradiction-detector.md",
    "search": "wiki-search.md",
    "enhance": "wiki-enhancer.md",
    "cos": "wiki-cos.md",
    "challenge": "wiki-challenge.md",
    "connect": "wiki-connect.md",
    "emerge": "wiki-emerge.md",
    "discover": "wiki-idea-discovery.md",
    "project-run": "wiki-project-runner.md",
}


@dataclass(frozen=True)
class Role:
    path: Path
    name: str
    description: str
    permission_profile: str
    model_profile: str
    reasoning_effort: str


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"{path}: missing YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{path}: unterminated YAML frontmatter") from exc

    result: dict[str, str] = {}
    i = 1
    while i < end:
        line = lines[i]
        if not line or line.startswith(" ") or ":" not in line:
            i += 1
            continue
        key, raw = line.split(":", 1)
        value = raw.strip()
        if value in {">", ">-", "|", "|-"}:
            block: list[str] = []
            i += 1
            while i < end and (not lines[i] or lines[i].startswith(" ")):
                block.append(lines[i].strip())
                i += 1
            result[key] = " ".join(part for part in block if part)
            continue
        result[key] = value.strip("\"'")
        i += 1
    return result


def load_role(path: Path) -> Role:
    data = _frontmatter(path)
    role = Role(
        path,
        data.get("name", ""),
        data.get("description", ""),
        data.get("permission_profile", ""),
        data.get("model_profile", ""),
        data.get("reasoning_effort", ""),
    )
    if not role.name or not role.description:
        raise ValueError(f"{path}: name and description are required")
    if role.model_profile not in MODEL_PROFILES:
        raise ValueError(f"{path}: unknown model_profile {role.model_profile!r}")
    if role.reasoning_effort not in EFFORTS:
        raise ValueError(f"{path}: unknown reasoning_effort {role.reasoning_effort!r}")
    return role


def resolve_role_settings(
    agent: str,
    cli: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    *,
    root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    config: LlmConfig | None = None,
    profile_models: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[Provider, str]:
    if agent not in AGENT_FILES:
        raise ValueError(f"Unknown wiki agent: {agent}")
    role = load_role(root / ".agents" / "roles" / AGENT_FILES[agent])
    provider = resolve_provider(
        cli,
        model,
        path=root / "tools" / "llm.local.json",
        environ=environ,
        profile=role.model_profile,
        config=config,
        profile_models=profile_models,
    )
    selected_effort = role.reasoning_effort if effort is None else effort
    if selected_effort not in EFFORTS:
        raise ValueError(f"Unknown reasoning effort: {selected_effort}")
    return provider, selected_effort
