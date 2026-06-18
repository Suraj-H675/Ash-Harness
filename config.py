"""Configuration loading for Ash."""

from pathlib import Path
from typing import Any

from pydantic import Field
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
        "anthropic/claude-3-7-sonnet-20250219",
        description="Model in provider/model string format (e.g. anthropic/claude-3-7-sonnet-20250219, ollama/qwen2.5-coder:7b)",
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

    safety_tier: str = Field(
        "interactive",
        description="Safety enforcement mode: 'interactive' or 'auto_approve' or 'dry_run'",
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

    mcp_servers: dict[str, Any] = Field(
        default_factory=dict,
        description="MCP server configurations loaded from .mcp.json.",
    )

    custom_providers: dict[str, dict] = Field(
        default_factory=dict,
        description="Custom OpenAI-compatible providers: name -> {base_url, api_key, models[]}",
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
        description="Memory backend: 'auto' (in-memory), 'chroma' (persistent), or 'fts5' (lexical-only)",
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

        # Load MCP servers from .mcp.json if not already populated.
        if not self.mcp_servers:
            from mcp.server import MCPServerConfig, load_mcp_servers

            mcp_path = self.workspace_root / ".mcp.json"
            if mcp_path.exists():
                raw = load_mcp_servers(mcp_path)
                self.mcp_servers = {
                    name: MCPServerConfig(
                        name=cfg.name,
                        command=cfg.command,
                        args=cfg.args,
                        env=cfg.env,
                    )
                    for name, cfg in raw.items()
                }
