"""Credential and configuration storage for Ash.

Handles reading/writing to ~/.ash/.env (API keys + ASH_MODEL) and
~/.ash/ash.toml (custom OpenAI-compatible providers).

All file writes are atomic (tempfile.mkstemp + os.replace).
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ash.profiles import active_profile_name, profile_directory
from ash.safe_io import read_bounded_bytes


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

ASH_DIR = Path.home() / ".ash"
ENV_FILE = ASH_DIR / ".env"
CONFIG_FILE = ASH_DIR / "ash.toml"
_INITIAL_PATHS = (ASH_DIR, ENV_FILE, CONFIG_FILE)
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FILE_BACKED_ENV_VALUES: dict[str, tuple[str, str]] = {}
_BACKUP_LABEL = re.compile(r"^[A-Za-z0-9_.-]+$")
_NON_SECRET_TOKEN_KEYS = frozenset(
    {
        "max_context_tokens",
        "max_completion_tokens",
        "max_turn_total_tokens",
        "max_tool_result_tokens",
        "max_attachment_tokens",
        "agent_token_budget",
        "show_token_meter",
    }
)
MAX_CONFIG_FILE_BYTES = 1024 * 1024
MAX_ENV_FILE_BYTES = 1024 * 1024
MAX_MIGRATION_STATE_BYTES = 1024 * 1024


def _paths() -> tuple[Path, Path, Path]:
    """Resolve paths lazily while preserving explicit test/application overrides."""

    configured = (ASH_DIR, ENV_FILE, CONFIG_FILE)
    if configured != _INITIAL_PATHS:
        return configured
    base_dir = Path.home() / ".ash"
    ash_dir = profile_directory(
        active_profile_name(ash_dir=base_dir),
        ash_dir=base_dir,
    )
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


def backup_config_file(path: str | Path, *, label: str) -> Path:
    """Create and verify a private immutable copy of a config file."""

    source = Path(path).expanduser()
    if not _BACKUP_LABEL.fullmatch(label):
        raise ValueError(
            "backup label may contain only letters, digits, '.', '_', and '-'"
        )
    if source.is_symlink():
        raise ValueError(f"refusing to back up symlinked config file: {source}")
    if not source.is_file():
        raise FileNotFoundError(source)

    before = source.stat()
    contents = read_bounded_bytes(
        source,
        MAX_CONFIG_FILE_BYTES,
        label="config backup source",
    )
    source_digest = hashlib.sha256(contents).digest()
    backup_dir = ensure_ash_dir() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        backup_dir.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{label}.{timestamp}.bak"
    fd, temporary = tempfile.mkstemp(
        dir=backup_dir,
        prefix=f".{label}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(contents)
            target.flush()
            os.fsync(target.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError(f"config changed while it was being backed up: {source}")
        copied_digest = hashlib.sha256(
            read_bounded_bytes(
                temporary,
                MAX_CONFIG_FILE_BYTES,
                label="temporary config backup",
            )
        ).digest()
        if copied_digest != source_digest:
            raise OSError(f"config backup verification failed: {source}")
        os.replace(temporary, destination)
        if os.name != "nt":
            destination.chmod(0o600)
        return destination
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def config_file_digest(path: str | Path) -> str:
    return hashlib.sha256(
        read_bounded_bytes(
            path,
            MAX_CONFIG_FILE_BYTES,
            label="config file",
        )
    ).hexdigest()


def migration_state_path() -> Path:
    return ensure_ash_dir() / "config-migrations.json"


def is_config_migration_recorded(path: str | Path) -> bool:
    """Return whether this exact source has a matching verified backup record."""

    source = Path(path).expanduser().resolve()
    state = _load_migration_state()
    record = state["migrations"].get(os.path.normcase(str(source)))
    if not isinstance(record, dict):
        return False
    backup_value = record.get("backup")
    digest = record.get("sha256")
    if not isinstance(backup_value, str) or not isinstance(digest, str):
        return False
    backup = Path(backup_value)
    try:
        return config_file_digest(source) == digest == config_file_digest(backup)
    except (OSError, ValueError):
        return False


def record_config_migration(path: str | Path, backup: str | Path) -> None:
    """Atomically record a completed migration after checking its backup."""

    source = Path(path).expanduser().resolve()
    backup_path = Path(backup).expanduser().resolve()
    source_digest = config_file_digest(source)
    if config_file_digest(backup_path) != source_digest:
        raise OSError("refusing to record a migration with a mismatched backup")
    state = _load_migration_state()
    state["migrations"][os.path.normcase(str(source))] = {
        "sha256": source_digest,
        "backup": str(backup_path),
        "migrated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_migration_state(state)


def _load_migration_state() -> dict[str, Any]:
    path = migration_state_path()
    if not path.exists():
        return {"version": 1, "migrations": {}}
    try:
        value = json.loads(
            read_bounded_bytes(
                path,
                MAX_MIGRATION_STATE_BYTES,
                label="config migration state",
            ).decode("utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load config migration state {path}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("migrations"), dict)
    ):
        raise ValueError(f"invalid config migration state: {path}")
    return value


def _save_migration_state(state: dict[str, Any]) -> None:
    path = migration_state_path()
    payload = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# .env file operations (atomic write)
# ---------------------------------------------------------------------------


def save_env_value(key: str, value: str) -> None:
    """Atomically write key=value to ~/.ash/.env.

    Preserves existing keys. Sets os.environ[key] = value so providers
    pick up the change immediately.
    """
    save_env_values({key: value})


def save_env_values(values: dict[str, str]) -> None:
    """Atomically persist multiple dotenv values and then publish them in-process."""

    if not values:
        return
    for key, value in values.items():
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid environment variable name: {key!r}")
        if not isinstance(value, str):
            raise TypeError(f"environment value for {key} must be a string")
        if any(character in value for character in ("\r", "\n", "\x00")):
            raise ValueError(
                f"environment value for {key} contains a forbidden newline or NUL"
            )

    ash_dir = ensure_ash_dir()
    env_file = get_env_path()
    lines: list[str] = []
    if env_file.exists():
        raw = read_bounded_bytes(
            env_file,
            MAX_ENV_FILE_BYTES,
            label="dotenv file",
        )
        for raw_line in raw.decode("utf-8").splitlines(keepends=True):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(raw_line)
                continue
            if "=" in stripped:
                existing_key = stripped.split("=", 1)[0]
                if existing_key in values:
                    continue
            lines.append(raw_line)

    fd, tmp = tempfile.mkstemp(dir=str(ash_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
            for key, value in values.items():
                f.write(f"{key}={value}\n")
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
    os.environ.update(values)
    _FILE_BACKED_ENV_VALUES.update(
        {key: (str(env_file), value) for key, value in values.items()}
    )


def file_backed_env_values(path: Path) -> dict[str, str]:
    """Return values published by this process after an atomic dotenv write."""

    selected_path = str(path)
    return {
        key: value
        for key, (source_path, value) in _FILE_BACKED_ENV_VALUES.items()
        if source_path == selected_path
    }


def get_env_value(key: str) -> str | None:
    """Read a value from os.environ first, then from ~/.ash/.env."""
    # os.environ wins
    if key in os.environ:
        return os.environ[key]

    env_file = get_env_path()
    if not env_file.exists():
        return None

    raw = read_bounded_bytes(env_file, MAX_ENV_FILE_BYTES, label="dotenv file")
    for line in raw.decode("utf-8").splitlines():
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
    raw = read_bounded_bytes(env_file, MAX_ENV_FILE_BYTES, label="dotenv file")
    for line in raw.decode("utf-8").splitlines():
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


def load_config(*, strict: bool = False) -> dict[str, Any]:
    """Load ~/.ash/ash.toml, optionally surfacing malformed input."""
    config_file = get_config_path()
    if not config_file.exists():
        return {}
    try:
        if strict:
            value = tomllib.loads(
                read_bounded_bytes(
                    config_file,
                    MAX_CONFIG_FILE_BYTES,
                    label="user TOML config",
                ).decode("utf-8")
            )
            return value if isinstance(value, dict) else {}
        import toml  # type: ignore[import-untyped]

        return toml.loads(
            read_bounded_bytes(
                config_file,
                MAX_CONFIG_FILE_BYTES,
                label="user TOML config",
            ).decode("utf-8")
        )
    except Exception:
        if strict:
            raise
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
    """Explain each AshConfig field's selected source and masked value."""

    fields = getattr(type(config), "model_fields", {})
    field_values = {field: getattr(config, field) for field in fields}
    secret_values = _collect_secret_values(field_values)
    toml_values = _load_raw_toml_config()
    dotenv_values = load_env()
    env_path = get_env_path()
    config_path = get_config_path()
    explanations: list[ConfigExplanation] = []

    for field in sorted(fields):
        env_key = f"ASH_{field.upper()}"
        source = "default"
        detail = "Ash built-in default"
        recorded = getattr(config, "config_source", lambda _field: None)(field)
        if recorded is not None:
            source, detail = recorded
        elif field == "model" and (
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
                value=_redact_known_secrets(
                    _mask_config_value(field, field_values[field]), secret_values
                ),
                source=_redact_known_secrets(source, secret_values),
                detail=_redact_known_secrets(detail, secret_values),
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
        value = tomllib.loads(
            read_bounded_bytes(
                path,
                MAX_CONFIG_FILE_BYTES,
                label="user TOML config",
            ).decode("utf-8")
        )
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _mask_config_value(field: str, value: Any) -> Any:
    return _mask_nested_config_value(field, value)


def _mask_nested_config_value(key: str, value: Any) -> Any:
    if _is_secret_name(key):
        return _mask_secret(str(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(nested_key): _mask_nested_config_value(str(nested_key), nested_value)
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_mask_nested_config_value(key, item) for item in value]
    return value


def _is_secret_name(name: str) -> bool:
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).strip("_").casefold()
    if normalized in {
        "authorization",
        "authorizations",
        "proxy_authorization",
        "proxy_authorizations",
        "credential",
        "credentials",
        "private_key",
        "private_keys",
        "privatekey",
        "privatekeys",
        "cookie",
        "cookies",
        "set_cookie",
        "set_cookies",
    }:
        return True
    parts = normalized.split("_")
    if any(part in {"password", "passwords", "secret", "secrets"} for part in parts):
        return True
    if any(part in {"token", "tokens"} for part in parts):
        return normalized not in _NON_SECRET_TOKEN_KEYS
    if any(part in {"apikey", "apikeys"} for part in parts):
        return True
    return any(
        parts[index] == "api" and parts[index + 1] in {"key", "keys"}
        for index in range(len(parts) - 1)
    )


def _collect_secret_values(value: Any, *, secret_context: bool = False) -> set[str]:
    values: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            values.update(
                _collect_secret_values(
                    nested,
                    secret_context=secret_context or _is_secret_name(str(key)),
                )
            )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            values.update(
                _collect_secret_values(item, secret_context=secret_context)
            )
    elif secret_context and isinstance(value, str) and value:
        values.add(value)
    return values


def _redact_known_secrets(value: Any, secret_values: set[str]) -> Any:
    if isinstance(value, str):
        redacted = value
        for secret in sorted(secret_values, key=len, reverse=True):
            replacement = _mask_secret(secret)
            if redacted == secret:
                return replacement
            if len(secret) >= 4:
                redacted = redacted.replace(secret, replacement)
            else:
                redacted = re.sub(
                    rf"(?<![A-Za-z0-9_-]){re.escape(secret)}(?![A-Za-z0-9_-])",
                    replacement,
                    redacted,
                )
        return redacted
    if isinstance(value, Mapping):
        return {
            key: _redact_known_secrets(nested, secret_values)
            for key, nested in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_known_secrets(item, secret_values) for item in value]
    return value


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
