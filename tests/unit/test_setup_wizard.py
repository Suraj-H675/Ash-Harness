"""Tests for cli/setup.py — provider flows and credential saving."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to patch stdin / getpass
# ---------------------------------------------------------------------------


def _fake_input(values: list[str]) -> MagicMock:
    """Return a mock input() that cycles through a list of values."""
    it = iter(values)
    m = MagicMock()
    m.side_effect = lambda _: next(it)
    return m


class _FakeGetpass:
    """Fake getpass.getpass that returns a configured value."""

    def __init__(self, password: str) -> None:
        self._password = password

    def __call__(self, _: str = "") -> str:
        return self._password


# ---------------------------------------------------------------------------
# Provider flow tests
# ---------------------------------------------------------------------------


class TestHasProviderConfigured:
    """Tests for _has_provider_configured."""

    def test_true_when_api_key_set_for_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """provider/model + matching API key = configured."""
        mock_config = MagicMock(model="anthropic/claude-3-5-sonnet")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("HOME", "/tmp")

        with patch("cli.setup.load_config", return_value={}):
            from cli.setup import _has_provider_configured

            assert _has_provider_configured(mock_config) is True

    def test_true_when_api_key_in_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """API key in env vars + provider/model format = configured."""
        mock_config = MagicMock(model="anthropic/claude-3-5-sonnet")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("HOME", "/tmp")

        with patch("cli.setup.load_config", return_value={}):
            from cli.setup import _has_provider_configured

            assert _has_provider_configured(mock_config) is True

    def test_true_when_custom_providers_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom provider in config.custom_providers = configured."""
        mock_config = MagicMock(model="my-minimax/MiniMax-M2.7")
        mock_config.custom_providers = {
            "my-minimax": {"base_url": "https://api.minimax.io/v1"}
        }
        for key in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "GROQ_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HOME", "/tmp")

        with patch("cli.setup.load_config", return_value={}):
            from cli.setup import _has_provider_configured

            assert _has_provider_configured(mock_config) is True

    def test_false_when_completely_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No model, no API keys, no custom providers = not configured."""
        mock_config = MagicMock(model="")
        for key in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "GROQ_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HOME", "/tmp")

        with patch("cli.setup.load_config", return_value={}):
            from cli.setup import _has_provider_configured

            assert _has_provider_configured(mock_config) is False


class TestGetCurrentModel:
    """Tests for _get_current_model."""

    def test_extracts_model_name_from_provider_slash_model(self) -> None:
        """'anthropic/claude-3-5-sonnet' → 'claude-3-5-sonnet'."""
        from cli.setup import _get_current_model

        mock_config = MagicMock(model="anthropic/claude-3-5-sonnet")
        assert _get_current_model(mock_config) == "claude-3-5-sonnet"

    def test_returns_raw_value_if_no_slash(self) -> None:
        """Model without slash is returned as-is."""
        from cli.setup import _get_current_model

        mock_config = MagicMock(model="llama3")
        assert _get_current_model(mock_config) == "llama3"

    def test_empty_model(self) -> None:
        """Empty model returns empty string."""
        from cli.setup import _get_current_model

        mock_config = MagicMock(model="")
        assert _get_current_model(mock_config) == ""


class TestAnthropicFlow:
    """Tests for _flow_anthropic — verifies correct env values are saved."""

    def test_saves_anthropic_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_flow_anthropic should save the given API key and ASH_MODEL."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # Clean env so get_env_value returns None (no existing key)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # _prompt_api_key calls getpass once, _prompt_model_list calls input once
        monkeypatch.setattr("cli.setup.getpass.getpass", _FakeGetpass("sk-ant-test123"))
        monkeypatch.setattr("builtins.input", _fake_input(["1"]))  # model selection

        with patch("cli.setup._verify_anthropic"):
            with patch("cli.setup.save_env_value") as mock_save:
                from cli.setup import _flow_anthropic

                _flow_anthropic("")
                calls = {call[0][0]: call[0][1] for call in mock_save.call_args_list}
                assert "ANTHROPIC_API_KEY" in calls
                assert calls["ANTHROPIC_API_KEY"] == "sk-ant-test123"
                assert "ASH_MODEL" in calls
                assert calls["ASH_MODEL"].startswith("anthropic/")


class TestGroqFlow:
    """Tests for _flow_groq — verifies correct env values are saved."""

    def test_saves_groq_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_flow_groq should save the given API key and ASH_MODEL."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setattr("cli.setup.getpass.getpass", _FakeGetpass("gsk_groq_test"))
        monkeypatch.setattr(
            "builtins.input", _fake_input(["1"])
        )  # select model index 1

        with patch("cli.setup._verify_openai"):
            with patch("cli.setup.save_env_value") as mock_save:
                from cli.setup import _flow_groq

                _flow_groq("")
                calls = {call[0][0]: call[0][1] for call in mock_save.call_args_list}
                assert "GROQ_API_KEY" in calls
                assert calls["GROQ_API_KEY"] == "gsk_groq_test"
                assert "ASH_MODEL" in calls
                assert calls["ASH_MODEL"].startswith("groq/")


class TestOpenaiCompatibleFlow:
    """Tests for _flow_openai_compatible — verifies TOML save."""

    def test_saves_custom_provider_to_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom endpoint metadata is saved without embedding its API key."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # provider name, base URL, API key (optional), model name
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(
                [
                    "my-minimax",
                    "https://api.minimax.io/v1",
                    "sk-cp-test",
                    "MiniMax-M2.7",
                ]
            ),
        )

        with patch("cli.setup._probe_models", return_value=[]):
            with patch("cli.setup.save_config") as mock_save_config:
                from cli.setup import _flow_openai_compatible

                _flow_openai_compatible()
                mock_save_config.assert_called_once()
                call_args = mock_save_config.call_args[0][0]
                assert "custom_providers" in call_args
                assert "my-minimax" in call_args["custom_providers"]
                cp = call_args["custom_providers"]["my-minimax"]
                assert cp["base_url"] == "https://api.minimax.io/v1"
                assert cp["key_env"] == "ASH_PROVIDER_MY_MINIMAX_API_KEY"
                assert "api_key" not in cp
                env_text = (tmp_path / ".ash" / ".env").read_text()
                assert "ASH_PROVIDER_MY_MINIMAX_API_KEY=sk-cp-test\n" in env_text
                assert "ASH_MODEL=my-minimax/MiniMax-M2.7\n" in env_text


class TestProbeModels:
    """Tests for _probe_models and _probe_ollama_models."""

    def test_probe_models_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_probe_models returns model IDs on 200 response."""
        mock_response = MagicMock(
            status_code=200,
            json=lambda: {
                "object": "list",
                "data": [
                    {"id": "gpt-4o"},
                    {"id": "gpt-4o-mini"},
                ],
            },
        )
        monkeypatch.setenv("HOME", "/tmp")
        with patch("cli.setup.httpx.get", return_value=mock_response):
            from cli.setup import _probe_models

            result = _probe_models("https://api.openai.com/v1", "sk-test")
            assert result == ["gpt-4o", "gpt-4o-mini"]

    def test_probe_models_http_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_probe_models returns [] on HTTP error."""
        monkeypatch.setenv("HOME", "/tmp")
        with patch("cli.setup.httpx.get", side_effect=Exception("network error")):
            from cli.setup import _probe_models

            result = _probe_models("https://api.openai.com/v1", "sk-test")
            assert result == []

    def test_probe_ollama_models_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_probe_ollama_models returns model names on 200 response."""
        mock_response = MagicMock(
            status_code=200,
            json=lambda: {
                "models": [
                    {"name": "llama3"},
                    {"name": "qwen2.5-coder:7b"},
                ],
            },
        )
        monkeypatch.setenv("HOME", "/tmp")
        with patch("cli.setup.httpx.get", return_value=mock_response):
            from cli.setup import _probe_ollama_models

            result = _probe_ollama_models("http://localhost:11434")
            assert result == ["llama3", "qwen2.5-coder:7b"]


