"""Configuration loading for Ash."""

from __future__ import annotations

import os
import threading
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


CURRENT_CONFIG_SCHEMA_VERSION = 1

_INITIAL_USER_CONFIG_PATH = Path.home() / ".ash" / "ash.toml"
_INITIAL_DOTENV_PATH = Path.home() / ".ash" / ".env"
_DOTENV_RUNTIME_LOCK = threading.RLock()
_DOTENV_RUNTIME_VALUES: dict[str, str] = {}


def _publish_dotenv_runtime_values(
    values: dict[str, str], setting_env_keys: set[str]
) -> None:
    """Refresh only non-setting environment values previously injected by Ash."""

    runtime_values = {
        key: value for key, value in values.items() if key not in setting_env_keys
    }
    with _DOTENV_RUNTIME_LOCK:
        for key, previous in list(_DOTENV_RUNTIME_VALUES.items()):
            current = os.environ.get(key)
            if current != previous:
                _DOTENV_RUNTIME_VALUES.pop(key, None)
                continue
            replacement = runtime_values.get(key)
            if replacement is None:
                os.environ.pop(key, None)
                _DOTENV_RUNTIME_VALUES.pop(key, None)
            else:
                os.environ[key] = replacement
                _DOTENV_RUNTIME_VALUES[key] = replacement
        for key, value in runtime_values.items():
            if key in _DOTENV_RUNTIME_VALUES or key in os.environ:
                continue
            os.environ[key] = value
            _DOTENV_RUNTIME_VALUES[key] = value

PROJECT_CONFIG_DIRECTORY = ".ash"
PROJECT_CONFIG_FILENAME = "config.toml"
PROJECT_MODEL_PROVIDERS = frozenset(
    {"anthropic", "openai", "deepseek", "groq", "ollama"}
)

# Project configuration is repository-controlled input. Keep host security,
# credentials, persistence paths, and network destinations user-owned.
PROJECT_CONFIG_FIELDS = frozenset(
    {
        "config_schema_version",
        "model",
        "temperature",
        "max_context_tokens",
        "max_completion_tokens",
        "max_tool_result_tokens",
        "max_attachment_tokens",
        "steering_queue_limit",
        "tool_search_threshold",
        "prompt_cache_enabled",
        "prompt_cache_retention",
        "provider_max_attempts",
        "provider_retry_base_delay",
        "provider_retry_max_delay",
        "provider_circuit_failure_threshold",
        "provider_circuit_cooldown_seconds",
        "model_pricing_usd_per_million",
        "context_compaction_threshold",
        "context_recent_messages",
        "context_budget_weights",
        "no_color",
        "reduced_motion",
        "screen_reader_mode",
        "show_token_meter",
        "input_mode",
        "tui_mode",
        "notification_method",
        "notification_events",
        "notification_include_preview",
        "keybindings",
        "enable_sprint_planning",
        "repo_map_exclude_patterns",
        "repo_map_enabled",
        "repo_map_max_files",
        "memory_backend",
    }
)


def discover_workspace_root(start: str | Path | None = None) -> Path:
    """Return the nearest Git workspace root, falling back to the start path."""

    candidate = Path(start or Path.cwd()).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if _is_git_marker(directory / ".git"):
            return directory
    return candidate


def _is_git_marker(path: Path) -> bool:
    if path.is_dir():
        return (path / "HEAD").is_file()
    if not path.is_file():
        return False
    try:
        return (
            path.read_text(encoding="utf-8", errors="replace")[:1024]
            .lstrip()
            .startswith("gitdir:")
        )
    except OSError:
        return False


def _configured_settings_path(key: str, initial: Path, current_default: Path) -> Path:
    configured = Path(
        str(AshConfig.model_config.get(key) or current_default)
    ).expanduser()
    return current_default if configured == initial else configured


def project_config_paths(workspace_root: Path, cwd: Path | None = None) -> list[Path]:
    """Return project config layers from workspace root to current directory."""

    root = workspace_root.expanduser().resolve()
    current = (cwd or Path.cwd()).expanduser().resolve()
    try:
        relative = current.relative_to(root)
    except ValueError:
        directories = [root]
    else:
        directories = [root]
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            directories.append(cursor)
    return [
        directory / PROJECT_CONFIG_DIRECTORY / PROJECT_CONFIG_FILENAME
        for directory in directories
    ]


def _known_settings_values(
    settings_cls: type[AshConfig], values: dict[str, Any]
) -> dict[str, Any]:
    return {
        key: value for key, value in values.items() if key in settings_cls.model_fields
    }


