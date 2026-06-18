from pathlib import Path

import pytest
from pydantic import ValidationError

from config import AshConfig


ENV_KEYS = [
    "ASH_PROVIDER",
    "ASH_MODEL",
    "ASH_TEMPERATURE",
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
                'model = "gpt-4o"',
                "temperature = 0.7",
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
    assert config.model == "gpt-4o"
    assert config.temperature == 0.7
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
                'model = "claude-3-5-sonnet-20241022"',
                "temperature = 0.0",
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
    monkeypatch.setenv("ASH_MODEL", "llama3.1")
    monkeypatch.setenv("ASH_TEMPERATURE", "0.2")
    monkeypatch.setenv("ASH_MAX_CONTEXT_TOKENS", "32000")
    monkeypatch.setenv("ASH_MAX_COMPLETION_TOKENS", "2048")
    monkeypatch.setenv("ASH_MAX_TOOL_RESULT_TOKENS", "8000")
    monkeypatch.setenv("ASH_SAFETY_TIER", "auto_approve")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(env_workspace))
    monkeypatch.setenv("ASH_COMMAND_BLOCKLIST", '["git clean", "del"]')
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(env_db))

    config = AshConfig.load()

    assert config.provider == "ollama"
    assert config.model == "llama3.1"
    assert config.temperature == 0.2
    assert config.max_context_tokens == 32000
    assert config.max_completion_tokens == 2048
    assert config.max_tool_result_tokens == 8000
    assert config.safety_tier == "auto_approve"
    assert config.workspace_root == env_workspace
    assert config.command_blocklist == ["git clean", "del"]
    assert config.db_directory == env_db


def test_config_loads_without_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AshConfig no longer requires an api_key field — keys come from env vars."""
    monkeypatch.chdir(tmp_path)

    # Should load without ValidationError (no required api_key field)
    config = AshConfig.load()
    assert config.provider == "anthropic"
    assert config.model == "claude-3-7-sonnet-20250219"


def test_backward_compat_ash_api_key_promoted_to_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASH_API_KEY should be promoted to ANTHROPIC_API_KEY for backward compat."""
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASH_API_KEY", "sk-ant-backcompat-key")

    config = AshConfig.load()

    # The promotion happens in model_post_init — ANTHROPIC_API_KEY should now be set
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-backcompat-key"


def test_backward_compat_ash_model_name_promoted_to_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASH_MODEL_NAME should be promoted to ASH_MODEL for backward compat."""
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASH_MODEL_NAME", "claude-3-5-sonnet-20241022")

    config = AshConfig.load()

    # The promotion happens in model_post_init — ASH_MODEL should now be set
    assert os.environ.get("ASH_MODEL") == "claude-3-5-sonnet-20241022"
    assert config.model == "claude-3-5-sonnet-20241022"
