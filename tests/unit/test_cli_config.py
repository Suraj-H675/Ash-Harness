"""Tests for cli/config.py — atomic writes, round-trip, credential helpers."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def restore_config_paths():
    from cli import config as cli_config

    original = (
        cli_config.ASH_DIR,
        cli_config.ENV_FILE,
        cli_config.CONFIG_FILE,
    )
    try:
        yield
    finally:
        (
            cli_config.ASH_DIR,
            cli_config.ENV_FILE,
            cli_config.CONFIG_FILE,
        ) = original


class TestAtomicWrite:
    """Verify that save_env_value is truly atomic."""

    def test_save_env_value_creates_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """save_env_value should create ~/.ash/.env with the key=value pair."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # Reload cli.config with patched HOME so ASH_DIR points to tmp_path
        from cli import config as cli_config

        # Patch the module-level constants
        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.save_env_value("ANTHROPIC_API_KEY", "sk-ant-test123")

        env_file = tmp_path / ".ash" / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "ANTHROPIC_API_KEY=sk-ant-test123" in content

    def test_save_env_value_is_atomic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify atomic write: no partial/temp files left behind."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.save_env_value("TEST_KEY", "test_value")

        # No .tmp files should remain
        tmp_files = list((tmp_path / ".ash").glob("*.tmp"))
        assert tmp_files == []

    def test_save_env_value_sets_os_environ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """save_env_value must set os.environ[key] immediately."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.save_env_value("MY_API_KEY", "secret123")

        assert os.environ.get("MY_API_KEY") == "secret123"

    def test_save_env_value_preserves_other_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saving one key must not destroy other existing keys."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.save_env_value("KEY_ONE", "value_one")
        cli_config.save_env_value("KEY_TWO", "value_two")

        env_file = tmp_path / ".ash" / ".env"
        content = env_file.read_text()
        assert "KEY_ONE=value_one" in content
        assert "KEY_TWO=value_two" in content

    def test_save_env_value_overwrites_existing_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saving the same key twice replaces the old value."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.save_env_value("ANTHROPIC_API_KEY", "old_key")
        cli_config.save_env_value("ANTHROPIC_API_KEY", "new_key")

        env_file = tmp_path / ".ash" / ".env"
        content = env_file.read_text()
        # Old key should not appear; new key should appear once
        assert "old_key" not in content
        assert "ANTHROPIC_API_KEY=new_key" in content

    def test_save_env_value_file_permissions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saved .env file must have mode 0600."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.save_env_value("ANTHROPIC_API_KEY", "sk-ant-test")

        env_file = tmp_path / ".ash" / ".env"
        mode = env_file.stat().st_mode & 0o777
        assert mode == 0o600


class TestLoadEnv:
    """Tests for get_env_value and load_env."""

    def test_get_env_value_prefers_environ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """os.environ takes priority over .env file."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("TEST_KEY", "from_environ")
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.ENV_FILE.write_text("TEST_KEY=from_file\n")

        result = cli_config.get_env_value("TEST_KEY")
        assert result == "from_environ"

    def test_get_env_value_falls_back_to_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If not in os.environ, read from .env file."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.ENV_FILE.write_text("ANTHROPIC_API_KEY=sk-ant-fromfile\n")

        result = cli_config.get_env_value("ANTHROPIC_API_KEY")
        assert result == "sk-ant-fromfile"

    def test_get_env_value_missing_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing key returns None."""
        monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()

        result = cli_config.get_env_value("DOES_NOT_EXIST")
        assert result is None

    def test_load_env_returns_all_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_env returns all key=value pairs."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.ENV_FILE.write_text(
            "KEY_ONE=value1\nKEY_TWO=value2\n# comment line\nKEY_THREE=value3\n"
        )

        env = cli_config.load_env()
        assert env["KEY_ONE"] == "value1"
        assert env["KEY_TWO"] == "value2"
        assert env["KEY_THREE"] == "value3"


class TestTomlConfig:
    """Tests for save_config / load_config with TOML."""

    def test_save_and_load_custom_providers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """save_config → load_config round-trip preserves custom_providers."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()

        test_config = {
            "custom_providers": {
                "my-minimax": {
                    "base_url": "https://api.minimax.io/v1",
                    "api_key": "sk-cp-test",
                    "models": ["MiniMax-M2.7"],
                },
            },
        }

        cli_config.save_config(test_config)
        result = cli_config.load_config()

        assert "custom_providers" in result
        assert "my-minimax" in result["custom_providers"]
        assert (
            result["custom_providers"]["my-minimax"]["base_url"]
            == "https://api.minimax.io/v1"
        )
        assert result["custom_providers"]["my-minimax"]["api_key"] == "sk-cp-test"
        assert result["custom_providers"]["my-minimax"]["models"] == ["MiniMax-M2.7"]

    def test_load_config_missing_file_returns_empty_dict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_config returns {} if ash.toml does not exist."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from cli import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        # No ensure_ash_dir, so ash.toml doesn't exist
        result = cli_config.load_config()
        assert result == {}


