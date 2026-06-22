"""Configuration loading for Ash."""

from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class AshConfig(BaseSettings):
    """Runtime settings loaded from environment variables, ~/.ash/.env, and ~/.ash/ash.toml."""

    model_config = SettingsConfigDict(
        env_prefix="ASH_",
        toml_file=str(Path.home() / ".ash" / "ash.toml"),
        env_file=str(Path.home() / ".ash" / ".env"),
        extra="ignore",
    )

    model: str = Field(
        "anthropic/claude-sonnet-4-6",
        description="Model in provider/model string format (e.g. anthropic/claude-sonnet-4-6, ollama/qwen3-coder)",
    )
    temperature: float = Field(0.0, description="Model generation temperature.")

    max_context_tokens: int = Field(
        128000,
        description="Maximum total tokens in the input context window.",
    )
    max_completion_tokens: int = Field(
        4000,
        description="Maximum tokens generated in response completion.",
    )
    max_tool_result_tokens: int = Field(
        20000,
        description="Limit for single tool response strings before middle truncation.",
    )
    model_pricing_usd_per_million: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description=(
            "Optional explicit model pricing: provider/model -> "
            "{input: USD per million tokens, output: USD per million tokens}."
        ),
    )
    fallback_models: list[str] = Field(
        default_factory=list,
        description="Ordered provider/model fallbacks used before output begins.",
    )
    context_compaction_threshold: float = Field(
        0.80,
        ge=0.1,
        le=1.0,
        description="Fraction of the usable input window that triggers compaction.",
    )
    context_recent_messages: int = Field(
        12,
        ge=2,
        description="Recent messages retained verbatim during compaction.",
    )

    safety_tier: str = Field(
        "interactive",
        description="Permission mode: interactive, auto_edit, plan, auto_approve, or dry_run.",
    )
    no_color: bool = Field(
        False,
        description="Disable ANSI colors in the interactive terminal UI.",
    )
    reduced_motion: bool = Field(
        False,
        description="Avoid live per-token redraws in the interactive terminal UI.",
    )
    show_token_meter: bool = Field(
        False,
        description="Show live context-token usage in the response panel.",
    )
    input_mode: str = Field(
        "emacs",
        description="Interactive editor mode: emacs or vi.",
    )
    keybindings: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "newline": ["escape enter", "c-j"],
            "open_editor": ["c-x c-e"],
        },
        description="Prompt actions mapped to prompt-toolkit key sequences.",
    )
    allow_unsafe_auto_approve: bool = Field(
        False,
        description="Allow full auto mode without an OS-level sandbox.",
    )
    workspace_root: Path = Field(
        default_factory=Path.cwd,
        description="Scoped base folder containing project target code.",
    )
    command_blocklist: list[str] = Field(
        default=["format", "rm -rf", "Remove-Item"],
        description="Command patterns that immediately fail SafetyGuard checks.",
    )

    db_directory: Path = Field(
        default=Path.home() / ".ash" / "db",
        description="Folder path housing local SQLite persistence files.",
    )
    session_retention_days: int = Field(
        0,
        ge=0,
        description="Delete sessions older than this many days; 0 disables cleanup.",
    )

    mcp_servers: dict[str, Any] = Field(
        default_factory=dict,
        description="MCP server configurations loaded from .mcp.json.",
    )

    custom_providers: dict[str, dict] = Field(
        default_factory=dict,
        description="Custom OpenAI-compatible providers with base URL, key env name, and models.",
    )

    repo_map_exclude_patterns: list[str] = Field(
        default_factory=lambda: [
            "node_modules/**",
            ".git/**",
            "__pycache__/**",
            "*.pyc",
            ".venv/**",
            "dist/**",
            "build/**",
        ],
        description="Glob patterns to exclude from RepoMap analysis.",
    )
    memory_backend: str = Field(
        "auto",
        description="Memory backend: auto, chroma, fts5, or off.",
    )
    chroma_persist_dir: Path = Field(
        Path(".ash/chroma"),
        description="Directory for ChromaDB persistent storage",
    )
    embedding_provider: str = Field(
        "auto",
        description="Embedding provider: 'auto' (deterministic), 'onnx' (local), 'openai' (remote)",
    )
    openai_api_key: str = Field("", description="API key for OpenAI embeddings")
    onnx_model_path: Path = Field(
        Path(".ash/model.onnx"),
        description="Path to ONNX MiniLM model for local embeddings",
    )

    @field_validator("memory_backend")
    @classmethod
    def validate_memory_backend(cls, value: str) -> str:
        if value not in {"auto", "chroma", "fts5", "off"}:
            raise ValueError("memory_backend must be auto, chroma, fts5, or off")
        return value

    @field_validator("model")
    @classmethod
    def validate_model_string(cls, value: str) -> str:
        """Require a non-empty ``provider/model`` identifier."""

        import os

        if "/" not in value and os.environ.get("ASH_PROVIDER"):
            value = f"{os.environ['ASH_PROVIDER']}/{value}"
        provider, separator, model_name = value.partition("/")
        if not separator or not provider.strip() or not model_name.strip():
            raise ValueError(
                "model must use provider/model format, for example "
                "'anthropic/claude-sonnet-4-5' or 'ollama/qwen2.5-coder:7b'"
            )
        return f"{provider.strip()}/{model_name.strip()}"

    @field_validator("safety_tier")
    @classmethod
    def validate_safety_tier(cls, value: str) -> str:
        from safety.policy import PermissionMode

        try:
            return PermissionMode(value).value
        except ValueError as exc:
            allowed = ", ".join(mode.value for mode in PermissionMode)
            raise ValueError(f"safety_tier must be one of: {allowed}") from exc

    @field_validator("input_mode")
    @classmethod
    def validate_input_mode(cls, value: str) -> str:
        value = value.casefold()
        if value not in {"emacs", "vi"}:
            raise ValueError("input_mode must be emacs or vi")
        return value

    @field_validator("keybindings")
    @classmethod
    def validate_keybindings(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        allowed_actions = {"newline", "open_editor"}
        unknown = set(value) - allowed_actions
        if unknown:
            raise ValueError(
                f"unknown keybinding action(s): {', '.join(sorted(unknown))}"
            )
        normalized: dict[str, list[str]] = {}
        owners: dict[tuple[str, ...], str] = {}
        for action, sequences in value.items():
            normalized[action] = []
            for raw_sequence in sequences:
                keys = tuple(raw_sequence.casefold().split())
                if not keys:
                    raise ValueError(f"empty key sequence for {action}")
                owner = owners.get(keys)
                if owner is not None:
                    raise ValueError(
                        f"key sequence {raw_sequence!r} is assigned to both {owner} and {action}"
                    )
                owners[keys] = action
                normalized[action].append(" ".join(keys))
        return normalized

    @property
    def provider(self) -> str:
        """Parse provider from the model string."""
        return self.model.split("/", 1)[0]

    @property
    def model_name(self) -> str:
        """Parse model name from the model string."""
        return self.model.split("/", 1)[1]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Use env vars before ~/.ash/ash.toml, while preserving init kwargs as highest priority."""

        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    @classmethod
    def load(cls, **overrides: Any) -> "AshConfig":
        """Load configuration with optional explicit field overrides."""

        return cls(**overrides)

    def model_post_init(self, *args: Any, **kwargs: Any) -> None:
        """Handle backward compat and load MCP servers."""
        import os

        from cli.config import load_env

        for key, value in load_env().items():
            os.environ.setdefault(key, value)

        # Backward compat: if ANTHROPIC_API_KEY is not set but ASH_API_KEY is,
        # promote it so _build_provider() finds the right key.
        if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ASH_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = os.environ["ASH_API_KEY"]

        # Backward compat: ASH_MODEL_NAME → ASH_MODEL with provider prefix.
        # Old ASH_MODEL_NAME set but no ASH_MODEL — promote, defaulting to anthropic.
        if not os.environ.get("ASH_MODEL") and os.environ.get("ASH_MODEL_NAME"):
            provider_val = os.environ.get("ASH_PROVIDER", "anthropic")
            os.environ["ASH_MODEL"] = f"{provider_val}/{os.environ['ASH_MODEL_NAME']}"
            object.__setattr__(self, "model", os.environ["ASH_MODEL"])

        # Backward compat: ASH_MODEL without "/" → prepend ASH_PROVIDER if set.
        model_val = os.environ.get("ASH_MODEL", "")
        provider_val = os.environ.get("ASH_PROVIDER", "")
        if model_val and "/" not in model_val and provider_val:
            new_model = f"{provider_val}/{model_val}"
            os.environ["ASH_MODEL"] = new_model
            object.__setattr__(self, "model", new_model)

        # Project MCP configuration is deliberately loaded later by AshLoop,
        # after the CLI has established workspace trust.
