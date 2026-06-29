"""Credential and configuration storage for Ash.

Handles reading/writing to ~/.ash/.env (API keys + ASH_MODEL) and
~/.ash/ash.toml (custom OpenAI-compatible providers).

All file writes are atomic (tempfile.mkstemp + os.replace).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

ASH_DIR = Path.home() / ".ash"
ENV_FILE = ASH_DIR / ".env"
CONFIG_FILE = ASH_DIR / "ash.toml"
_INITIAL_PATHS = (ASH_DIR, ENV_FILE, CONFIG_FILE)


def _paths() -> tuple[Path, Path, Path]:
    """Resolve paths lazily while preserving explicit test/application overrides."""

    configured = (ASH_DIR, ENV_FILE, CONFIG_FILE)
    if configured != _INITIAL_PATHS:
        return configured
    ash_dir = Path.home() / ".ash"
    return ash_dir, ash_dir / ".env", ash_dir / "ash.toml"


def ensure_ash_dir() -> Path:
    """Create ~/.ash/ directory if it does not exist. Returns the path."""
    ash_dir, _, _ = _paths()
    ash_dir.mkdir(parents=True, exist_ok=True)
    return ash_dir


def get_env_path() -> Path:
    """Return ~/.ash/.env."""
    return _paths()[1]


def get_config_path() -> Path:
    """Return ~/.ash/ash.toml."""
    return _paths()[2]


# ---------------------------------------------------------------------------
# .env file operations (atomic write)
# ---------------------------------------------------------------------------


def save_env_value(key: str, value: str) -> None:
    """Atomically write key=value to ~/.ash/.env.

    Preserves existing keys. Sets os.environ[key] = value so providers
    pick up the change immediately.
    """
    ash_dir = ensure_ash_dir()
    env_file = get_env_path()
    # Set in environment immediately so providers see it
    os.environ[key] = value

    # Read existing lines
    lines: list[str] = []
    if env_file.exists():
        with env_file.open() as f:
            for raw_line in f:
                stripped = raw_line.strip()
                # Skip empty lines and comments
                if not stripped or stripped.startswith("#"):
                    lines.append(raw_line)
                    continue
                # Parse key=... (value may contain = signs)
                if "=" in stripped:
                    existing_key = stripped.split("=", 1)[0]
                    if existing_key == key:
                        # Skip the old line; we'll append the new one
                        continue
                lines.append(raw_line)

    # Build new line
    new_line = f"{key}={value}\n"

    # Atomic write via mkstemp + replace
    fd, tmp = tempfile.mkstemp(dir=str(ash_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
            f.write(new_line)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_file)
        os.chmod(env_file, 0o600)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_env_value(key: str) -> str | None:
    """Read a value from os.environ first, then from ~/.ash/.env."""
    # os.environ wins
    if key in os.environ:
        return os.environ[key]

    env_file = get_env_path()
    if not env_file.exists():
        return None

    with env_file.open() as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                k, _, v = stripped.partition("=")
                if k == key:
                    return v
    return None


def load_env() -> dict[str, str]:
    """Load all key=value pairs from ~/.ash/.env (ignores comments/blank lines)."""
    env: dict[str, str] = {}
    env_file = get_env_path()
    if not env_file.exists():
        return env
    with env_file.open() as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                k, _, v = stripped.partition("=")
                env[k] = v
    return env


# ---------------------------------------------------------------------------
# TOML config operations (custom_providers) — uses `toml` library
# ---------------------------------------------------------------------------


def save_config(config: dict[str, Any]) -> None:
    """Save a dict (typically custom_providers) to ~/.ash/ash.toml.

    Writes atomically via tempfile.mkstemp + os.replace.
    """
    ash_dir = ensure_ash_dir()
    config_file = get_config_path()
    import toml  # type: ignore[import-untyped]

    # Serialize to string via toml library
    toml_str = toml.dumps(config)

    fd, tmp = tempfile.mkstemp(dir=str(ash_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(toml_str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_file)
        os.chmod(config_file, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> dict[str, Any]:
    """Load custom_providers from ~/.ash/ash.toml. Returns {} if file missing."""
    config_file = get_config_path()
    if not config_file.exists():
        return {}
    try:
        import toml  # type: ignore[import-untyped]

        with config_file.open() as f:
            return toml.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Config explanation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigExplanation:
    field: str
    value: Any
    source: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "detail": self.detail,
        }


def explain_config(config: Any) -> list[ConfigExplanation]:
    """Explain each AshConfig field's best-known source and masked value."""

    fields = getattr(type(config), "model_fields", {})
    toml_values = _load_raw_toml_config()
    dotenv_values = load_env()
    env_path = get_env_path()
    config_path = get_config_path()
    explanations: list[ConfigExplanation] = []

    for field in sorted(fields):
        env_key = f"ASH_{field.upper()}"
        source = "default"
        detail = "Ash built-in default"
        if field == "model" and (
            model_source := _env_or_dotenv_source("ASH_MODEL", dotenv_values, env_path)
        ):
            source, detail = model_source
        elif field == "model" and (
            legacy_model_source := _env_or_dotenv_source(
                "ASH_MODEL_NAME", dotenv_values, env_path
            )
        ):
            source, detail = legacy_model_source
        elif env_source := _env_or_dotenv_source(env_key, dotenv_values, env_path):
            source, detail = env_source
        elif field in toml_values:
            source = "toml"
            detail = str(config_path)
        explanations.append(
            ConfigExplanation(
                field=field,
                value=_mask_config_value(field, getattr(config, field)),
                source=source,
                detail=detail,
            )
        )
    return explanations