class TestCmdSetup:
    """Tests for cmd_setup entry point."""

    def test_cmd_setup_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cmd_setup should return 0 on success."""
        monkeypatch.setenv("HOME", "/tmp")
        monkeypatch.setenv("TERM", "xterm")  # ensure isatty true-ish

        mock_args = MagicMock(
            section="model",
            quick=False,
            non_interactive=False,
        )

        with patch("cli.setup.is_interactive_stdin", return_value=True):
            from cli.setup import SetupOutcome, cmd_setup

            with patch(
                "cli.setup.run_setup_wizard",
                return_value=SetupOutcome.SUCCESS,
            ):
                result = cmd_setup(mock_args)
                assert result == 0

    def test_cmd_setup_non_interactive_returns_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from cli.setup import cmd_setup

        args = MagicMock(section="model", quick=False, non_interactive=True)
        monkeypatch.setattr("cli.setup.is_interactive_stdin", lambda: False)

        assert cmd_setup(args) == 2
        assert "requires an interactive terminal" in capsys.readouterr().err


class TestSetupNavigation:
    def test_provider_selection_can_cancel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.setup import SetupOutcome, select_provider_and_model

        monkeypatch.setattr("builtins.input", _fake_input(["c"]))

        assert select_provider_and_model(MagicMock(model="")) == SetupOutcome.CANCELLED

    def test_blank_api_key_returns_to_provider_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.setup import SetupOutcome, select_provider_and_model

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr("builtins.input", _fake_input(["1", "c"]))
        monkeypatch.setattr("cli.setup.getpass.getpass", _FakeGetpass(""))

        assert select_provider_and_model(MagicMock(model="")) == SetupOutcome.CANCELLED
