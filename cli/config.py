"""Credential and configuration storage for Ash.

Handles reading/writing to ~/.ash/.env (API keys + ASH_MODEL) and
~/.ash/ash.toml (custom OpenAI-compatible providers).

All file writes are atomic (tempfile.mkstemp + os.replace).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

ASH_DIR = Path.home() / ".ash"
ENV_FILE = ASH_DIR / ".env"
CONFIG_FILE = ASH_DIR / "ash.toml"


def ensure_ash_dir() -> Path:
    """Create ~/.ash/ directory if it does not exist. Returns the path."""
    ASH_DIR.mkdir(parents=True, exist_ok=True)
    return ASH_DIR


def get_env_path() -> Path:
    """Return ~/.ash/.env."""
    return ENV_FILE


def get_config_path() -> Path:
    """Return ~/.ash/ash.toml."""
    return CONFIG_FILE


# ---------------------------------------------------------------------------
# .env file operations (atomic write)
# ---------------------------------------------------------------------------


def save_env_value(key: str, value: str) -> None:
    """Atomically write key=value to ~/.ash/.env.

    Preserves existing keys. Sets os.environ[key] = value so providers
    pick up the change immediately.
    """
    ensure_ash_dir()
    # Set in environment immediately so providers see it
    os.environ[key] = value

    # Read existing lines
    lines: list[str] = []
    if ENV_FILE.exists():
        with ENV_FILE.open() as f:
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
    fd, tmp = tempfile.mkstemp(dir=str(ASH_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
            f.write(new_line)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV_FILE)
        os.chmod(ENV_FILE, 0o600)
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

    if not ENV_FILE.exists():
        return None

    with ENV_FILE.open() as f:
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
    if not ENV_FILE.exists():
        return env
    with ENV_FILE.open() as f:
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
    ensure_ash_dir()
    import toml  # type: ignore[import-untyped]

    # Serialize to string via toml library
    toml_str = toml.dumps(config)

    fd, tmp = tempfile.mkstemp(dir=str(ASH_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(toml_str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> dict[str, Any]:
    """Load custom_providers from ~/.ash/ash.toml. Returns {} if file missing."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        import toml  # type: ignore[import-untyped]

        with CONFIG_FILE.open() as f:
            return toml.load(f)
    except Exception:
        return {}


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
