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

    def test_provider_specific_current_model_does_not_cross_providers(self) -> None:
        from cli.setup import _get_current_model_for_provider

        config = MagicMock(model="anthropic/claude-example")
        assert _get_current_model_for_provider(config, "anthropic") == "claude-example"
        assert _get_current_model_for_provider(config, "openai") == ""


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
        monkeypatch.setattr(
            "builtins.input", _fake_input(["", "1"])
        )  # default base URL, model selection

        from cli.setup import ModelProbe, _flow_anthropic

        with (
            patch(
                "cli.setup._probe_anthropic_models_detailed",
                return_value=ModelProbe(models=("claude-test",)),
            ),
            patch("cli.setup.save_env_values") as mock_save,
        ):
            _flow_anthropic("")
            calls = mock_save.call_args.args[0]
            assert "ANTHROPIC_API_KEY" in calls
            assert calls["ANTHROPIC_API_KEY"] == "sk-ant-test123"
            assert "ASH_MODEL" in calls
            assert calls["ASH_MODEL"].startswith("anthropic/")

    def test_cancelled_model_selection_does_not_save_partial_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.setup import ModelProbe, SetupCancelled, _flow_anthropic

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr("cli.setup.getpass.getpass", _FakeGetpass("sk-new"))
        monkeypatch.setattr("builtins.input", _fake_input(["", "c"]))

        with (
            patch(
                "cli.setup._probe_anthropic_models_detailed",
                return_value=ModelProbe(models=("model",)),
            ),
            patch("cli.setup.save_env_values") as save,
            pytest.raises(SetupCancelled),
        ):
            _flow_anthropic("")
        save.assert_not_called()


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

        from cli.setup import ModelProbe, _flow_groq

        with (
            patch(
                "cli.setup._probe_models_detailed",
                return_value=ModelProbe(models=("groq-test",)),
            ),
            patch("cli.setup.save_env_values") as mock_save,
        ):
            _flow_groq("")
            calls = mock_save.call_args.args[0]
            assert "GROQ_API_KEY" in calls
            assert calls["GROQ_API_KEY"] == "gsk_groq_test"
            assert "ASH_MODEL" in calls
            assert calls["ASH_MODEL"].startswith("groq/")


class TestOpenAIFlow:
    def test_custom_base_url_is_used_for_discovery_and_saved_atomically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.setup import ModelProbe, _flow_openai

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)
        monkeypatch.setattr("cli.setup.getpass.getpass", _FakeGetpass("sk-openai"))
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(["https://gateway.example/v1/", "1"]),
        )

        with (
            patch(
                "cli.setup._probe_models_detailed",
                return_value=ModelProbe(models=("gateway-model",)),
            ) as probe,
            patch("cli.setup.save_env_values") as save,
        ):
            _flow_openai("")

        probe.assert_called_once_with("https://gateway.example/v1", "sk-openai")
        assert save.call_args.args[0] == {
            "OPENAI_API_KEY": "sk-openai",
            "OPENAI_API_BASE": "https://gateway.example/v1",
            "ASH_MODEL": "openai/gateway-model",
        }


class TestDiscoveryRecovery:
    def test_probe_can_retry_then_verify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.setup import ModelProbe, _discover_models

        monkeypatch.setattr("builtins.input", _fake_input(["r"]))
        probe = MagicMock(
            side_effect=[
                ModelProbe(error="HTTP 503"),
                ModelProbe(models=("model-a",)),
            ]
        )

        assert _discover_models("Provider", probe) == (["model-a"], True)
        assert probe.call_count == 2

    def test_probe_can_continue_explicitly_without_verification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.setup import ModelProbe, _discover_models

        monkeypatch.setattr("builtins.input", _fake_input(["s"]))

        assert _discover_models(
            "Provider",
            lambda: ModelProbe(error="offline"),
            fallback=["manual-model"],
        ) == (["manual-model"], False)


class TestOpenaiCompatibleFlow:
    """Tests for _flow_openai_compatible — verifies TOML save."""

    def test_saves_custom_provider_to_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom endpoint metadata is saved without embedding its API key."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # provider name, base URL, model name
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(
                [
                    "my-minimax",
                    "https://api.minimax.io/v1",
                    "MiniMax-M2.7",
                ]
            ),
        )
        monkeypatch.setattr("cli.setup.getpass.getpass", _FakeGetpass("sk-cp-test"))

        from cli.setup import ModelProbe

        with patch(
            "cli.setup._probe_models_detailed",
            return_value=ModelProbe(models=("MiniMax-M2.7",)),
        ):
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

    def test_probe_error_redacts_echoed_api_key(self) -> None:
        from cli.setup import _probe_models_detailed

        response = MagicMock(
            status_code=401,
            text="invalid key sk-secret-value",
        )
        with patch("cli.setup.httpx.get", return_value=response):
            result = _probe_models_detailed(
                "https://api.example.test/v1",
                "sk-secret-value",
            )

        assert result.models == ()
        assert "sk-secret-value" not in (result.error or "")
        assert "[REDACTED]" in (result.error or "")

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