def _case_insensitive_value(values: Any, key: str) -> Any:
    wanted = key.casefold()
    for candidate, value in values.items():
        if str(candidate).casefold() == wanted:
            return value
    return None


def _apply_legacy_model_values(
    destination: dict[str, Any], raw_values: Any
) -> dict[str, str]:
    """Normalize legacy model variables within one precedence layer."""

    details: dict[str, str] = {}
    model = _case_insensitive_value(raw_values, "ASH_MODEL")
    if model is None:
        model = destination.get("model")
    legacy_model = _case_insensitive_value(raw_values, "ASH_MODEL_NAME")
    provider = _case_insensitive_value(raw_values, "ASH_PROVIDER") or "anthropic"
    if model is not None:
        normalized = str(model)
        if "/" not in normalized and provider:
            normalized = f"{provider}/{normalized}"
        destination["model"] = normalized
        details["model"] = "ASH_MODEL"
    elif legacy_model is not None:
        destination["model"] = f"{provider}/{legacy_model}"
        details["model"] = "ASH_MODEL_NAME"
    return details


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot load project config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"project config {path} must contain a TOML table")
    return value


def _filter_project_config(
    settings_cls: type[AshConfig],
    path: Path,
    values: dict[str, Any],
    diagnostics: list[str],
) -> dict[str, Any]:
    filtered: dict[str, Any] = {}
    for field, value in values.items():
        if field not in settings_cls.model_fields:
            diagnostics.append(
                f"ignored unknown project config key {field!r} in {path}"
            )
            continue
        if field not in PROJECT_CONFIG_FIELDS:
            diagnostics.append(
                f"ignored user-owned project config key {field!r} in {path}"
            )
            continue
        if field == "model":
            provider, separator, _ = str(value).partition("/")
            if not separator or provider not in PROJECT_MODEL_PROVIDERS:
                diagnostics.append(
                    f"ignored project model with non-built-in provider in {path}"
                )
                continue
        filtered[field] = value
    return filtered


def _merge_config_layer(
    merged: dict[str, Any],
    sources: dict[str, tuple[str, str]],
    values: dict[str, Any],
    *,
    source: str,
    detail: str,
    detail_by_field: dict[str, str] | None = None,
) -> None:
    for field, value in values.items():
        merged[field] = value
        selected_detail = (detail_by_field or {}).get(field, detail)
        sources[field] = (source, selected_detail)


