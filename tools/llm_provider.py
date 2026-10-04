#!/usr/bin/env python3
"""One provider selection for Brain launchers, wiki agents, and scheduled jobs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


if sys.version_info < (3, 11):
    sys.stderr.write(
        "VaultLens requires Python 3.11 or newer. Use Homebrew Python or set BRAIN_PYTHON for host wrappers.\n"
    )
    raise SystemExit(2)


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "tools" / "llm.local.json"
PROFILE_PATH = ROOT / "tools" / "model-profiles.json"
MODEL_PROFILES = {"standard", "deep"}
DEFAULT_CLI = "claude"
BACKENDS = {
    "claude": {"model": "", "health_host": "api.anthropic.com"},
    "codex": {"model": "", "health_host": "chatgpt.com"},
}


@dataclass(frozen=True)
class Provider:
    cli: str
    model: str
    health_host: str
    identity: str


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read provider configuration {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"cli", "models", "profiles"}:
        raise ValueError(
            f"{path}: expected an object with only cli, models and profiles"
        )
    if "cli" in data and (
        not isinstance(data["cli"], str) or data["cli"] not in BACKENDS
    ):
        raise ValueError(f"{path}: cli must be claude or codex")
    models = data.get("models", {})
    if not isinstance(models, dict) or set(models) - BACKENDS.keys():
        raise ValueError(f"{path}: models must map claude or codex to model names")
    if any(not isinstance(model, str) for model in models.values()):
        raise ValueError(f"{path}: model names must be strings")
    _validate_profiles(data.get("profiles", {}), path)
    return data


def _validate_profiles(data: object, path: Path) -> None:
    if not isinstance(data, dict) or set(data) - BACKENDS.keys():
        raise ValueError(f"{path}: profiles must map claude or codex to role models")
    for profiles in data.values():
        if (
            not isinstance(profiles, dict)
            or set(profiles) - MODEL_PROFILES
            or any(not isinstance(model, str) for model in profiles.values())
        ):
            raise ValueError(
                f"{path}: role models must map standard or deep to strings"
            )


def load_profile_models(path: Path = PROFILE_PATH) -> dict:
    """Load the tracked provider mappings; malformed policy fails closed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read model profiles {path}: {exc}") from exc
    _validate_profiles(data, path)
    return data


def resolve_provider(
    cli: str | None = None,
    model: str | None = None,
    *,
    path: Path = CONFIG_PATH,
    environ: Mapping[str, str] | None = None,
    profile: str | None = None,
    config: dict | None = None,
    profile_models: Mapping | None = None,
) -> Provider:
    """Explicit arguments override environment, local configuration, then defaults."""
    env = os.environ if environ is None else environ
    config = load_config(path) if config is None else config
    selected = (
        cli
        if cli is not None
        else env.get("VAULTLENS_LLM_CLI", config.get("cli", DEFAULT_CLI))
    )
    selected = selected.strip().lower()
    if selected not in BACKENDS:
        raise ValueError("VAULTLENS_LLM_CLI must be claude or codex")
    defaults = BACKENDS[selected]
    default_model = defaults["model"]
    if profile is not None:
        if profile not in MODEL_PROFILES:
            raise ValueError(f"Unknown model profile: {profile}")
        mappings = (
            load_profile_models(path.parent / "model-profiles.json")
            if profile_models is None
            else profile_models
        )
        default_model = (
            config.get("profiles", {})
            .get(selected, {})
            .get(profile, mappings.get(selected, {}).get(profile, default_model))
        )
    selected_model = (
        model
        if model is not None
        else env.get(
            "VAULTLENS_LLM_MODEL",
            config.get("models", {}).get(selected, default_model),
        )
    )
    host = env.get("VAULTLENS_LLM_HEALTH_HOST", defaults["health_host"]).strip()
    identity = env.get("VAULTLENS_LLM_IDENTITY", f"{selected}-plan").strip()
    if not host or not identity:
        raise ValueError("Provider health host and identity must not be empty")
    return Provider(selected, selected_model.strip(), host, identity)


def select_provider(
    cli: str, model: str | None = None, *, path: Path = CONFIG_PATH
) -> None:
    """Save a local preference without carrying a model across providers."""
    if cli not in BACKENDS:
        raise ValueError("Provider must be claude or codex")
    config = load_config(path)
    config["cli"] = cli
    if model is not None:
        config.setdefault("models", {})[cli] = model.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(config, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="Show effective provider configuration")
    show.add_argument("--cli", choices=BACKENDS)
    show.add_argument("--model")
    select = commands.add_parser(
        "select", help="Save the vault's local default provider"
    )
    select.add_argument("cli", choices=BACKENDS)
    select.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        if args.command == "select":
            select_provider(args.cli, args.model)
            print(f"Saved {args.cli} in {CONFIG_PATH}")
            if "VAULTLENS_LLM_CLI" in os.environ:
                print("VAULTLENS_LLM_CLI still overrides this local preference.")
        else:
            print(json.dumps(asdict(resolve_provider(args.cli, args.model)), indent=2))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