class TestSetupValidation:
    @pytest.mark.parametrize(
        "value",
        [
            "localhost:11434",
            "ftp://example.com",
            "https://user:secret@example.com/v1",
            "https://example.com/v1?token=secret",
        ],
    )
    def test_base_url_rejects_unsafe_or_ambiguous_values(self, value: str) -> None:
        from cli.setup import _validate_base_url

        with pytest.raises(ValueError):
            _validate_base_url(value)

    def test_base_url_normalizes_trailing_slash(self) -> None:
        from cli.setup import _validate_base_url

        assert _validate_base_url("http://localhost:11434/") == "http://localhost:11434"


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

        with patch("cli.setup._has_provider_configured", return_value=False):
            assert cmd_setup(args) == 2
        assert "requires an interactive terminal" in capsys.readouterr().err

    def test_cmd_setup_non_interactive_accepts_existing_configuration(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from cli.setup import cmd_setup

        args = MagicMock(section="model", quick=False, non_interactive=True)
        monkeypatch.setattr("cli.setup.is_interactive_stdin", lambda: False)

        with patch("cli.setup._has_provider_configured", return_value=True):
            assert cmd_setup(args) == 0
        output = capsys.readouterr().out
        assert "Ash is configured for" in output
        assert "doctor --connect" in output


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


class TestLegacyConfigMigration:
    @pytest.fixture(autouse=True)
    def _clear_destination_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in (
            "ASH_MODEL",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "GROQ_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)

    @staticmethod
    def _configure_paths(tmp_path: Path) -> None:
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / "home" / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"

    def test_migrates_complete_historical_config_and_records_backup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cli import config as cli_config
        from cli.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        legacy.write_text(
            '\n'.join(
                [
                    'provider = "openai"',
                    'model_name = "gpt-test"',
                    'api_key = "legacy-secret"',
                    'temperature = 0.3',
                    'max_context_tokens = 64000',
                    'max_completion_tokens = 2048',
                    'max_tool_result_tokens = 9000',
                    'safety_tier = "dry_run"',
                    'workspace_root = "."',
                    'command_blocklist = ["danger"]',
                    'db_directory = ".ash-db"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input([""]))

        _migrate_old_ash_toml()

        env = cli_config.load_env()
        assert env["OPENAI_API_KEY"] == "legacy-secret"
        assert env["ASH_MODEL"] == "openai/gpt-test"
        assert "ANTHROPIC_API_KEY" not in env
        user = cli_config.load_config(strict=True)
        assert user["config_schema_version"] == 1
        assert user["temperature"] == 0.3
        assert user["max_context_tokens"] == 64000
        assert user["max_completion_tokens"] == 2048
        assert user["max_tool_result_tokens"] == 9000
        assert user["safety_tier"] == "dry_run"
        assert user["workspace_root"] == str(project.resolve())
        assert user["db_directory"] == str((project / ".ash-db").resolve())
        assert user["command_blocklist"] == ["danger"]
        backups = list((cli_config.ASH_DIR / "backups").glob("legacy-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == legacy.read_bytes()
        assert cli_config.is_config_migration_recorded(legacy) is True

        repeated_prompt = MagicMock(side_effect=AssertionError("prompted twice"))
        monkeypatch.setattr("builtins.input", repeated_prompt)
        _migrate_old_ash_toml()
        repeated_prompt.assert_not_called()

    def test_preserves_existing_destinations_and_backs_up_user_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cli import config as cli_config
        from cli.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        (project / "ash.toml").write_text(
            'provider = "anthropic"\n'
            'model_name = "legacy-model"\n'
            'api_key = "legacy-key"\n'
            'temperature = 0.8\n'
            'max_context_tokens = 32000\n',
            encoding="utf-8",
        )
        cli_config.save_config({"temperature": 0.1})
        cli_config.save_env_values(
            {
                "ANTHROPIC_API_KEY": "new-key",
                "ASH_MODEL": "anthropic/new-model",
            }
        )
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))

        _migrate_old_ash_toml()

        assert cli_config.load_env()["ANTHROPIC_API_KEY"] == "new-key"
        assert cli_config.load_env()["ASH_MODEL"] == "anthropic/new-model"
        user = cli_config.load_config(strict=True)
        assert user["temperature"] == 0.1
        assert user["max_context_tokens"] == 32000
        destination_backups = list(
            (cli_config.ASH_DIR / "backups").glob(
                "user-ash.toml-pre-migration.*.bak"
            )
        )
        assert len(destination_backups) == 1
        assert "temperature = 0.1" in destination_backups[0].read_text()
        output = capsys.readouterr().out
        assert "ANTHROPIC_API_KEY" in output
        assert "ASH_MODEL" in output
        assert "temperature" in output

    def test_ignores_unrelated_toml_and_placeholder_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cli import config as cli_config
        from cli.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        legacy.write_text('name = "another-tool"\n', encoding="utf-8")
        monkeypatch.chdir(project)
        prompt = MagicMock(side_effect=AssertionError("unexpected prompt"))
        monkeypatch.setattr("builtins.input", prompt)

        _migrate_old_ash_toml()
        prompt.assert_not_called()
        assert not cli_config.ENV_FILE.exists()

        legacy.write_text(
            'provider = "anthropic"\n'
            'model_name = "test"\n'
            'api_key = "replace-with-your-api-key"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))
        _migrate_old_ash_toml()
        assert "ANTHROPIC_API_KEY" not in cli_config.load_env()

    def test_refuses_to_overwrite_malformed_user_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cli import config as cli_config
        from cli.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        legacy.write_text('model_name = "legacy"\n', encoding="utf-8")
        cli_config.ensure_ash_dir()
        cli_config.CONFIG_FILE.write_text("invalid = [", encoding="utf-8")
        original = cli_config.CONFIG_FILE.read_bytes()
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))

        with pytest.raises(Exception):
            _migrate_old_ash_toml()

        assert cli_config.CONFIG_FILE.read_bytes() == original
        assert cli_config.is_config_migration_recorded(legacy) is False
