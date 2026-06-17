from pathlib import Path

import pytest
from pydantic import ValidationError

from config import AshConfig


ENV_KEYS = [
    "ASH_PROVIDER",
    "ASH_MODEL_NAME",
    "ASH_TEMPERATURE",
    "ASH_API_KEY",
    "ASH_MAX_CONTEXT_TOKENS",
    "ASH_MAX_COMPLETION_TOKENS",
    "ASH_MAX_TOOL_RESULT_TOKENS",
    "ASH_SAFETY_TIER",
    "ASH_WORKSPACE_ROOT",
    "ASH_COMMAND_BLOCKLIST",
    "ASH_DB_DIRECTORY",
]


@pytest.fixture(autouse=True)
def clear_ash_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_config_loads_all_fields_from_ash_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    db_directory = tmp_path / "db"
    workspace.mkdir()

    (tmp_path / "ash.toml").write_text(
        "\n".join(
            [
                'provider = "openai"',
                'model_name = "gpt-4.1"',
                "temperature = 0.7",
                'api_key = "toml-key"',
                "max_context_tokens = 64000",
                "max_completion_tokens = 3000",
                "max_tool_result_tokens = 12000",
                'safety_tier = "dry_run"',
                f'workspace_root = "{workspace}"',
                'command_blocklist = ["rm -rf", "curl"]',
                f'db_directory = "{db_directory}"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    config = AshConfig.load()

    assert config.provider == "openai"
    assert config.model_name == "gpt-4.1"
    assert config.temperature == 0.7
    assert config.api_key == "toml-key"
    assert config.max_context_tokens == 64000
    assert config.max_completion_tokens == 3000
    assert config.max_tool_result_tokens == 12000
    assert config.safety_tier == "dry_run"
    assert config.workspace_root == workspace
    assert config.command_blocklist == ["rm -rf", "curl"]
    assert config.db_directory == db_directory


def test_environment_variables_override_ash_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml_workspace = tmp_path / "toml-workspace"
    env_workspace = tmp_path / "env-workspace"
    toml_db = tmp_path / "toml-db"
    env_db = tmp_path / "env-db"

    (tmp_path / "ash.toml").write_text(
        "\n".join(
            [
                'provider = "anthropic"',
                'model_name = "claude-3-5-sonnet-20241022"',
                "temperature = 0.0",
                'api_key = "toml-key"',
                "max_context_tokens = 128000",
                "max_completion_tokens = 4000",
                "max_tool_result_tokens = 20000",
                'safety_tier = "interactive"',
                f'workspace_root = "{toml_workspace}"',
                'command_blocklist = ["format", "rm -rf", "Remove-Item"]',
                f'db_directory = "{toml_db}"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("ASH_PROVIDER", "ollama")
    monkeypatch.setenv("ASH_MODEL_NAME", "llama3.1")
    monkeypatch.setenv("ASH_TEMPERATURE", "0.2")
    monkeypatch.setenv("ASH_API_KEY", "env-key")
    monkeypatch.setenv("ASH_MAX_CONTEXT_TOKENS", "32000")
    monkeypatch.setenv("ASH_MAX_COMPLETION_TOKENS", "2048")
    monkeypatch.setenv("ASH_MAX_TOOL_RESULT_TOKENS", "8000")
    monkeypatch.setenv("ASH_SAFETY_TIER", "auto_approve")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(env_workspace))
    monkeypatch.setenv("ASH_COMMAND_BLOCKLIST", '["git clean", "del"]')
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(env_db))

    config = AshConfig.load()

    assert config.provider == "ollama"
    assert config.model_name == "llama3.1"
    assert config.temperature == 0.2
    assert config.api_key == "env-key"
    assert config.max_context_tokens == 32000
    assert config.max_completion_tokens == 2048
    assert config.max_tool_result_tokens == 8000
    assert config.safety_tier == "auto_approve"
    assert config.workspace_root == env_workspace
    assert config.command_blocklist == ["git clean", "del"]
    assert config.db_directory == env_db


def test_api_key_is_required_without_env_or_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError):
        AshConfig.load()
