"""Credential and configuration storage for Ash."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ash.plugins.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.profiles import active_profile_name, profile_directory
from ash.safe_io import (
    atomic_write_unlinked_bytes,
    ensure_anchored_directory,
    read_bounded_open_file,
    strict_json_loads,
)


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


def _state_trusted_root(ash_dir: Path | None = None) -> Path:
    """Return the stable anchor above Ash's mutable state tree."""

    state_dir = Path(os.path.abspath((ash_dir or _paths()[0]).expanduser()))
    default_root = Path(os.path.abspath((Path.home() / ".ash").expanduser()))
    try:
        state_dir.relative_to(default_root)
    except ValueError:
        candidate = state_dir.parent
    else:
        candidate = default_root.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def ensure_ash_dir() -> Path:
    """Create ~/.ash/ directory if it does not exist. Returns the path."""
    ash_dir, _, _ = _paths()
    return ensure_anchored_directory(
        ash_dir,
        trusted_root=_state_trusted_root(ash_dir),
        label="Ash state directory",
        mode=0o700,
    )


def _write_anchored_private_file(
    directory: AnchoredDirectory,
    name: str,
    payload: bytes,
    *,
    label: str,
) -> None:
    existing = directory.stat(name)
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise ValueError(f"refusing to replace non-regular {label}: {directory.path / name}")
    temporary_name = directory.unique_name(f".{name}.", ".tmp")
    descriptor = directory.create_file(temporary_name, mode=0o600)
    renamed = False
    completed = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short write while writing {label}")
            view = view[written:]
        os.fsync(descriptor)
        directory.validation_path()
        directory.rename(
            temporary_name,
            name,
            expected_source_descriptor=descriptor,
        )
        renamed = True
        directory.validation_path()
        if os.name != "nt":
            directory.sync()
        completed = True
    finally:
        if not completed:
            cleanup_name = name if renamed else temporary_name
            try:
                directory.unlink(
                    cleanup_name,
                    missing_ok=True,
                    expected_descriptor=descriptor,
                )
            except BaseException:
                pass
        os.close(descriptor)


def _read_anchored_private_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> bytes | None:
    try:
        with AnchoredDirectory.open(
            path.parent,
            create=False,
            private=False,
            pin_path=True,
        ) as directory:
            directory.validation_path()
            raw = directory.read_file(path.name, max_bytes=max_bytes)
            directory.validation_path()
            return raw
    except FileNotFoundError:
        return None
    except AnchoredFilesystemError as exc:
        detail = str(exc)
        if "exceeds" in detail.casefold():
            raise ValueError(f"{label} exceeds {max_bytes} bytes: {path}") from exc
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc


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
    contents = read_bounded_open_file(
        source,
        MAX_CONFIG_FILE_BYTES,
        label="config backup source",
    )
    source_digest = hashlib.sha256(contents).digest()
    ash_dir = ensure_ash_dir()
    trusted_root = _state_trusted_root(ash_dir)
    backup_dir = ensure_anchored_directory(
        ash_dir / "backups",
        trusted_root=trusted_root,
        label="config backup directory",
        mode=0o700,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{label}.{timestamp}.bak"
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OSError(f"config changed while it was being backed up: {source}")
    atomic_write_unlinked_bytes(
        destination,
        contents,
        label="config backup",
        mode=0o600,
        trusted_root=trusted_root,
    )
    copied_digest = hashlib.sha256(
        read_bounded_open_file(
            destination,
            MAX_CONFIG_FILE_BYTES,
            label="config backup",
            trusted_root=trusted_root,
        )
    ).digest()
    if copied_digest != source_digest:
        raise OSError(f"config backup verification failed: {source}")
    return destination


def config_file_digest(path: str | Path) -> str:
    return hashlib.sha256(
        read_bounded_open_file(
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
    trusted_root = _state_trusted_root(path.parent)
    try:
        value = strict_json_loads(
            read_bounded_open_file(
                path,
                MAX_MIGRATION_STATE_BYTES,
                label="config migration state",
                trusted_root=trusted_root,
            ).decode("utf-8")
        )
    except FileNotFoundError:
        return {"version": 1, "migrations": {}}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
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
    atomic_write_unlinked_bytes(
        path,
        payload,
        label="config migration state",
        mode=0o600,
        trusted_root=_state_trusted_root(path.parent),
    )


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

    ensure_ash_dir()
    env_file = get_env_path()
    try:
        with AnchoredDirectory.open(
            env_file.parent,
            create=True,
            private=True,
            pin_path=True,
        ) as directory:
            directory.validation_path()
            raw = directory.read_file(env_file.name, max_bytes=MAX_ENV_FILE_BYTES)
            lines: list[str] = []
            if raw is not None:
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

            rendered = "".join(lines)
            if rendered and not rendered.endswith("\n"):
                rendered += "\n"
            rendered += "".join(f"{key}={value}\n" for key, value in values.items())
            _write_anchored_private_file(
                directory,
                env_file.name,
                rendered.encode("utf-8"),
                label="dotenv file",
            )
    except AnchoredFilesystemError as exc:
        raise ValueError(f"cannot persist dotenv file {env_file}: {exc}") from exc
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
    raw = _read_anchored_private_file(
        env_file,
        max_bytes=MAX_ENV_FILE_BYTES,
        label="dotenv file",
    )
    if raw is None:
        return None
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
    raw = _read_anchored_private_file(
        env_file,
        max_bytes=MAX_ENV_FILE_BYTES,
        label="dotenv file",
    )
    if raw is None:
        return env
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
    """
    ensure_ash_dir()
    config_file = get_config_path()
    import toml  # type: ignore[import-untyped]

    # Serialize to string via toml library
    toml_str = toml.dumps(config)

    try:
        with AnchoredDirectory.open(
            config_file.parent,
            create=True,
            private=True,
            pin_path=True,
        ) as directory:
            directory.validation_path()
            _write_anchored_private_file(
                directory,
                config_file.name,
                toml_str.encode("utf-8"),
                label="user TOML config",
            )
            directory.validation_path()
    except AnchoredFilesystemError as exc:
        raise ValueError(f"cannot persist user TOML config {config_file}: {exc}") from exc


def load_config(*, strict: bool = False) -> dict[str, Any]:
    """Load ~/.ash/ash.toml, optionally surfacing malformed input."""
    config_file = get_config_path()
    try:
        raw = _read_anchored_private_file(
            config_file,
            max_bytes=MAX_CONFIG_FILE_BYTES,
            label="user TOML config",
        )
        if raw is None:
            return {}
        if strict:
            value = tomllib.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        import toml  # type: ignore[import-untyped]

        return toml.loads(raw.decode("utf-8"))
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
    try:
        value = tomllib.loads(
            read_bounded_open_file(
                path,
                MAX_CONFIG_FILE_BYTES,
                label="user TOML config",
                trusted_root=_state_trusted_root(_paths()[0]),
            ).decode("utf-8")
        )
    except FileNotFoundError:
        return {}
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