class TestMaskKey:
    """Tests for mask_key display helper."""

    def test_mask_key_short_value(self) -> None:
        """Short values are fully masked."""
        from cli.config import mask_key

        assert mask_key("SHORT") == "****"

    def test_mask_key_long_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Long values show first 4 + ... + last 4 chars of the env value."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abcdefgh1234")
        from cli.config import mask_key

        result = mask_key("ANTHROPIC_API_KEY")
        assert result.startswith("sk-a")
        assert result.endswith("1234")
        assert "..." in result


class TestIsInteractiveStdin:
    """Tests for is_interactive_stdin."""

    def test_isatty_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When stdin is a TTY, returns True."""
        monkeypatch.setattr(sys, "stdin", io_open_mock(isatty=True))
        from cli.config import is_interactive_stdin

        assert is_interactive_stdin() is True

    def test_isatty_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When stdin is not a TTY (piped), returns False."""
        monkeypatch.setattr(sys, "stdin", io_open_mock(isatty=False))
        from cli.config import is_interactive_stdin

        assert is_interactive_stdin() is False


# ---------------------------------------------------------------------------
# Helper to make a fake stdin with controllable isatty()
# ---------------------------------------------------------------------------


class _FakeStdin(io.TextIOBase):
    def __init__(self, isatty_result: bool) -> None:
        self._isatty_result = isatty_result

    def isatty(self) -> bool:
        return self._isatty_result


def io_open_mock(isatty: bool) -> io.TextIOBase:
    return _FakeStdin(isatty)


def test_ash_config_loads_saved_provider_key_into_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli import config as cli_config
    from config import AshConfig

    cli_config.ASH_DIR = tmp_path / ".ash"
    cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
    cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
    cli_config.save_env_value("ANTHROPIC_API_KEY", "saved-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    AshConfig()

    assert os.environ["ANTHROPIC_API_KEY"] == "saved-key"


def test_explain_config_reports_sources_and_masks_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli import config as cli_config
    from config import AshConfig

    cli_config.ASH_DIR = tmp_path / ".ash"
    cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
    cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
    cli_config.ensure_ash_dir()
    cli_config.ENV_FILE.write_text("ASH_SAFETY_TIER=dry_run\n", encoding="utf-8")
    cli_config.CONFIG_FILE.write_text(
        "max_context_tokens = 64000\n"
        "[custom_providers.local]\n"
        "api_key = 'local-secret-value'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASH_MODEL", "openai/gpt-5.2")
    monkeypatch.setenv("ASH_OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.delenv("ASH_SAFETY_TIER", raising=False)

    config = AshConfig(
        model="openai/gpt-5.2",
        safety_tier="dry_run",
        max_context_tokens=64000,
        openai_api_key="sk-secret-value",
        custom_providers={"local": {"api_key": "local-secret-value"}},
    )
    entries = {
        entry.field: entry for entry in cli_config.explain_config(config)
    }

    assert entries["model"].source == "env"
    assert entries["model"].detail == "ASH_MODEL"
    assert entries["safety_tier"].source == "dotenv"
    assert entries["max_context_tokens"].source == "toml"
    assert entries["max_context_tokens"].value == 64000
    assert entries["openai_api_key"].value == "sk-s...alue"
    assert entries["custom_providers"].value["local"]["api_key"] == "loca...alue"


def test_render_config_explain_json_is_machine_readable() -> None:
    from cli.config import ConfigExplanation, render_config_explain

    rendered = render_config_explain(
        [ConfigExplanation("model", "ollama/test", "env", "ASH_MODEL")],
        json_output=True,
    )

    assert json.loads(rendered) == {
        "config": [
            {
                "field": "model",
                "value": "ollama/test",
                "source": "env",
                "detail": "ASH_MODEL",
            }
        ]
    }


def test_config_explain_cli_json_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.cli import main
    from config import AshConfig

    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: cls(model="ollama/test")),
    )

    assert main(["config", "explain", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(entry["field"] == "model" for entry in payload["config"])
