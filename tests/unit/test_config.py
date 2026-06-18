from pathlib import Path

import pytest

from config import AshConfig


ENV_KEYS = [
    "ASH_MODEL",
    "ASH_TEMPERATURE",
    "ASH_MAX_CONTEXT_TOKENS",
    "ASH_MAX_COMPLETION_TOKENS",
    "ASH_MAX_TOOL_RESULT_TOKENS",
    "ASH_SAFETY_TIER",
    "ASH_WORKSPACE_ROOT",
    "ASH_COMMAND_BLOCKLIST",
    "ASH_DB_DIRECTORY",
    # Provider API keys — clear these so they don't pollute tests
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_BASE",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_BASE",
    "GROQ_API_KEY",
    "GROQ_API_BASE",
    "OLLAMA_API_BASE",
    # Backward compat
    "ASH_API_KEY",
    "ASH_MODEL_NAME",
    "ASH_PROVIDER",
]


@pytest.fixture(autouse=True)
def clear_ash_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Also clear ~/.ash/ so tests don't bleed state between runs
    ash_dir = Path.home() / ".ash"
    for f in (ash_dir / ".env", ash_dir / "ash.toml"):
        if f.exists():
            f.unlink()


def test_config_loads_all_fields_from_ash_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    db_directory = tmp_path / "db"
    workspace.mkdir()

    toml_path = tmp_path / "ash.toml"
    toml_path.write_text(
        "\n".join(
            [
                'model = "openai/gpt-4o"',
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
    # Patch toml_file so AshConfig.load() reads from our tmp_path file.
    # The class-level toml_file is ~/.ash/ash.toml (absolute), so chdir has no effect.
    original_toml_file = AshConfig.model_config.get("toml_file")
    AshConfig.model_config["toml_file"] = str(toml_path)
    try:
        config = AshConfig.load()
    finally:
        AshConfig.model_config["toml_file"] = original_toml_file

    assert config.provider == "openai"
    assert config.model == "openai/gpt-4o"
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
                'model = "anthropic/claude-3-5-sonnet-20241022"',
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

    # ASH_MODEL without '/' gets ASH_PROVIDER prepended
    monkeypatch.setenv("ASH_MODEL", "llama3.1")
    monkeypatch.setenv("ASH_PROVIDER", "ollama")
    monkeypatch.setenv("ASH_TEMPERATURE", "0.2")
    monkeypatch.setenv("ASH_MAX_CONTEXT_TOKENS", "32000")
    monkeypatch.setenv("ASH_MAX_COMPLETION_TOKENS", "2048")
    monkeypatch.setenv("ASH_MAX_TOOL_RESULT_TOKENS", "8000")
    monkeypatch.setenv("ASH_SAFETY_TIER", "auto_approve")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(env_workspace))
    monkeypatch.setenv("ASH_COMMAND_BLOCKLIST", '["git clean", "del"]')
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(env_db))

    config = AshConfig.load()

    # ASH_MODEL="llama3.1" + ASH_PROVIDER="ollama" → "ollama/llama3.1"
    assert config.provider == "ollama"
    assert config.model == "ollama/llama3.1"
    assert config.temperature == 0.2
    assert config.max_context_tokens == 32000
    assert config.max_completion_tokens == 2048
    assert config.max_tool_result_tokens == 8000
    assert config.safety_tier == "auto_approve"
    assert config.workspace_root == env_workspace
    assert config.command_blocklist == ["git clean", "del"]
    assert config.db_directory == env_db


def test_config_loads_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AshConfig no longer requires an api_key field — keys come from env vars."""
    monkeypatch.chdir(tmp_path)

    # Should load without ValidationError (no required api_key field)
    config = AshConfig.load()
    assert config.provider == "anthropic"
    assert config.model == "anthropic/claude-3-7-sonnet-20250219"


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
    """ASH_MODEL_NAME should be promoted to ASH_MODEL for backward compat.
    Without ASH_PROVIDER set, defaults to anthropic."""
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASH_MODEL_NAME", "claude-3-5-sonnet-20241022")

    config = AshConfig.load()

    # Promoted to ASH_MODEL with anthropic as default provider
    assert os.environ.get("ASH_MODEL") == "anthropic/claude-3-5-sonnet-20241022"
    assert config.model == "anthropic/claude-3-5-sonnet-20241022"
    assert config.provider == "anthropic"


def test_backward_compat_ash_provider_prepends_ash_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASH_MODEL without '/' gets ASH_PROVIDER prepended."""
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASH_PROVIDER", "groq")
    monkeypatch.setenv("ASH_MODEL", "llama-3.3-70b-versatile")

    config = AshConfig.load()

    # Prepended to form provider/model string
    assert os.environ.get("ASH_MODEL") == "groq/llama-3.3-70b-versatile"
    assert config.model == "groq/llama-3.3-70b-versatile"
    assert config.provider == "groq"


def test_ash_model_provider_format_used_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASH_MODEL already containing '/' is used as-is, no modification."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASH_MODEL", "anthropic/claude-3-7-sonnet-20250219")

    config = AshConfig.load()

    assert config.model == "anthropic/claude-3-7-sonnet-20250219"
    assert config.provider == "anthropic"