def _env_or_dotenv_source(
    key: str,
    dotenv_values: dict[str, str],
    env_path: Path,
) -> tuple[str, str] | None:
    if key in os.environ:
        if key in dotenv_values and os.environ[key] == dotenv_values[key]:
            return "dotenv", str(env_path)
        return "env", key
    if key in dotenv_values:
        return "dotenv", str(env_path)
    return None


def render_config_explain(
    explanations: list[ConfigExplanation],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps(
            {"config": [entry.to_dict() for entry in explanations]},
            ensure_ascii=False,
            sort_keys=True,
        )
    lines = ["Ash config"]
    for entry in explanations:
        lines.append(f"{entry.field}: {entry.value!r} ({entry.source}: {entry.detail})")
    return "\n".join(lines)


def _load_raw_toml_config() -> dict[str, Any]:
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _mask_config_value(field: str, value: Any) -> Any:
    if _is_secret_name(field):
        return _mask_secret(str(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _mask_nested_config_value(str(key), nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_mask_nested_config_value(field, item) for item in value]
    return value


def _mask_nested_config_value(key: str, value: Any) -> Any:
    if _is_secret_name(key):
        return _mask_secret(str(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(nested_key): _mask_nested_config_value(str(nested_key), nested_value)
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_mask_nested_config_value(key, item) for item in value]
    return value


def _is_secret_name(name: str) -> bool:
    lowered = name.casefold()
    secret_markers = (
        "api_key",
        "apikey",
        "secret",
        "password",
        "access_token",
        "auth_token",
        "bearer_token",
        "refresh_token",
    )
    return any(marker in lowered for marker in secret_markers)


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return value[:4] + "..." + value[-4:]


# ---------------------------------------------------------------------------
# Interactive stdin detection
# ---------------------------------------------------------------------------


def is_interactive_stdin() -> bool:
    """Return True if stdin appears to be a TTY (interactive)."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def mask_key(key: str) -> str:
    """Return a masked version of an API key for display, e.g. 'sk-...xxxx'."""
    value = os.environ.get(key, "")
    if len(value) <= 4:
        return "****"
    return value[:4] + "..." + value[-4:]
