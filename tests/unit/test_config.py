from pathlib import Path

import pytest

from config import AshConfig


ENV_KEYS = [
    "ASH_MODEL",
    "ASH_CONFIG_SCHEMA_VERSION",
    "ASH_TEMPERATURE",
    "ASH_MAX_CONTEXT_TOKENS",
    "ASH_MAX_COMPLETION_TOKENS",
    "ASH_MAX_TOOL_RESULT_TOKENS",
    "ASH_NOTIFICATION_METHOD",
    "ASH_NOTIFICATION_EVENTS",
    "ASH_NOTIFICATION_INCLUDE_PREVIEW",
    "ASH_SCREEN_READER_MODE",
    "ASH_SAFETY_TIER",
    "ASH_WORKSPACE_ROOT",
    "ASH_COMMAND_BLOCKLIST",
    "ASH_ALLOWED_WEB_DOMAINS",
    "ASH_ENABLE_SPRINT_PLANNING",
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
def clear_ash_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from cli import config as cli_config

    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    ash_dir = tmp_path / ".ash"
    monkeypatch.setattr(cli_config, "ASH_DIR", ash_dir)
    monkeypatch.setattr(cli_config, "ENV_FILE", ash_dir / ".env")
    monkeypatch.setattr(cli_config, "CONFIG_FILE", ash_dir / "ash.toml")
    original_toml_file = AshConfig.model_config.get("toml_file")
    original_env_file = AshConfig.model_config.get("env_file")
    AshConfig.model_config["toml_file"] = str(ash_dir / "ash.toml")
    AshConfig.model_config["env_file"] = str(ash_dir / ".env")
    try:
        yield
    finally:
        AshConfig.model_config["toml_file"] = original_toml_file
        AshConfig.model_config["env_file"] = original_env_file


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
    assert config.config_schema_version == 1
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


def test_future_config_schema_version_is_refused(tmp_path: Path) -> None:
    toml_path = tmp_path / "ash.toml"
    toml_path.write_text(
        "\n".join(
            [
                "config_schema_version = 999",
                'model = "ollama/llama3"',
            ]
        ),
        encoding="utf-8",
    )
    original_toml_file = AshConfig.model_config.get("toml_file")
    AshConfig.model_config["toml_file"] = str(toml_path)
    try:
        with pytest.raises(ValueError, match="newer than this Ash version supports"):
            AshConfig.load()
    finally:
        AshConfig.model_config["toml_file"] = original_toml_file


def test_config_loads_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AshConfig no longer requires an api_key field — keys come from env vars."""
    monkeypatch.chdir(tmp_path)

    # Should load without ValidationError (no required api_key field)
    config = AshConfig.load()
    assert config.provider == "anthropic"
    assert config.model == "anthropic/claude-sonnet-4-6"


def test_terminal_preferences_are_validated() -> None:
    config = AshConfig(
        input_mode="VI",
        tui_mode="INLINE",
        no_color=True,
        reduced_motion=True,
        show_token_meter=True,
        keybindings={"newline": ["c-o"], "open_editor": ["c-x c-e"]},
    )
    assert config.input_mode == "vi"
    assert config.tui_mode == "inline"
    assert config.no_color is True
    assert config.reduced_motion is True
    assert config.show_token_meter is True
    assert config.keybindings["newline"] == ["c-o"]


def test_screen_reader_mode_forces_linear_terminal_preferences() -> None:
    config = AshConfig(
        screen_reader_mode=True,
        tui_mode="viewport",
        no_color=False,
        reduced_motion=False,
        show_token_meter=True,
    )

    assert config.screen_reader_mode is True
    assert config.tui_mode == "inline"
    assert config.no_color is True
    assert config.reduced_motion is True
    assert config.show_token_meter is False


def test_terminal_notification_preferences_are_validated() -> None:
    config = AshConfig(
        notification_method="OSC9",
        notification_events=["TURN_COMPLETE", "turn_complete"],
        notification_include_preview=True,
    )

    assert config.notification_method == "osc9"
    assert config.notification_events == ["turn_complete"]
    assert config.notification_include_preview is True
    with pytest.raises(ValueError, match="notification_method"):
        AshConfig(notification_method="toast")
    with pytest.raises(ValueError, match="notification_events"):
        AshConfig(notification_events=["startup"])


def test_prompt_cache_preferences_are_validated() -> None:
    config = AshConfig(prompt_cache_enabled=False, prompt_cache_retention="EXTENDED")

    assert config.prompt_cache_enabled is False
    assert config.prompt_cache_retention == "extended"
    with pytest.raises(ValueError, match="prompt_cache_retention"):
        AshConfig(prompt_cache_retention="forever")


def test_allowed_web_domains_are_normalized_and_validated() -> None:
    config = AshConfig(
        allowed_web_domains=["Example.COM.", "*.Docs.Example", "example.com"]
    )
    assert config.allowed_web_domains == ["*.docs.example", "example.com"]

    with pytest.raises(ValueError, match="must be hostnames"):
        AshConfig(allowed_web_domains=["https://example.com"])
    with pytest.raises(ValueError, match="wildcards only"):
        AshConfig(allowed_web_domains=["api.*.example.com"])


def test_sprint_planning_can_be_enabled_from_config() -> None:
    config = AshConfig(enable_sprint_planning=True)
    assert config.enable_sprint_planning is True


def test_terminal_keybinding_collisions_are_rejected() -> None:
    with pytest.raises(ValueError, match="assigned to both"):
        AshConfig(
            keybindings={"newline": ["c-j"], "open_editor": ["C-J"]},
        )


def test_unknown_terminal_input_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="input_mode"):
        AshConfig(input_mode="modal")

    with pytest.raises(ValueError, match="tui_mode"):
        AshConfig(tui_mode="floating")


def test_backward_compat_ash_api_key_promoted_to_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASH_API_KEY should be promoted to ANTHROPIC_API_KEY for backward compat."""
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASH_API_KEY", "sk-ant-backcompat-key")

    AshConfig.load()

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


def test_model_requires_provider_prefix() -> None:
    with pytest.raises(ValueError, match="provider/model"):
        AshConfig(model="claude")
