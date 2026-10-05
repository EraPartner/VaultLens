#!/usr/bin/env python3
"""Fixture checks for shared provider selection without invoking either CLI."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm_provider as providers  # noqa: E402


class ProviderTests(unittest.TestCase):
    fixture: tempfile.TemporaryDirectory[str]
    config: Path

    def setUp(self) -> None:
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.config = Path(self.fixture.name) / "tools" / "llm.local.json"

    def write_config(self, data: object) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def resolve(
        self,
        *,
        cli: str | None = None,
        model: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> providers.Provider:
        return providers.resolve_provider(
            cli,
            model,
            path=self.config,
            environ={} if environ is None else environ,
        )

    def test_precedence_and_native_defaults(self) -> None:
        default = self.resolve()
        self.assertEqual(default.cli, "claude")
        self.assertEqual(default.model, "")
        for cli in providers.BACKENDS:
            self.assertEqual(self.resolve(cli=cli).model, "")
        self.write_config({"cli": "codex", "models": {"codex": "saved-codex"}})
        saved = self.resolve()
        self.assertEqual((saved.cli, saved.model), ("codex", "saved-codex"))
        env = {"VAULTLENS_LLM_CLI": "claude", "VAULTLENS_LLM_MODEL": "env-model"}
        from_env = self.resolve(environ=env)
        self.assertEqual((from_env.cli, from_env.model), ("claude", "env-model"))
        explicit = self.resolve(cli="codex", model="explicit-model", environ=env)
        self.assertEqual((explicit.cli, explicit.model), ("codex", "explicit-model"))
        self.write_config({"cli": "codex"})
        self.assertEqual(self.resolve().model, "")
        self.assertEqual(self.resolve(model="").model, "")

    def test_saved_switches_retain_each_providers_model(self) -> None:
        providers.select_provider("claude", "opus", path=self.config)
        providers.select_provider("codex", "chosen-codex", path=self.config)
        self.assertEqual(self.resolve().model, "chosen-codex")
        providers.select_provider("claude", path=self.config)
        self.assertEqual(self.resolve().model, "opus")
        providers.select_provider("codex", path=self.config)
        self.assertEqual(self.resolve().model, "chosen-codex")
        self.assertEqual(
            providers.load_config(self.config)["models"],
            {"claude": "opus", "codex": "chosen-codex"},
        )
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_environment_health_identity_and_normalization(self) -> None:
        actual = self.resolve(
            environ={
                "VAULTLENS_LLM_CLI": " CODEX ",
                "VAULTLENS_LLM_MODEL": " selected ",
                "VAULTLENS_LLM_HEALTH_HOST": " provider.example ",
                "VAULTLENS_LLM_IDENTITY": " local-plan ",
            }
        )
        self.assertEqual(
            actual,
            providers.Provider("codex", "selected", "provider.example", "local-plan"),
        )
        for key in (
            "VAULTLENS_LLM_CLI",
            "VAULTLENS_LLM_HEALTH_HOST",
            "VAULTLENS_LLM_IDENTITY",
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.resolve(environ={key: " "})

    def test_invalid_config_fails_even_with_explicit_provider(self) -> None:
        invalid_configs: tuple[object, ...] = (
            [],
            None,
            {"other": "codex"},
            {"cli": 1},
            {"cli": "other"},
            {"models": []},
            {"models": {"other": "x"}},
            {"models": {"claude": None}},
        )
        for invalid in invalid_configs:
            with self.subTest(config=invalid):
                self.write_config(invalid)
                with self.assertRaises(ValueError):
                    self.resolve(cli="claude", model="sonnet")
        self.config.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Cannot read provider configuration"):
            self.resolve(cli="codex")
        self.config.write_bytes(b"\xff")
        with self.assertRaises(ValueError):
            self.resolve()

    def test_unsupported_provider_error_names_its_source(self) -> None:
        with self.assertRaisesRegex(ValueError, r"'gemini' from the cli argument"):
            self.resolve(cli="gemini")
        with self.assertRaisesRegex(ValueError, r"'gemini' from VAULTLENS_LLM_CLI"):
            self.resolve(environ={"VAULTLENS_LLM_CLI": "gemini"})
        with self.assertRaisesRegex(ValueError, "saved provider preference"):
            providers.resolve_provider(
                path=self.config, environ={}, config={"cli": "gemini"}
            )

    def test_unreadable_config_fails_closed(self) -> None:
        with mock.patch.object(
            Path, "read_text", side_effect=PermissionError("fixture denial")
        ):
            with self.assertRaisesRegex(ValueError, "fixture denial"):
                self.resolve(cli="codex")

    def test_invalid_selection_preserves_saved_config(self) -> None:
        self.write_config({"cli": "claude"})
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            providers.select_provider("other", path=self.config)
        self.assertEqual(self.config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
