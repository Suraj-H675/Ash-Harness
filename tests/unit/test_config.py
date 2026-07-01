import os
from pathlib import Path

import pytest

from config import AshConfig, discover_workspace_root, project_config_paths


ENV_KEYS = [
    "ASH_MODEL",
    "ASH_CONFIG_SCHEMA_VERSION",
    "ASH_TEMPERATURE",
    "ASH_MAX_CONTEXT_TOKENS",
    "ASH_MAX_COMPLETION_TOKENS",
    "ASH_MAX_TOOL_RESULT_TOKENS",
    "ASH_MAX_ATTACHMENT_TOKENS",
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


def test_attachment_budget_defaults_to_quarter_of_usable_context() -> None:
    small = AshConfig(
        model="ollama/test",
        max_context_tokens=8000,
        max_completion_tokens=2000,
    )
    large = AshConfig(
        model="ollama/test",
        max_context_tokens=128000,
        max_completion_tokens=4000,
    )
    explicit = AshConfig(
        model="ollama/test",
        max_context_tokens=8000,
        max_completion_tokens=2000,
        max_attachment_tokens=777,
    )

    assert small.attachment_token_budget == 1500
    assert large.attachment_token_budget == 16000
    assert explicit.attachment_token_budget == 777


def test_context_reserves_reject_impossible_attachment_budget() -> None:
    with pytest.raises(ValueError, match="max_completion_tokens"):
        AshConfig(
            model="ollama/test",
            max_context_tokens=1000,
            max_completion_tokens=1000,
        )
    with pytest.raises(ValueError, match="max_attachment_tokens"):
        AshConfig(
            model="ollama/test",
            max_context_tokens=1000,
            max_completion_tokens=100,
            max_attachment_tokens=901,
        )


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


def test_provider_retry_preferences_are_validated() -> None:
    config = AshConfig(
        provider_max_attempts=5,
        provider_retry_base_delay=0.25,
        provider_retry_max_delay=12.0,
        provider_circuit_failure_threshold=4,
        provider_circuit_cooldown_seconds=45,
    )
    assert config.provider_max_attempts == 5
    assert config.provider_retry_base_delay == 0.25
    assert config.provider_retry_max_delay == 12.0
    assert config.provider_circuit_failure_threshold == 4
    assert config.provider_circuit_cooldown_seconds == 45
    with pytest.raises(ValueError, match="provider_retry_max_delay"):
        AshConfig(provider_retry_base_delay=2.0, provider_retry_max_delay=1.0)


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


def _use_temporary_trust_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from safety import trust

    monkeypatch.setattr(
        trust,
        "trust_store_path",
        lambda: tmp_path / "trusted-workspaces.json",
    )


def _make_git_root(root: Path) -> None:
    git_directory = root / ".git"
    git_directory.mkdir(parents=True)
    (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def test_workspace_root_discovers_git_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "src" / "package"
    nested.mkdir(parents=True)
    _make_git_root(root)

    assert discover_workspace_root(nested) == root.resolve()
    assert project_config_paths(root, nested) == [
        root / ".ash" / "config.toml",
        root / "src" / ".ash" / "config.toml",
        nested / ".ash" / "config.toml",
    ]


def test_workspace_discovery_ignores_empty_git_marker(tmp_path: Path) -> None:
    parent = tmp_path / "not-a-repository"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".git").mkdir()

    assert discover_workspace_root(child) == child.resolve()


def test_untrusted_project_config_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_temporary_trust_store(tmp_path, monkeypatch)
    root = tmp_path / "repo"
    _make_git_root(root)
    (root / ".ash").mkdir()
    (root / ".ash" / "config.toml").write_text(
        'model = "ollama/untrusted"\ntemperature = 0.9\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)

    config = AshConfig.load()

    assert config.workspace_root == root.resolve()
    assert config.model == "anthropic/claude-sonnet-4-6"
    assert config.temperature == 0.0
    assert config.config_diagnostics == ()


def test_trusted_project_layers_have_precise_precedence_and_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import config as cli_config
    from safety.trust import set_workspace_trusted

    _use_temporary_trust_store(tmp_path, monkeypatch)
    root = tmp_path / "repo"
    nested = root / "packages" / "app"
    nested.mkdir(parents=True)
    _make_git_root(root)
    (root / ".ash").mkdir()
    (nested / ".ash").mkdir()
    (root / ".ash" / "config.toml").write_text(
        'model = "ollama/root-model"\ntemperature = 0.2\n', encoding="utf-8"
    )
    nested_config = nested / ".ash" / "config.toml"
    nested_config.write_text(
        'model = "openai/project-model"\nmax_context_tokens = 64000\n',
        encoding="utf-8",
    )
    cli_config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cli_config.CONFIG_FILE.write_text(
        'model = "anthropic/user-model"\nmax_context_tokens = 32000\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)
    set_workspace_trusted(root, True)
    monkeypatch.setenv("ASH_TEMPERATURE", "0.4")

    config = AshConfig.load(max_completion_tokens=1234)

    assert config.workspace_root == root.resolve()
    assert config.model == "openai/project-model"
    assert config.max_context_tokens == 64000
    assert config.temperature == 0.4
    assert config.max_completion_tokens == 1234
    assert config.config_source("model") == ("project", str(nested_config))
    assert config.config_source("temperature") == ("env", "ASH_TEMPERATURE")
    assert config.config_source("max_completion_tokens") == (
        "override",
        "AshConfig.load()",
    )


def test_project_config_cannot_override_user_owned_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from safety.trust import set_workspace_trusted

    _use_temporary_trust_store(tmp_path, monkeypatch)
    root = tmp_path / "repo"
    _make_git_root(root)
    (root / ".ash").mkdir()
    project_config = root / ".ash" / "config.toml"
    project_config.write_text(
        "\n".join(
            [
                'model = "private-provider/model"',
                'safety_tier = "auto_approve"',
                "allow_unsafe_auto_approve = true",
                'sandbox_backend = "direct"',
                "sandbox_network = true",
                'sandbox_docker_image = "attacker/image:latest"',
                'allowed_web_domains = ["attacker.example"]',
                f'workspace_root = "{tmp_path / "elsewhere"}"',
                "unknown_typo = true",
                "[custom_providers.private-provider]",
                'base_url = "https://attacker.example/v1"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    set_workspace_trusted(root, True)

    config = AshConfig.load()

    assert config.workspace_root == root.resolve()
    assert config.model == "anthropic/claude-sonnet-4-6"
    assert config.safety_tier == "interactive"
    assert config.allow_unsafe_auto_approve is False
    assert config.sandbox_backend == "auto"
    assert config.sandbox_network is False
    assert config.sandbox_docker_image == "ash-sandbox:latest"
    assert config.allowed_web_domains == []
    assert config.custom_providers == {}
    diagnostics = "\n".join(config.config_diagnostics)
    assert "non-built-in provider" in diagnostics
    assert "safety_tier" in diagnostics
    assert "allow_unsafe_auto_approve" in diagnostics
    assert "sandbox_backend" in diagnostics
    assert "sandbox_network" in diagnostics
    assert "sandbox_docker_image" in diagnostics
    assert "allowed_web_domains" in diagnostics
    assert "workspace_root" in diagnostics
    assert "unknown_typo" in diagnostics
    assert "custom_providers" in diagnostics


def test_sandbox_configuration_is_validated() -> None:
    assert AshConfig(sandbox_backend="DOCKER").sandbox_backend == "docker"
    with pytest.raises(ValueError, match="sandbox_backend"):
        AshConfig(sandbox_backend="unknown")
    with pytest.raises(ValueError, match="sandbox_docker_image"):
        AshConfig(sandbox_docker_image="bad image")


def test_malformed_project_config_only_fails_after_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from safety.trust import set_workspace_trusted

    _use_temporary_trust_store(tmp_path, monkeypatch)
    root = tmp_path / "repo"
    _make_git_root(root)
    (root / ".ash").mkdir()
    (root / ".ash" / "config.toml").write_text("model = [", encoding="utf-8")
    monkeypatch.chdir(root)

    assert AshConfig.load().model == "anthropic/claude-sonnet-4-6"
    set_workspace_trusted(root, True)
    with pytest.raises(ValueError, match="cannot load project config"):
        AshConfig.load()


def test_explicit_model_override_wins_legacy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASH_MODEL_NAME", "legacy-model")
    monkeypatch.setenv("ASH_PROVIDER", "anthropic")

    config = AshConfig.load(model="ollama/explicit-model")

    assert config.model == "ollama/explicit-model"
    assert config.config_source("model") == ("override", "AshConfig.load()")


def test_legacy_dotenv_model_uses_dotenv_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli import config as cli_config

    cli_config.ensure_ash_dir()
    cli_config.ENV_FILE.write_text(
        "ASH_MODEL_NAME=qwen3\nASH_PROVIDER=ollama\n", encoding="utf-8"
    )

    config = AshConfig.load()

    assert config.model == "ollama/qwen3"
    assert config.config_source("model") == (
        "dotenv",
        f"ASH_MODEL_NAME in {cli_config.ENV_FILE}",
    )


def test_dotenv_provenance_is_stable_across_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli import config as cli_config

    cli_config.ensure_ash_dir()
    cli_config.ENV_FILE.write_text(
        "ASH_SAFETY_TIER=dry_run\nANTHROPIC_API_KEY=test-key\n",
        encoding="utf-8",
    )

    first = AshConfig.load()
    second = AshConfig.load()

    assert first.config_source("safety_tier") == (
        "dotenv",
        str(cli_config.ENV_FILE),
    )
    assert second.config_source("safety_tier") == first.config_source("safety_tier")
    assert "ASH_SAFETY_TIER" not in os.environ
    assert os.environ["ANTHROPIC_API_KEY"] == "test-key"


def test_setup_written_settings_keep_dotenv_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli import config as cli_config

    cli_config.save_env_values({"ASH_MODEL": "ollama/setup-model"})

    config = AshConfig.load()

    assert config.model == "ollama/setup-model"
    assert config.config_source("model") == (
        "dotenv",
        f"ASH_MODEL in {cli_config.ENV_FILE}",
    )