class AshConfig(BaseSettings):
    """Validated runtime settings and their resolved source metadata."""

    model_config = SettingsConfigDict(
        env_prefix="ASH_",
        toml_file=str(_INITIAL_USER_CONFIG_PATH),
        env_file=str(_INITIAL_DOTENV_PATH),
        extra="ignore",
    )

    _config_sources: dict[str, tuple[str, str]] = PrivateAttr(default_factory=dict)
    _config_diagnostics: list[str] = PrivateAttr(default_factory=list)

    model: str = Field(
        "anthropic/claude-sonnet-4-6",
        description="Model in provider/model string format (e.g. anthropic/claude-sonnet-4-6, ollama/qwen3-coder)",
    )
    config_schema_version: int = Field(
        CURRENT_CONFIG_SCHEMA_VERSION,
        ge=1,
        description="Ash config schema version. Newer versions are refused.",
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
    max_turn_total_tokens: int = Field(
        0,
        ge=0,
        le=10_000_000,
        description=(
            "Maximum prompt plus completion tokens consumed by one agent turn; "
            "0 disables the turn-wide limit."
        ),
    )
    max_tool_result_tokens: int = Field(
        20000,
        description="Limit for single tool response strings before middle truncation.",
    )
    max_attachment_tokens: int = Field(
        0,
        ge=0,
        description=(
            "Combined attachment token cap; 0 selects 25% of usable input context "
            "up to 16000 tokens."
        ),
    )
    steering_queue_limit: int = Field(
        20,
        ge=1,
        le=100,
        description="Maximum user steering messages waiting for a running turn.",
    )
    tool_search_threshold: int = Field(
        32,
        ge=0,
        le=1000,
        description=(
            "Defer nonessential provider tool schemas above this catalog size; "
            "0 disables deferred loading."
        ),
    )
    prompt_cache_enabled: bool = Field(
        True,
        description="Enable first-party provider prompt-cache optimizations.",
    )
    prompt_cache_retention: str = Field(
        "memory",
        description="Prompt cache retention: memory or extended.",
    )
    model_pricing_usd_per_million: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description=(
            "Optional explicit model pricing that overrides built-in defaults: "
            "provider/model -> {input, output, cache_read, cache_write: USD "
            "per million tokens}. Cache rates default to the input rate when "
            "omitted. Built-in defaults cover major Anthropic, OpenAI, "
            "DeepSeek, and Groq models."
        ),
    )
    fallback_models: list[str] = Field(
        default_factory=list,
        description="Ordered provider/model fallbacks used before output begins.",
    )
    provider_max_attempts: int = Field(
        3,
        ge=1,
        le=10,
        description="Maximum provider attempts before any response output is emitted.",
    )
    provider_retry_base_delay: float = Field(
        0.5,
        ge=0.0,
        le=60.0,
        description="Initial provider retry delay in seconds.",
    )
    provider_retry_max_delay: float = Field(
        8.0,
        ge=0.0,
        le=300.0,
        description="Maximum provider retry delay, including Retry-After values.",
    )
    provider_circuit_failure_threshold: int = Field(
        5,
        ge=2,
        le=20,
        description="Exhausted transient requests required to open the provider circuit.",
    )
    provider_circuit_cooldown_seconds: float = Field(
        30.0,
        gt=0.0,
        le=3600.0,
        description="Provider circuit cooldown before a half-open probe.",
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
    context_budget_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "system": 0.20,
            "tools": 0.15,
            "history": 0.45,
            "repo_map": 0.10,
            "memory": 0.10,
        },
        description=(
            "Relative provider-input budget weights for system, tools, "
            "history, repo_map, and memory context."
        ),
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
    screen_reader_mode: bool = Field(
        False,
        description="Use linear, non-rewriting interactive terminal output.",
    )
    show_token_meter: bool = Field(
        False,
        description="Show live context-token usage in the response panel.",
    )
    input_mode: str = Field(
        "emacs",
        description="Interactive editor mode: emacs or vi.",
    )
    tui_mode: str = Field(
        "viewport",
        description="Interactive terminal renderer: viewport or inline.",
    )
    notification_method: str = Field(
        "off",
        description="Desktop notification method: off, auto, osc9, or bel.",
    )
    notification_events: list[str] = Field(
        default_factory=lambda: ["turn_complete", "approval_required"],
        description="Interactive events that emit configured desktop notifications.",
    )
    notification_include_preview: bool = Field(
        False,
        description="Include a bounded assistant response preview in completion notifications.",
    )
    keybindings: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "newline": ["escape enter", "c-j"],
            "open_editor": ["c-x c-e"],
        },
        description="Prompt actions mapped to prompt-toolkit key sequences.",
    )
    enable_sprint_planning: bool = Field(
        False,
        description="Generate an editable sprint contract for multi-step requests.",
    )
    max_concurrent_agents: int = Field(
        4,
        ge=1,
        le=32,
        description="Maximum live provider-backed subagents across Ash processes.",
    )
    agent_token_budget: int = Field(
        4000,
        ge=1,
        le=1_000_000,
        description="Maximum completion tokens consumed by one subagent task.",
    )
    agent_time_budget_seconds: float = Field(
        900.0,
        ge=1.0,
        le=86_400.0,
        description="Maximum wall-clock duration of one subagent task.",
    )
    agent_lease_seconds: float = Field(
        30.0,
        ge=5.0,
        le=3600.0,
        description="Renewable durable ownership lease for a live subagent task.",
    )
    allow_unsafe_auto_approve: bool = Field(
        False,
        description="Allow full auto mode without an OS-level sandbox.",
    )
    allow_unsafe_plugin_runtime: bool = Field(
        False,
        description=(
            "Allow executable plugins without OS filesystem and network isolation."
        ),
    )
    sandbox_backend: str = Field(
        "auto",
        description="Command isolation backend: auto, native, docker, or direct.",
    )
    sandbox_network: bool = Field(
        False,
        description="Allow network access inside isolated shell commands.",
    )
    sandbox_docker_image: str = Field(
        "ash-sandbox:latest",
        description="Local Docker image used by the Docker sandbox backend.",
    )
    workspace_root: Path = Field(
        default_factory=discover_workspace_root,
        description="Scoped base folder containing project target code.",
    )
    command_blocklist: list[str] = Field(
        default=["format", "rm -rf", "Remove-Item"],
        description="Command patterns that immediately fail SafetyGuard checks.",
    )
    command_env_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "User-owned environment variable names explicitly forwarded to shell "
            "commands. Values are read from the Ash process environment at execution time."
        ),
    )
    allowed_web_domains: list[str] = Field(
        default_factory=list,
        description=(
            "Optional web fetch/search allowlist. Entries are hostnames or wildcard "
            "subdomains like *.example.com. Empty allows any public host."
        ),
    )
    web_search_provider: str = Field(
        "auto",
        description="Web search provider: auto, brave, or tavily.",
    )
    web_search_timeout_seconds: float = Field(
        20.0,
        ge=1.0,
        le=120.0,
        description="Wall-clock timeout for one web search provider request.",
    )
    browser_headless: bool = Field(
        True,
        description="Run the optional Playwright browser without a visible window.",
    )
    browser_timeout_seconds: float = Field(
        30.0,
        ge=1.0,
        le=120.0,
        description="Timeout for one browser navigation or interaction.",
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
    automation_enabled: bool = Field(
        True,
        description="Allow explicitly configured durable automation workers.",
    )
    automation_max_concurrent_runs: int = Field(
        2,
        ge=1,
        le=32,
        description="Maximum automation runs executed concurrently by one worker.",
    )
    automation_poll_seconds: float = Field(
        1.0,
        ge=0.1,
        le=60.0,
        description="Automation worker polling interval in seconds.",
    )
    automation_lease_seconds: float = Field(
        60.0,
        ge=5.0,
        le=3600.0,
        description="Renewable ownership lease duration for an automation run.",
    )
    automation_run_retention_days: int = Field(
        30,
        ge=1,
        le=3650,
        description="Retention period for terminal automation run records.",
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
    repo_map_enabled: bool = Field(
        True,
        description="Build and inject a repository symbol map into model context.",
    )
    repo_map_max_files: int = Field(
        500,
        ge=1,
        le=10_000,
        description="Maximum source files indexed by the repository map.",
    )
    lsp_enabled: bool = Field(
        True,
        description=(
            "Enable installed language servers and post-edit diagnostics in trusted workspaces."
        ),
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

    @field_validator("prompt_cache_retention")
    @classmethod
    def validate_prompt_cache_retention(cls, value: str) -> str:
        normalized = value.casefold()
        if normalized not in {"memory", "extended"}:
            raise ValueError("prompt_cache_retention must be memory or extended")
        return normalized

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
        from ash.safety.policy import PermissionMode

        try:
            return PermissionMode(value).value
        except ValueError as exc:
            allowed = ", ".join(mode.value for mode in PermissionMode)
            raise ValueError(f"safety_tier must be one of: {allowed}") from exc

    @field_validator("sandbox_backend")
    @classmethod
    def validate_sandbox_backend(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"auto", "native", "docker", "direct"}:
            raise ValueError("sandbox_backend must be auto, native, docker, or direct")
        return normalized

    @field_validator("sandbox_docker_image")
    @classmethod
    def validate_sandbox_docker_image(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(char.isspace() for char in normalized):
            raise ValueError("sandbox_docker_image must be a non-empty image reference")
        return normalized

    @field_validator("config_schema_version")
    @classmethod
    def validate_config_schema_version(cls, value: int) -> int:
        if value > CURRENT_CONFIG_SCHEMA_VERSION:
            raise ValueError(
                "config_schema_version "
                f"{value} is newer than this Ash version supports "
                f"({CURRENT_CONFIG_SCHEMA_VERSION})"
            )
        return value

    @field_validator("context_budget_weights")
    @classmethod
    def validate_context_budget_weights(
        cls, value: dict[str, float]
    ) -> dict[str, float]:
        from ash.context.history import normalize_context_budget_weights

        return normalize_context_budget_weights(value)

    @field_validator("allowed_web_domains")
    @classmethod
    def validate_allowed_web_domains(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            item = raw.strip().casefold().rstrip(".")
            if not item:
                continue
            if (
                "://" in item
                or "/" in item
                or any(character.isspace() for character in item)
            ):
                raise ValueError(
                    "allowed_web_domains entries must be hostnames, not URLs"
                )
            if item.startswith("*."):
                host = item[2:]
                if not host or "*" in host:
                    raise ValueError(
                        "allowed_web_domains wildcard entries must look like *.example.com"
                    )
            elif "*" in item:
                raise ValueError(
                    "allowed_web_domains supports wildcards only as a leading '*.'"
                )
            normalized.append(item)
        return sorted(set(normalized))

    @field_validator("web_search_provider")
    @classmethod
    def validate_web_search_provider(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"auto", "brave", "tavily"}:
            raise ValueError("web_search_provider must be auto, brave, or tavily")
        return normalized

    @field_validator("command_env_allowlist")
    @classmethod
    def validate_command_env_allowlist(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            name = raw.strip()
            first = name[:1]
            if not name or not (first.isascii() and (first.isalpha() or first == "_")):
                raise ValueError(
                    "command_env_allowlist entries must be environment variable names"
                )
            if not all(
                character.isascii() and (character.isalnum() or character == "_")
                for character in name
            ):
                raise ValueError(
                    "command_env_allowlist entries must be environment variable names"
                )
            normalized.append(name)
        return list(dict.fromkeys(normalized))

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

    @field_validator("tui_mode")
    @classmethod
    def validate_tui_mode(cls, value: str) -> str:
        normalized = value.casefold()
        if normalized not in {"viewport", "inline"}:
            raise ValueError("tui_mode must be viewport or inline")
        return normalized

    @field_validator("notification_method")
    @classmethod
    def validate_notification_method(cls, value: str) -> str:
        from ash.ui.notifications import NotificationMethod

        try:
            return NotificationMethod(value.casefold()).value
        except ValueError as exc:
            raise ValueError(
                "notification_method must be off, auto, osc9, or bel"
            ) from exc

    @field_validator("notification_events")
    @classmethod
    def validate_notification_events(cls, value: list[str]) -> list[str]:
        from ash.ui.notifications import NotificationEvent

        normalized: list[str] = []
        for event in value:
            try:
                normalized.append(NotificationEvent(event.casefold()).value)
            except ValueError as exc:
                raise ValueError(
                    "notification_events entries must be turn_complete or approval_required"
                ) from exc
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_provider_retry_delays(self) -> "AshConfig":
        if self.provider_retry_max_delay < self.provider_retry_base_delay:
            raise ValueError(
                "provider_retry_max_delay must be greater than or equal to "
                "provider_retry_base_delay"
            )
        return self

    @model_validator(mode="after")
    def validate_context_reserves(self) -> "AshConfig":
        if self.max_completion_tokens >= self.max_context_tokens:
            raise ValueError("max_completion_tokens must be below max_context_tokens")
        usable = self.max_context_tokens - self.max_completion_tokens
        if self.max_attachment_tokens > usable:
            raise ValueError(
                "max_attachment_tokens must not exceed the usable input context"
            )
        return self

    @model_validator(mode="after")
    def apply_screen_reader_preferences(self) -> "AshConfig":
        if not self.screen_reader_mode:
            return self
        self.no_color = True
        self.reduced_motion = True
        self.show_token_meter = False
        self.tui_mode = "inline"
        return self

    @property
    def provider(self) -> str:
        """Parse provider from the model string."""
        return self.model.split("/", 1)[0]

    @property
    def attachment_token_budget(self) -> int:
        if self.max_attachment_tokens:
            return self.max_attachment_tokens
        usable = self.max_context_tokens - self.max_completion_tokens
        return max(1, min(16_000, usable // 4))

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
    def load(
        cls,
        *,
        _override_source: str = "override",
        _override_detail: str = "AshConfig.load()",
        **overrides: Any,
    ) -> "AshConfig":
        """Resolve all supported layers and retain exact field provenance.

        Precedence, highest first, is explicit overrides, process environment,
        trusted project config, user TOML, user dotenv, then built-in defaults.
        """

        user_config_path = _configured_settings_path(
            "toml_file",
            _INITIAL_USER_CONFIG_PATH,
            Path.home() / ".ash" / "ash.toml",
        )
        dotenv_path = _configured_settings_path(
            "env_file",
            _INITIAL_DOTENV_PATH,
            Path.home() / ".ash" / ".env",
        )

        raw_dotenv_values = DotEnvSettingsSource(cls, env_file=dotenv_path)()
        dotenv_values = _known_settings_values(cls, raw_dotenv_values)
        user_values = _known_settings_values(
            cls,
            TomlConfigSettingsSource(cls, toml_file=user_config_path)(),
        )
        env_values = _known_settings_values(cls, EnvSettingsSource(cls)())

        dotenv_details = _apply_legacy_model_values(dotenv_values, raw_dotenv_values)
        env_details = _apply_legacy_model_values(env_values, os.environ)
        from ash.commands.config import file_backed_env_values

        file_backed_values = file_backed_env_values(dotenv_path)
        for field in list(env_values):
            env_key = env_details.get(field, f"ASH_{field.upper()}")
            if (
                file_backed_values.get(env_key) == os.environ.get(env_key)
                and dotenv_values.get(field) == env_values[field]
            ):
                del env_values[field]
                env_details.pop(field, None)

        base_values: dict[str, Any] = {}
        for values in (dotenv_values, user_values, env_values, overrides):
            base_values.update(values)
        workspace_value = base_values.get("workspace_root")
        workspace_root = (
            Path(workspace_value).expanduser().resolve()
            if workspace_value is not None
            else discover_workspace_root()
        )

        diagnostics: list[str] = []
        project_layers: list[tuple[Path, dict[str, Any]]] = []
        from ash.safety.trust import is_workspace_trusted

        if is_workspace_trusted(workspace_root):
            for path in project_config_paths(workspace_root):
                if not path.is_file():
                    continue
                values = _read_toml(path)
                filtered = _filter_project_config(cls, path, values, diagnostics)
                project_layers.append((path, filtered))

        merged: dict[str, Any] = {}
        sources: dict[str, tuple[str, str]] = {
            field: ("default", "Ash built-in default") for field in cls.model_fields
        }
        _merge_config_layer(
            merged,
            sources,
            dotenv_values,
            source="dotenv",
            detail=str(dotenv_path),
            detail_by_field={
                field: f"{key} in {dotenv_path}"
                for field, key in dotenv_details.items()
            },
        )
        _merge_config_layer(
            merged,
            sources,
            user_values,
            source="user",
            detail=str(user_config_path),
        )
        for path, values in project_layers:
            _merge_config_layer(
                merged,
                sources,
                values,
                source="project",
                detail=str(path),
            )
        _merge_config_layer(
            merged,
            sources,
            env_values,
            source="env",
            detail="process environment",
            detail_by_field={field: key for field, key in env_details.items()}
            | {
                field: f"ASH_{field.upper()}"
                for field in env_values
                if field not in env_details
            },
        )
        _merge_config_layer(
            merged,
            sources,
            overrides,
            source=_override_source,
            detail=_override_detail,
        )

        config = cls(**merged)
        if env_details.get("model") == "ASH_MODEL_NAME" and not os.environ.get(
            "ASH_MODEL"
        ):
            os.environ["ASH_MODEL"] = str(env_values["model"])
        config._config_sources = sources
        config._config_diagnostics = diagnostics
        config._record_derived_sources()
        return config

    def config_source(self, field: str) -> tuple[str, str] | None:
        """Return the selected source and detail for one field."""

        return self._config_sources.get(field)

    @property
    def config_diagnostics(self) -> tuple[str, ...]:
        """Warnings produced while loading trusted project configuration."""

        return tuple(self._config_diagnostics)

    def with_overrides(
        self,
        values: dict[str, Any],
        *,
        source: str = "cli",
        detail: str = "command-line option",
    ) -> "AshConfig":
        """Copy settings while retaining provenance for explicit overrides."""

        updated = self.model_copy(update=values)
        updated._config_sources = dict(self._config_sources)
        updated._config_sources.update(
            {field: (source, detail) for field in values if field in self.model_fields}
        )
        updated._config_diagnostics = list(self._config_diagnostics)
        updated._record_derived_sources()
        return updated

    def _record_derived_sources(self) -> None:
        if not self.screen_reader_mode:
            return
        for field in ("no_color", "reduced_motion", "show_token_meter", "tui_mode"):
            self._config_sources[field] = ("derived", "screen_reader_mode")

    def model_post_init(self, *args: Any, **kwargs: Any) -> None:
        """Handle backward compat and load MCP servers."""
        from ash.commands.config import load_env

        setting_env_keys = {
            f"ASH_{field.upper()}" for field in type(self).model_fields
        } | {"ASH_MODEL_NAME", "ASH_PROVIDER"}
        _publish_dotenv_runtime_values(load_env(), setting_env_keys)

        # Backward compat: if ANTHROPIC_API_KEY is not set but ASH_API_KEY is,
        # promote it so _build_provider() finds the right key.
        if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ASH_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = os.environ["ASH_API_KEY"]

        # Direct AshConfig() construction still supports the legacy model name.
        # AshConfig.load() normalizes this before validation and records provenance.
        if (
            "model" not in self.model_fields_set
            and not os.environ.get("ASH_MODEL")
            and os.environ.get("ASH_MODEL_NAME")
        ):
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
