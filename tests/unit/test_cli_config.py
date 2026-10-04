"""Tests for cli/config.py — atomic writes, round-trip, credential helpers."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def restore_config_paths():
    from ash.commands import config as cli_config

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
        from ash.commands import config as cli_config

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
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.save_env_value("TEST_KEY", "test_value")

        # No .tmp files should remain
        tmp_files = list((tmp_path / ".ash").glob("*.tmp"))
        assert tmp_files == []

    def test_save_env_value_rejects_symlinked_state_directory(
        self, tmp_path: Path
    ) -> None:
        from ash.commands import config as cli_config

        outside = tmp_path / "outside"
        outside.mkdir()
        linked = tmp_path / ".ash"
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlinks are unavailable: {exc}")
        cli_config.ASH_DIR = linked
        cli_config.ENV_FILE = linked / ".env"
        cli_config.CONFIG_FILE = linked / "ash.toml"

        with pytest.raises(ValueError, match="symlink or junction"):
            cli_config.save_env_value("ASH_REPRO", "secret")

        assert not (outside / ".env").exists()

    def test_save_env_value_rejects_state_root_swapped_after_path_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        home = tmp_path / "home"
        state = home / ".ash"
        state.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / ".env"
        victim.write_text("MARKER=do-not-touch\n", encoding="utf-8")
        cli_config.ASH_DIR = state
        cli_config.ENV_FILE = state / ".env"
        cli_config.CONFIG_FILE = state / "ash.toml"
        monkeypatch.delenv("DUMMY_API_KEY", raising=False)
        real_get_env_path = cli_config.get_env_path
        swapped = False

        def get_path_then_swap():
            nonlocal swapped
            path = real_get_env_path()
            if not swapped:
                swapped = True
                state.rename(home / ".ash-real")
                try:
                    state.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    pytest.skip(f"symlink creation is unavailable: {exc}")
            return path

        monkeypatch.setattr(cli_config, "get_env_path", get_path_then_swap)

        with pytest.raises((OSError, ValueError)):
            cli_config.save_env_value("DUMMY_API_KEY", "dummy-secret-value")
        assert swapped is True
        assert victim.read_text(encoding="utf-8") == "MARKER=do-not-touch\n"
        assert "DUMMY_API_KEY" not in os.environ

    def test_save_env_value_does_not_override_process_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Persisted profile state must not become a process-env override."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("MY_API_KEY", "operator-secret")
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.save_env_value("MY_API_KEY", "secret123")

        assert cli_config.load_env()["MY_API_KEY"] == "secret123"
        assert os.environ["MY_API_KEY"] == "operator-secret"

    def test_save_env_value_preserves_other_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saving one key must not destroy other existing keys."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from ash.commands import config as cli_config

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
        from ash.commands import config as cli_config

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

    def test_config_backup_is_verified_and_private(self, tmp_path: Path) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        source = tmp_path / "legacy.toml"
        source.write_bytes(b'model_name = "legacy"\n')

        backup = cli_config.backup_config_file(source, label="legacy-ash.toml")

        assert backup.parent == cli_config.ASH_DIR / "backups"
        assert backup.read_bytes() == source.read_bytes()
        assert backup.name.startswith("legacy-ash.toml.")
        assert backup.name.endswith(".bak")
        if os.name != "nt":
            assert backup.stat().st_mode & 0o777 == 0o600
            assert backup.parent.stat().st_mode & 0o777 == 0o700
        assert list(backup.parent.glob("*.tmp")) == []

    def test_config_backup_rejects_symlinks_and_invalid_labels(
        self, tmp_path: Path
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        source = tmp_path / "source.toml"
        source.write_text("value = 1\n", encoding="utf-8")
        symlink = tmp_path / "linked.toml"
        try:
            symlink.symlink_to(source)
        except OSError:
            pytest.skip("symlinks are unavailable")

        with pytest.raises(ValueError, match="symlinked"):
            cli_config.backup_config_file(symlink, label="legacy")
        with pytest.raises(ValueError, match="backup label"):
            cli_config.backup_config_file(source, label="../escape")

    def test_config_backup_rejects_oversized_source(self, tmp_path: Path) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        source = tmp_path / "large.toml"
        source.write_bytes(b"x" * (cli_config.MAX_CONFIG_FILE_BYTES + 1))

        with pytest.raises(ValueError, match="config backup source exceeds"):
            cli_config.backup_config_file(source, label="large")
        assert not (cli_config.ASH_DIR / "backups").exists()

    def test_config_file_digest_rejects_oversized_source(self, tmp_path: Path) -> None:
        from ash.commands import config as cli_config

        source = tmp_path / "large.toml"
        source.write_bytes(b"x" * (cli_config.MAX_CONFIG_FILE_BYTES + 1))

        with pytest.raises(ValueError, match="config file exceeds"):
            cli_config.config_file_digest(source)

    def test_migration_record_requires_exact_source_and_backup(
        self, tmp_path: Path
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        source = tmp_path / "legacy.toml"
        source.write_text('model_name = "legacy"\n', encoding="utf-8")
        backup = cli_config.backup_config_file(source, label="legacy")

        assert cli_config.is_config_migration_recorded(source) is False
        cli_config.record_config_migration(source, backup)
        assert cli_config.is_config_migration_recorded(source) is True

        source.write_text('model_name = "changed"\n', encoding="utf-8")
        assert cli_config.is_config_migration_recorded(source) is False

    def test_migration_record_lookup_uses_same_lexical_source_identity(
        self, tmp_path: Path
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        real_root = tmp_path / "real-root"
        source = real_root / "nested" / "legacy.toml"
        source.parent.mkdir(parents=True)
        source.write_text('model_name = "legacy"\n', encoding="utf-8")
        alias = tmp_path / "alias-root"
        try:
            alias.symlink_to(real_root, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlinks are unavailable: {exc}")
        aliased_source = alias / "nested" / "legacy.toml"

        backup = cli_config.backup_config_file(aliased_source, label="legacy")
        cli_config.record_config_migration(aliased_source, backup)

        assert cli_config.is_config_migration_recorded(aliased_source) is True

    def test_migration_record_rejects_mismatched_or_corrupt_state(
        self, tmp_path: Path
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        source = tmp_path / "legacy.toml"
        backup = tmp_path / "wrong.bak"
        source.write_text("source\n", encoding="utf-8")
        backup.write_text("different\n", encoding="utf-8")

        with pytest.raises(OSError, match="mismatched backup"):
            cli_config.record_config_migration(source, backup)

        cli_config.ensure_ash_dir()
        cli_config.migration_state_path().write_text("not json", encoding="utf-8")
        with pytest.raises(ValueError, match="cannot load config migration state"):
            cli_config.is_config_migration_recorded(source)

        cli_config.migration_state_path().write_text(
            '{"version":2,"version":1,"migrations":{}}', encoding="utf-8"
        )
        with pytest.raises(ValueError, match="duplicate JSON object key"):
            cli_config.is_config_migration_recorded(source)

    def test_migration_state_rejects_state_root_swapped_after_path_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        home = tmp_path / "home"
        state = home / ".ash"
        state.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "config-migrations.json"
        victim.write_text(
            '{"version":1,"migrations":{"attacker":{}}}\n',
            encoding="utf-8",
        )
        cli_config.ASH_DIR = state
        cli_config.ENV_FILE = state / ".env"
        cli_config.CONFIG_FILE = state / "ash.toml"
        real_state_path = cli_config.migration_state_path
        swapped = False

        def state_path_then_swap():
            nonlocal swapped
            path = real_state_path()
            if not swapped:
                swapped = True
                state.rename(home / ".ash-real")
                try:
                    state.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    pytest.skip(f"symlink creation is unavailable: {exc}")
            return path

        monkeypatch.setattr(cli_config, "migration_state_path", state_path_then_swap)

        with pytest.raises((OSError, ValueError)):
            cli_config._load_migration_state()
        assert swapped is True
        assert victim.read_text(encoding="utf-8") == (
            '{"version":1,"migrations":{"attacker":{}}}\n'
        )

    def test_save_env_values_persists_without_mutating_process_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        monkeypatch.delenv("ASH_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "operator-key")

        cli_config.save_env_values(
            {
                "OPENAI_API_KEY": "sk-test",
                "OPENAI_API_BASE": "https://example.test/v1",
                "ASH_MODEL": "openai/test-model",
            }
        )

        assert cli_config.load_env() == {
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_API_BASE": "https://example.test/v1",
            "ASH_MODEL": "openai/test-model",
        }
        assert "ASH_MODEL" not in os.environ
        assert "OPENAI_API_BASE" not in os.environ
        assert os.environ["OPENAI_API_KEY"] == "operator-key"

    def test_save_env_values_serializes_concurrent_updates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        real_write = cli_config._write_anchored_private_file
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        call_lock = threading.Lock()
        calls = 0

        def paused_write(directory, name, payload, *, label):
            nonlocal calls
            with call_lock:
                calls += 1
                call = calls
            if call == 1:
                first_entered.set()
                assert release_first.wait(5)
            else:
                second_entered.set()
            return real_write(directory, name, payload, label=label)

        monkeypatch.setattr(cli_config, "_write_anchored_private_file", paused_write)
        errors: list[BaseException] = []

        def save(key: str, value: str) -> None:
            try:
                cli_config.save_env_values({key: value})
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        first = threading.Thread(target=save, args=("FIRST_KEY", "one"))
        second = threading.Thread(target=save, args=("SECOND_KEY", "two"))
        first.start()
        assert first_entered.wait(5)
        second.start()
        assert second_entered.wait(0.1) is False
        release_first.set()
        first.join(5)
        second.join(5)

        assert errors == []
        assert first.is_alive() is False
        assert second.is_alive() is False
        assert cli_config.load_env() == {
            "FIRST_KEY": "one",
            "SECOND_KEY": "two",
        }

    def test_save_env_values_binds_one_profile_path_for_transaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        work = tmp_path / ".ash" / "profiles" / "work"
        personal = tmp_path / ".ash" / "profiles" / "personal"
        work.mkdir(parents=True)
        personal.mkdir(parents=True)
        resolutions = iter(
            (
                (work, work / ".env", work / "ash.toml"),
                (personal, personal / ".env", personal / "ash.toml"),
            )
        )
        calls = 0

        def alternating_paths():
            nonlocal calls
            calls += 1
            return next(resolutions)

        monkeypatch.setattr(cli_config, "_paths", alternating_paths)

        cli_config.save_env_values({"BOUND_KEY": "work-value"})

        assert calls == 1
        assert work.joinpath(".env").read_text(encoding="utf-8") == (
            "BOUND_KEY=work-value\n"
        )
        assert not personal.joinpath(".env").exists()

    def test_save_env_values_rejects_dotenv_injection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"

        with pytest.raises(ValueError, match="forbidden newline"):
            cli_config.save_env_value("OPENAI_API_KEY", "key\nASH_MODEL=evil/model")
        assert not cli_config.ENV_FILE.exists()

    def test_failed_env_write_does_not_mutate_process_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(
            cli_config,
            "_write_anchored_private_file",
            MagicMock(side_effect=OSError("disk")),
        )

        with pytest.raises(OSError, match="disk"):
            cli_config.save_env_value("OPENAI_API_KEY", "sk-not-persisted")
        assert "OPENAI_API_KEY" not in os.environ

    def test_save_env_value_file_permissions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saved .env file must have mode 0600."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        cli_config.ensure_ash_dir()
        cli_config.save_env_value("ANTHROPIC_API_KEY", "sk-ant-test")

        env_file = tmp_path / ".ash" / ".env"
        if os.name == "posix":
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
        from ash.commands import config as cli_config

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
        from ash.commands import config as cli_config

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
        from ash.commands import config as cli_config

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
        from ash.commands import config as cli_config

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

    def test_load_env_rejects_oversized_dotenv(self, tmp_path: Path) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        cli_config.ensure_ash_dir()
        cli_config.ENV_FILE.write_bytes(
            b"#" * (cli_config.MAX_ENV_FILE_BYTES + 1)
        )

        with pytest.raises(ValueError, match="dotenv file exceeds"):
            cli_config.load_env()

    def test_get_env_value_rejects_state_root_swapped_after_path_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        home = tmp_path / "home"
        state = home / ".ash"
        state.mkdir(parents=True)
        (state / ".env").write_text("DUMMY_READ_KEY=legitimate\n", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / ".env").write_text(
            "DUMMY_READ_KEY=attacker-controlled\n",
            encoding="utf-8",
        )
        cli_config.ASH_DIR = state
        cli_config.ENV_FILE = state / ".env"
        cli_config.CONFIG_FILE = state / "ash.toml"
        monkeypatch.delenv("DUMMY_READ_KEY", raising=False)
        real_get_env_path = cli_config.get_env_path
        swapped = False

        def get_path_then_swap():
            nonlocal swapped
            path = real_get_env_path()
            if not swapped:
                swapped = True
                state.rename(home / ".ash-real")
                try:
                    state.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    pytest.skip(f"symlink creation is unavailable: {exc}")
            return path

        monkeypatch.setattr(cli_config, "get_env_path", get_path_then_swap)

        with pytest.raises((OSError, ValueError)):
            cli_config.get_env_value("DUMMY_READ_KEY")
        assert swapped is True

    def test_get_env_value_rejects_plain_state_root_swap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        state = tmp_path / ".ash"
        saved = tmp_path / ".ash-original"
        replacement = tmp_path / ".ash-replacement"
        state.mkdir()
        replacement.mkdir()
        (state / ".env").write_text("DUMMY_READ_KEY=legitimate\n", encoding="utf-8")
        (replacement / ".env").write_text(
            "DUMMY_READ_KEY=attacker-controlled\n",
            encoding="utf-8",
        )
        cli_config.ASH_DIR = state
        cli_config.ENV_FILE = state / ".env"
        cli_config.CONFIG_FILE = state / "ash.toml"
        monkeypatch.delenv("DUMMY_READ_KEY", raising=False)
        real_open = cli_config.AnchoredDirectory.open
        swapped = False

        def open_then_swap(path, **kwargs):
            nonlocal swapped
            directory = real_open(path, **kwargs)
            if Path(path) == state and not swapped:
                swapped = True
                state.rename(saved)
                replacement.rename(state)
            return directory

        monkeypatch.setattr(
            cli_config.AnchoredDirectory,
            "open",
            staticmethod(open_then_swap),
        )

        with pytest.raises((OSError, ValueError)):
            cli_config.get_env_value("DUMMY_READ_KEY")

        assert swapped is True

    def test_load_env_rejects_plain_state_root_swap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        state = tmp_path / ".ash"
        saved = tmp_path / ".ash-original"
        replacement = tmp_path / ".ash-replacement"
        state.mkdir()
        replacement.mkdir()
        (state / ".env").write_text("SAFE=legitimate\n", encoding="utf-8")
        (replacement / ".env").write_text("EVIL=attacker\n", encoding="utf-8")
        cli_config.ASH_DIR = state
        cli_config.ENV_FILE = state / ".env"
        cli_config.CONFIG_FILE = state / "ash.toml"
        real_open = cli_config.AnchoredDirectory.open
        swapped = False

        def open_then_swap(path, **kwargs):
            nonlocal swapped
            directory = real_open(path, **kwargs)
            if Path(path) == state and not swapped:
                swapped = True
                state.rename(saved)
                replacement.rename(state)
            return directory

        monkeypatch.setattr(
            cli_config.AnchoredDirectory,
            "open",
            staticmethod(open_then_swap),
        )

        with pytest.raises((OSError, ValueError)):
            cli_config.load_env()

        assert swapped is True


class TestTomlConfig:
    """Tests for save_config / load_config with TOML."""

    def test_save_and_load_custom_providers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """save_config → load_config round-trip preserves custom_providers."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from ash.commands import config as cli_config

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
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = tmp_path / ".ash" / ".env"
        cli_config.CONFIG_FILE = tmp_path / ".ash" / "ash.toml"

        # No ensure_ash_dir, so ash.toml doesn't exist
        result = cli_config.load_config()
        assert result == {}

    def test_mutate_config_serializes_concurrent_read_modify_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        cli_config.ensure_ash_dir()
        cli_config.save_config({"existing": "keep"})
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        errors: list[BaseException] = []

        def first_mutation(config: dict[str, object]) -> None:
            config["alpha"] = 1
            first_entered.set()
            assert release_first.wait(5)

        def second_mutation(config: dict[str, object]) -> None:
            second_entered.set()
            config["beta"] = 2

        def run(mutator) -> None:
            try:
                cli_config.mutate_config(mutator)
            except BaseException as exc:  # pragma: no cover - diagnostic assertion
                errors.append(exc)

        first = threading.Thread(target=run, args=(first_mutation,))
        second = threading.Thread(target=run, args=(second_mutation,))
        first.start()
        assert first_entered.wait(5)
        second.start()
        assert not second_entered.wait(0.1)
        release_first.set()
        first.join(5)
        second.join(5)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert second_entered.is_set()
        assert cli_config.load_config(strict=True) == {
            "existing": "keep",
            "alpha": 1,
            "beta": 2,
        }

    def test_save_config_preserves_renamed_file_on_directory_sync_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        cli_config.ensure_ash_dir()
        cli_config.save_config({"model": "old/model"})

        def fail_sync(self) -> None:
            del self
            raise OSError("injected directory durability failure")

        monkeypatch.setattr(cli_config.AnchoredDirectory, "sync", fail_sync)

        with pytest.raises(OSError, match="durability failure"):
            cli_config.save_config({"model": "new/model"})

        assert cli_config.CONFIG_FILE.exists()
        assert cli_config.load_config(strict=True) == {"model": "new/model"}

    def test_load_config_rejects_oversized_toml(self, tmp_path: Path) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
        cli_config.ensure_ash_dir()
        cli_config.CONFIG_FILE.write_bytes(
            b"#" * (cli_config.MAX_CONFIG_FILE_BYTES + 1)
        )

        with pytest.raises(ValueError, match="user TOML config exceeds"):
            cli_config.load_config(strict=True)

    def test_save_config_rejects_state_root_swapped_after_path_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        home = tmp_path / "home"
        state = home / ".ash"
        state.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "ash.toml"
        victim.write_text('marker = "do-not-touch"\n', encoding="utf-8")
        cli_config.ASH_DIR = state
        cli_config.ENV_FILE = state / ".env"
        cli_config.CONFIG_FILE = state / "ash.toml"
        real_get_config_path = cli_config.get_config_path
        swapped = False

        def get_path_then_swap():
            nonlocal swapped
            path = real_get_config_path()
            if not swapped:
                swapped = True
                state.rename(home / ".ash-real")
                try:
                    state.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    pytest.skip(f"symlink creation is unavailable: {exc}")
            return path

        monkeypatch.setattr(cli_config, "get_config_path", get_path_then_swap)

        with pytest.raises((OSError, ValueError)):
            cli_config.save_config({"model": "dummy/model"})
        assert swapped is True
        assert victim.read_text(encoding="utf-8") == 'marker = "do-not-touch"\n'

    def test_save_config_rejects_plain_state_root_swap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        home = tmp_path / "home"
        state = home / ".ash"
        saved = home / ".ash-original"
        replacement = tmp_path / "replacement"
        state.mkdir(parents=True)
        replacement.mkdir()
        victim = replacement / "ash.toml"
        victim.write_text('marker = "do-not-touch"\n', encoding="utf-8")
        cli_config.ASH_DIR = state
        cli_config.ENV_FILE = state / ".env"
        cli_config.CONFIG_FILE = state / "ash.toml"
        real_open = cli_config.AnchoredDirectory.open
        swapped = False

        def open_then_swap(path, **kwargs):
            nonlocal swapped
            directory = real_open(path, **kwargs)
            if Path(path) == state and not swapped:
                swapped = True
                state.rename(saved)
                replacement.rename(state)
            return directory

        monkeypatch.setattr(
            cli_config.AnchoredDirectory,
            "open",
            staticmethod(open_then_swap),
        )

        with pytest.raises((OSError, ValueError)):
            cli_config.save_config(
                {"custom_providers": {"private": {"api_key": "SECRET"}}}
            )

        assert swapped is True
        assert (state / "ash.toml").read_text(encoding="utf-8") == (
            'marker = "do-not-touch"\n'
        )
        assert not (saved / "ash.toml").exists()

    def test_load_config_rejects_state_root_swapped_after_path_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        home = tmp_path / "home"
        state = home / ".ash"
        state.mkdir(parents=True)
        (state / "ash.toml").write_text('model = "legitimate/model"\n', encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "ash.toml").write_text(
            'model = "attacker/model"\n',
            encoding="utf-8",
        )
        cli_config.ASH_DIR = state
        cli_config.ENV_FILE = state / ".env"
        cli_config.CONFIG_FILE = state / "ash.toml"
        real_get_config_path = cli_config.get_config_path
        swapped = False

        def get_path_then_swap():
            nonlocal swapped
            path = real_get_config_path()
            if not swapped:
                swapped = True
                state.rename(home / ".ash-real")
                try:
                    state.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    pytest.skip(f"symlink creation is unavailable: {exc}")
            return path

        monkeypatch.setattr(cli_config, "get_config_path", get_path_then_swap)

        with pytest.raises((OSError, ValueError)):
            cli_config.load_config(strict=True)
        assert swapped is True

    def test_load_config_rejects_plain_state_root_swap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands import config as cli_config

        home = tmp_path / "home"
        state = home / ".ash"
        saved = home / ".ash-original"
        replacement = tmp_path / "replacement"
        state.mkdir(parents=True)
        replacement.mkdir()
        (state / "ash.toml").write_text(
            'model = "legitimate/model"\n', encoding="utf-8"
        )
        (replacement / "ash.toml").write_text(
            'model = "attacker/model"\n', encoding="utf-8"
        )
        cli_config.ASH_DIR = state
        cli_config.ENV_FILE = state / ".env"
        cli_config.CONFIG_FILE = state / "ash.toml"
        real_open = cli_config.AnchoredDirectory.open
        swapped = False

        def open_then_swap(path, **kwargs):
            nonlocal swapped
            directory = real_open(path, **kwargs)
            if Path(path) == state and not swapped:
                swapped = True
                state.rename(saved)
                replacement.rename(state)
            return directory

        monkeypatch.setattr(
            cli_config.AnchoredDirectory,
            "open",
            staticmethod(open_then_swap),
        )

        with pytest.raises((OSError, ValueError)):
            cli_config.load_config(strict=True)

        assert swapped is True


def test_json_schema_loader_rejects_oversized_files(tmp_path: Path) -> None:
    from ash.cli import MAX_JSON_SCHEMA_BYTES, _load_json_schema

    schema = tmp_path / "schema.json"
    schema.write_bytes(b" " * (MAX_JSON_SCHEMA_BYTES + 1))

    with pytest.raises(ValueError, match="JSON Schema file exceeds"):
        _load_json_schema(schema)


def test_json_schema_loader_rejects_duplicate_fields(tmp_path: Path) -> None:
    from ash.cli import _load_json_schema

    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"string","type":"object"}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        _load_json_schema(schema)


class TestMaskKey:
    """Tests for mask_key display helper."""

    def test_mask_key_short_value(self) -> None:
        """Short values are fully masked."""
        from ash.commands.config import mask_key

        assert mask_key("SHORT") == "****"

    def test_mask_key_long_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Long values show first 4 + ... + last 4 chars of the env value."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abcdefgh1234")
        from ash.commands.config import mask_key

        result = mask_key("ANTHROPIC_API_KEY")
        assert result.startswith("sk-a")
        assert result.endswith("1234")
        assert "..." in result


class TestIsInteractiveStdin:
    """Tests for is_interactive_stdin."""

    def test_isatty_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When stdin is a TTY, returns True."""
        monkeypatch.setattr(sys, "stdin", io_open_mock(isatty=True))
        from ash.commands.config import is_interactive_stdin

        assert is_interactive_stdin() is True

    def test_isatty_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When stdin is not a TTY (piped), returns False."""
        monkeypatch.setattr(sys, "stdin", io_open_mock(isatty=False))
        from ash.commands.config import is_interactive_stdin

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
    from ash.commands import config as cli_config
    from ash.config import AshConfig

    cli_config.ASH_DIR = tmp_path / ".ash"
    cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
    cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"
    cli_config.save_env_value("ANTHROPIC_API_KEY", "saved-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    AshConfig()

    assert os.environ["ANTHROPIC_API_KEY"] == "saved-key"


def test_prompt_cache_key_is_stable_without_exposing_workspace(tmp_path: Path) -> None:
    from ash.cli import _prompt_cache_key
    from ash.config import AshConfig

    config = AshConfig(workspace_root=tmp_path / "private-workspace")

    first = _prompt_cache_key(config)
    assert first == _prompt_cache_key(config)
    assert first.startswith("ash-project-")
    assert len(first) == len("ash-project-") + 24
    assert str(config.workspace_root) not in first


def test_model_catalog_rendering_and_shared_input_picker() -> None:
    import asyncio
    from types import SimpleNamespace

    from ash.cli import AVAILABLE_MODELS, _interactive_model_picker, _render_model_list
    from ash.config import AshConfig

    config = AshConfig(model=AVAILABLE_MODELS[0])
    rendered = _render_model_list(config, numbered=True)
    assert "[1]" in rendered
    assert "(current)" in rendered

    class Prompt:
        async def read(self, prompt: str) -> str:
            assert prompt.startswith("Pick a number")
            return "2"

    selected = []
    output = []
    loop = SimpleNamespace(switch_model=selected.append)
    asyncio.run(_interactive_model_picker(config, loop, Prompt(), output.append))

    assert selected == [AVAILABLE_MODELS[1]]
    assert config.model == AVAILABLE_MODELS[1]
    assert output[-1] == (
        f"Switched to {AVAILABLE_MODELS[1]} for this session only. "
        "Run 'ash setup model' to save a default model."
    )


def test_model_catalog_advertises_current_deepseek_models_only() -> None:
    from ash.cli import AVAILABLE_MODELS

    assert "deepseek/deepseek-flash" in AVAILABLE_MODELS
    assert "deepseek/deepseek-v4-pro" in AVAILABLE_MODELS
    assert "deepseek/deepseek-chat" not in AVAILABLE_MODELS
    assert "deepseek/deepseek-reasoner" not in AVAILABLE_MODELS


def test_model_catalog_advertises_current_openai_frontier_models() -> None:
    from ash.cli import AVAILABLE_MODELS

    assert "openai/gpt-6-astra" in AVAILABLE_MODELS
    assert "openai/gpt-6.1-sol" in AVAILABLE_MODELS
    assert "openai/gpt-6-luna" in AVAILABLE_MODELS
    assert "openai/gpt-5.2-codex" not in AVAILABLE_MODELS
    assert "openai/gpt-5-mini" not in AVAILABLE_MODELS


def test_model_catalog_uses_current_groq_production_defaults() -> None:
    from ash.cli import AVAILABLE_MODELS

    assert "groq/openai/gpt-oss-120b" in AVAILABLE_MODELS
    assert "groq/openai/gpt-oss-20b" in AVAILABLE_MODELS
    assert "groq/llama-3.3-70b-versatile" not in AVAILABLE_MODELS
    assert "groq/llama-3.1-8b-instant" not in AVAILABLE_MODELS
    assert "groq/qwen3.3-32b" not in AVAILABLE_MODELS
    assert "groq/compound-mini" not in AVAILABLE_MODELS


def test_model_picker_can_switch_to_cached_live_discovery() -> None:
    import asyncio
    from types import SimpleNamespace

    from ash.cli import (
        AVAILABLE_MODELS,
        _interactive_model_picker,
    )
    from ash.config import AshConfig

    config = AshConfig(model="openai/gpt-6-astra")
    discovered = ["openai/live-model"]
    rendered_order = [
        model
        for model in [
            item
            for item in AVAILABLE_MODELS
            if item.startswith("openai/")
        ]
        + discovered
    ]
    live_index = rendered_order.index("openai/live-model") + 1

    class Prompt:
        async def read(self, prompt: str) -> str:
            assert prompt.startswith("Pick a number")
            return str(live_index)

    selected = []
    output = []
    loop = SimpleNamespace(switch_model=selected.append)

    asyncio.run(
        _interactive_model_picker(
            config,
            loop,
            Prompt(),
            output.append,
            discovered=discovered,
        )
    )

    assert "Available openai models:" in output[0]
    assert "live-model" in output[0]
    assert selected == ["openai/live-model"]
    assert config.model == "openai/live-model"


@pytest.mark.asyncio
async def test_live_model_discovery_uses_normalized_provider_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.cli import _discover_live_model_catalog
    from ash.config import AshConfig
    from ash.providers.readiness import ProviderConnection, ProviderVerification

    verification = ProviderVerification(
        connection=ProviderConnection(
            provider="openai",
            model_name="gpt-current",
            base_url="https://example.test",
            catalog_endpoint="https://example.test/models",
            catalog_format="openai",
            auth_mode="chatgpt",
        ),
        models=("gpt-live-a", "gpt-live-b"),
        selected_model_available=True,
    )

    async def verify(config, *, timeout=10.0):
        assert config.openai_auth_mode == "chatgpt"
        assert timeout == 10.0
        return verification

    monkeypatch.setattr(
        "ash.commands.providers.verify_provider_catalog",
        verify,
    )
    config = AshConfig(
        model="openai/gpt-current",
        openai_auth_mode="chatgpt",
    )

    assert await _discover_live_model_catalog(config) == [
        "openai/gpt-live-a",
        "openai/gpt-live-b",
    ]


def test_model_catalog_includes_configured_custom_models() -> None:
    from ash.cli import _configured_model_catalog, _render_model_list
    from ash.config import AshConfig

    config = AshConfig(
        model="my-minimax/MiniMax-M2.7",
        custom_providers={
            "my-minimax": {
                "base_url": "https://api.example.test/v1",
                "models": ["MiniMax-M2.7", "MiniMax-Text"],
            }
        },
    )

    catalog = _configured_model_catalog(config)
    rendered = _render_model_list(config)

    assert "my-minimax/MiniMax-M2.7" in catalog
    assert "my-minimax/MiniMax-Text" in catalog
    assert "(current)" in rendered
    assert rendered.count("MiniMax-M2.7") >= 1


def test_model_human_renderers_sanitize_provider_control_characters() -> None:
    from ash.cli import _render_model_capabilities, _render_model_list, render_model_catalog_refresh
    from ash.config import AshConfig

    malicious = "MODEL\x1b[2J\u202ehidden\u202c"
    config = AshConfig(
        model=f"custom/{malicious}",
        custom_providers={
            "custom": {
                "base_url": "https://api.example.test/v1",
                "models": [malicious],
            }
        },
    )

    outputs = (
        _render_model_list(config),
        _render_model_capabilities(f"custom/{malicious}"),
        render_model_catalog_refresh(config, [f"custom/{malicious}"]),
    )

    for rendered in outputs:
        assert "MODEL\\x1b[2J\\u202ehidden\\u202c" in rendered
        assert "\x1b[2J" not in rendered
        assert "\u202e" not in rendered


def test_model_capability_display_covers_budgets_and_custom_models() -> None:
    from ash.cli import _render_model_capabilities, _render_model_list
    from ash.config import AshConfig

    config = AshConfig(model="anthropic/claude-sonnet-5-5")
    rendered = _render_model_capabilities("anthropic/claude-sonnet-5-5")
    list_rendered = _render_model_list(config)

    assert "tools" in rendered and "vision" in rendered and "reasoning" in rendered
    assert "context 1,000,000" in rendered
    assert "output 128,000" in rendered
    assert "claude-opus-5-5 [tools, vision, reasoning]" in list_rendered
    assert "gemini-3.8-flash [tools, vision, reasoning]" in list_rendered


def test_custom_model_capability_display_uses_configured_declaration() -> None:
    from ash.cli import _render_model_capabilities
    from ash.config import AshConfig

    config = AshConfig(
        model="custom/agent-model",
        custom_providers={
            "custom": {
                "base_url": "http://127.0.0.1:8000/v1",
                "auth_mode": "none",
                "model_capabilities": {
                    "agent-model": {
                        "native_tools": True,
                        "vision": True,
                        "context_window": 32_768,
                        "max_output_tokens": 4096,
                    }
                },
            }
        },
    )

    rendered = _render_model_capabilities("custom/agent-model", config)

    assert "[tools, vision]" in rendered
    assert "context 32,768" in rendered
    assert "output 4,096" in rendered


def test_live_catalog_refresh_renders_discovered_and_static_models() -> None:
    from ash.cli import render_model_catalog_refresh
    from ash.config import AshConfig

    config = AshConfig(model="openai/discovered-a")
    rendered = render_model_catalog_refresh(
        config,
        ["openai/discovered-b", "openai/discovered-a"],
    )

    assert "Live:" in rendered
    assert "openai/discovered-a (current)" in rendered
    assert "openai/discovered-b" in rendered
    assert "Available models:" in rendered


def test_live_catalog_refresh_reports_failure_without_losing_static_catalog() -> None:
    from ash.cli import render_model_catalog_refresh
    from ash.config import AshConfig

    config = AshConfig(model="anthropic/claude-sonnet-5-5")
    rendered = render_model_catalog_refresh(config, [], error="offline")

    assert "Live discovery unavailable: offline" in rendered
    assert "claude-sonnet-5-5" in rendered


def test_context_provenance_renders_fragment_metadata() -> None:
    from ash.cli import _render_context_provenance
    from ash.context.history import (
        ContextBudgetSlice,
        ContextFragment,
        ContextFragmentKind,
        ContextTrust,
        ContextBudgetReport,
    )

    report = ContextBudgetReport(
        maximum=1000,
        completion_reserve=100,
        input_limit=900,
        slices={"system": ContextBudgetSlice("system", 100, 10)},
        fragments=(
            ContextFragment(
                kind=ContextFragmentKind.REPO_MAP,
                source="workspace_repository_map",
                trust=ContextTrust.PROJECT,
                tokens=20,
                limit=100,
                truncated=False,
                content_sha256="a" * 64,
                metadata=(("untrusted_content_policy", "data_not_instructions"),),
            ),
        ),
    )

    rendered = _render_context_provenance(report)

    assert "repo_map source=workspace_repository_map" in rendered
    assert "trust=project" in rendered
    assert "untrusted_content_policy=data_not_instructions" in rendered


def test_runtime_capabilities_render_dynamic_and_static_sources() -> None:
    from types import SimpleNamespace

    from ash.cli import _render_runtime_capabilities
    from ash.config import AshConfig
    from ash.providers.capabilities import ProviderCapabilities

    config = AshConfig(model="ollama/tool-model")
    dynamic = SimpleNamespace(
        provider=SimpleNamespace(
            provider_family="ollama",
            model_name="tool-model",
            capabilities=ProviderCapabilities(True, local=True, context_window=32768),
            _dynamic_capabilities=ProviderCapabilities(True, local=True),
        ),
    )
    static = SimpleNamespace(
        provider=SimpleNamespace(
            provider_family="openai",
            model_name="gpt-test",
            capabilities=ProviderCapabilities(True, vision=True),
            _dynamic_capabilities=None,
        ),
    )
    nested_dynamic = SimpleNamespace(
        provider=SimpleNamespace(
            provider_family="failover",
            model_name="primary",
            capabilities=ProviderCapabilities(True, context_window=16384),
            providers=[
                SimpleNamespace(
                    _dynamic_capabilities=ProviderCapabilities(True),
                    providers=None,
                ),
                SimpleNamespace(_dynamic_capabilities=None, providers=None),
            ],
        ),
    )

    dynamic_rendered = _render_runtime_capabilities(dynamic, config)  # type: ignore[arg-type]
    static_rendered = _render_runtime_capabilities(static, config)  # type: ignore[arg-type]
    nested_rendered = _render_runtime_capabilities(nested_dynamic, config)  # type: ignore[arg-type]

    assert "Runtime capabilities for ollama/tool-model:" in dynamic_rendered
    assert "source: dynamic manifest" in dynamic_rendered
    assert "context_window=32,768" in dynamic_rendered
    assert "Runtime capabilities for openai/gpt-test:" in static_rendered
    assert "source: static/default registry" in static_rendered
    assert "source: dynamic manifest" in nested_rendered


def test_explain_config_reports_sources_and_masks_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import config as cli_config
    from ash.config import AshConfig

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
    entries = {entry.field: entry for entry in cli_config.explain_config(config)}

    assert entries["model"].source == "env"
    assert entries["model"].detail == "ASH_MODEL"
    assert entries["safety_tier"].source == "dotenv"
    assert entries["max_context_tokens"].source == "toml"
    assert entries["max_context_tokens"].value == 64000
    assert entries["openai_api_key"].value == "sk-s...alue"
    assert entries["custom_providers"].value["local"]["api_key"] == "loca...alue"


def test_explain_config_shows_credential_env_references_not_secret_values() -> None:
    from ash.commands import config as cli_config
    from ash.config import AshConfig

    config = AshConfig(
        provider_api_key_envs={
            "openai": ["OPENAI_PRIMARY", "OPENAI_BACKUP"],
        }
    )
    entries = {entry.field: entry for entry in cli_config.explain_config(config)}

    assert entries["provider_api_key_envs"].value == {
        "openai": ["OPENAI_PRIMARY", "OPENAI_BACKUP"],
    }


def test_explain_config_masks_credential_helper_definition() -> None:
    from ash.commands import config as cli_config
    from ash.config import AshConfig

    config = AshConfig(
        provider_api_key_helpers={
            "openai": {
                "command": ["op", "read", "op://private-vault/openai/key"],
                "env": ["OP_SERVICE_ACCOUNT_TOKEN"],
            }
        }
    )
    entries = {entry.field: entry for entry in cli_config.explain_config(config)}
    rendered = repr(entries["provider_api_key_helpers"].value)

    assert "private-vault" not in rendered
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in rendered


def test_render_config_explain_json_is_machine_readable() -> None:
    from ash.commands.config import ConfigExplanation, render_config_explain

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
    from ash.config import AshConfig

    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: cls(model="ollama/test")),
    )

    assert main(["config", "explain", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(entry["field"] == "model" for entry in payload["config"])


def test_config_explain_cli_json_redacts_nested_mcp_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.cli import main
    from ash.config import AshConfig

    bearer = "Bearer nested-bearer-secret-value"
    proxy = "Basic nested-proxy-secret-value"
    api_key = "nested-api-key-secret-value"
    token = "nested-token-secret-value"
    password = "nested-password-secret-value"
    secret = "nested-client-secret-value"
    plural_api_keys = "plural-api-keys-secret-value"
    hyphen_plural_api_keys = "hyphen-api-keys-secret-value"
    camel_plural_tokens = "camel-access-tokens-secret-value"
    camel_plural_secrets = "camel-client-secrets-secret-value"
    plural_passwords = "plural-passwords-secret-value"
    max_access_token = "max-access-token-secret-value"
    max_auth_token = "max-auth-token-secret-value"
    credential = "credential-secret-value"
    credentials = "credentials-secret-value"
    private_key = "private-key-secret-value"
    private_key_camel = "private-key-camel-secret-value"
    cookie = "session-cookie-secret-value"
    set_cookie = "session-set-cookie-secret-value"
    short_secret = "x"
    mcp_servers = {
        "remote": {
            "transport": "http",
            "headers": {
                "Authorization": bearer,
                "proxy-authorization": proxy,
                "X-API-Key": api_key,
                "x-request-token": token,
                "dbPassword": password,
                "clientSecret": secret,
                "apiKeys": plural_api_keys,
                "API-KEYS": hyphen_plural_api_keys,
                "accessTokens": camel_plural_tokens,
                "clientSecrets": camel_plural_secrets,
                "PASSWORDS": plural_passwords,
                "Cookie": cookie,
                "Set-Cookie": set_cookie,
                "Accept": "application/json",
            },
            "nested": [
                {"TOKEN": token, "ordinary": "keep-this-value"},
                {
                    "tokenizer": "keep-tokenizer-value",
                    "secretary": "keep-secretary-value",
                },
            ],
            "args": ["--safe", "ordinary-argument"],
            "metadata": {
                "apiKey": short_secret,
                "credential": credential,
                "credentials": credentials,
                "private_key": private_key,
                "privateKey": private_key_camel,
                "maxAccessToken": max_access_token,
                "max_auth_token": max_auth_token,
                "max_context_tokens": 2048,
                "description": "example",
            },
        }
    }
    config = AshConfig(
        model="ollama/test",
        mcp_servers=mcp_servers,
        max_context_tokens=64000,
        agent_token_budget=4000,
        show_token_meter=True,
    )
    config._config_sources = {
        "mcp_servers": ("env", f"resolved from {bearer}; short secret {short_secret}"),
    }
    original_mcp_servers = json.loads(json.dumps(mcp_servers))

    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    assert main(["config", "explain", "--json"]) == 0
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)
    mcp_value = next(
        entry["value"] for entry in payload["config"] if entry["field"] == "mcp_servers"
    )

    for raw_secret in (
        bearer,
        proxy,
        api_key,
        token,
        password,
        secret,
        plural_api_keys,
        hyphen_plural_api_keys,
        camel_plural_tokens,
        camel_plural_secrets,
        plural_passwords,
        max_access_token,
        max_auth_token,
        credential,
        credentials,
        private_key,
        private_key_camel,
        cookie,
        set_cookie,
    ):
        assert raw_secret not in rendered
    assert mcp_value["remote"]["headers"]["Authorization"] == "Bear...alue"
    assert mcp_value["remote"]["headers"]["proxy-authorization"] == "Basi...alue"
    assert mcp_value["remote"]["headers"]["X-API-Key"] == "nest...alue"
    assert mcp_value["remote"]["headers"]["apiKeys"] == "plur...alue"
    assert mcp_value["remote"]["headers"]["API-KEYS"] == "hyph...alue"
    assert mcp_value["remote"]["headers"]["accessTokens"] == "came...alue"
    assert mcp_value["remote"]["headers"]["clientSecrets"] == "came...alue"
    assert mcp_value["remote"]["headers"]["PASSWORDS"] == "plur...alue"
    assert mcp_value["remote"]["headers"]["Cookie"] == "sess...alue"
    assert mcp_value["remote"]["headers"]["Set-Cookie"] == "sess...alue"
    assert mcp_value["remote"]["headers"]["Accept"] == "application/json"
    assert mcp_value["remote"]["nested"][0]["ordinary"] == "keep-this-value"
    assert mcp_value["remote"]["nested"][1]["tokenizer"] == "keep-tokenizer-value"
    assert mcp_value["remote"]["nested"][1]["secretary"] == "keep-secretary-value"
    assert mcp_value["remote"]["metadata"] == {
        "apiKey": "****",
        "credential": "cred...alue",
        "credentials": "cred...alue",
        "private_key": "priv...alue",
        "privateKey": "priv...alue",
        "maxAccessToken": "max-...alue",
        "max_auth_token": "max-...alue",
        "max_context_tokens": 2048,
        "description": "example",
    }
    config_values = {entry["field"]: entry["value"] for entry in payload["config"]}
    assert config_values["max_context_tokens"] == 64000
    assert config_values["agent_token_budget"] == 4000
    assert config_values["show_token_meter"] is True
    assert "resolved from" in next(
        entry["detail"]
        for entry in payload["config"]
        if entry["field"] == "mcp_servers"
    )
    assert next(
        entry["detail"]
        for entry in payload["config"]
        if entry["field"] == "mcp_servers"
    ) == "resolved from Bear...alue; short secret ****"
    assert mcp_servers == original_mcp_servers
    assert config.mcp_servers == original_mcp_servers

    capsys.readouterr()
    assert main(["config", "explain"]) == 0
    human_rendered = capsys.readouterr().out
    for raw_secret in (
        bearer,
        proxy,
        api_key,
        token,
        password,
        secret,
        plural_api_keys,
        hyphen_plural_api_keys,
        camel_plural_tokens,
        camel_plural_secrets,
        plural_passwords,
        max_access_token,
        max_auth_token,
        credential,
        credentials,
        private_key,
        private_key_camel,
        cookie,
        set_cookie,
    ):
        assert raw_secret not in human_rendered
    assert "keep-this-value" in human_rendered


def test_config_explain_reports_global_cli_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.cli import main

    monkeypatch.setenv("ASH_MODEL", "ollama/test")
    assert (
        main(
            [
                "--mode",
                "plan",
                "--db-directory",
                str(tmp_path / "db"),
                "config",
                "explain",
                "--json",
            ]
        )
        == 0
    )
    entries = {
        item["field"]: item for item in json.loads(capsys.readouterr().out)["config"]
    }
    assert entries["safety_tier"]["source"] == "cli"
    assert entries["safety_tier"]["value"] == "plan"
    assert entries["db_directory"]["source"] == "cli"
    assert entries["db_directory"]["value"] == str(tmp_path / "db")


def test_cli_rejects_oversized_stdin_prompt_before_startup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.cli import MAX_CLI_INPUT_BYTES, main

    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * (MAX_CLI_INPUT_BYTES + 1)))

    assert main(["--prompt", "-"]) == 2
    assert (
        f"stdin prompt exceeds {MAX_CLI_INPUT_BYTES} bytes"
        in capsys.readouterr().err
    )


def test_cli_json_reports_oversized_stdin_prompt_as_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.cli import MAX_CLI_INPUT_BYTES, main

    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * (MAX_CLI_INPUT_BYTES + 1)))

    assert main(["--prompt", "-", "--output-format", "json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["type"] == "error"
    assert payload["error"]["exit_code"] == 2
    assert f"stdin prompt exceeds {MAX_CLI_INPUT_BYTES} bytes" in payload["error"]["message"]


def test_ci_reset_never_prompts_for_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.cli import main

    class TtyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", TtyInput("yes\n"))
    monkeypatch.setattr(
        "builtins.input",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("--ci must never prompt")
        ),
    )

    assert main(["--ci", "reset", "--cache"]) == 2
    assert "Reset cancelled." in capsys.readouterr().err


def test_reset_all_confirmation_explains_retained_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.cli import main

    class TtyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    prompts: list[str] = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", TtyInput())
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": prompts.append(prompt) or "n",
    )

    assert main(["reset", "--all"]) == 2
    assert len(prompts) == 1
    assert "named profiles and installed extensions will be retained" in prompts[0]
    assert "Reset cancelled." in capsys.readouterr().err


def test_repair_cli_delegates_to_release_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.cli import main

    observed: dict[str, object] = {}

    def fake_repair(*, current_version=None, json_output=False) -> int:
        observed["current_version"] = current_version
        observed["json_output"] = json_output
        return 0

    monkeypatch.setattr(
        "ash.commands.update.require_managed_release_invocation",
        lambda: None,
    )
    monkeypatch.setattr("ash.commands.update.repair_installation", fake_repair)

    assert main(["repair", "--json"]) == 0
    assert observed["json_output"] is True
    assert isinstance(observed["current_version"], str)


def test_update_apply_refuses_editable_invocation_before_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.cli import main

    monkeypatch.setattr(
        "ash.commands.update.require_managed_release_invocation",
        lambda: (_ for _ in ()).throw(
            ValueError("This Ash process is running from an editable/source checkout.")
        ),
    )
    monkeypatch.setattr(
        "ash.commands.update.check_for_update",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("network update check must not run")
        ),
    )

    assert main(["update", "--apply"]) == 1
    assert "editable/source checkout" in capsys.readouterr().err


def test_startup_denial_uses_one_untrusted_snapshot_even_if_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.cli import main
    from ash.config import AshConfig

    class TtyStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    stale = AshConfig(
        model="ollama/project-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    safe = stale.model_copy(update={"model": "ollama/safe-user-model"})
    load_calls: list[bool | None] = []
    runtime_observed: dict[str, object] = {}

    def fake_load_config_or_report(**kwargs):
        trust_override = kwargs.get("_workspace_trust_override")
        load_calls.append(trust_override)
        return (safe if trust_override is False else stale), 0

    def fail_persist(_path, _trusted):
        raise OSError("state directory unavailable")

    def fake_build_runtime(config, _ui, **kwargs):
        runtime_observed["model"] = config.model
        runtime_observed["workspace_trusted"] = kwargs["workspace_trusted"]
        raise ValueError("stop after trust boundary")

    monkeypatch.setattr("ash.cli._load_config_or_report", fake_load_config_or_report)
    monkeypatch.setattr(
        "ash.safety.trust.workspace_trust_state",
        lambda _path: "unknown",
    )
    monkeypatch.setattr("ash.safety.trust.set_workspace_trusted", fail_persist)
    monkeypatch.setattr(
        "ash.commands.setup._has_provider_configured",
        lambda _config: True,
    )
    monkeypatch.setattr("ash.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ash.ui.terminal.TerminalUI", lambda **_kwargs: object())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    monkeypatch.setattr(sys, "stdin", TtyStream())
    monkeypatch.setattr(sys, "stdout", TtyStream())
    monkeypatch.setattr(sys, "stderr", TtyStream())

    assert main([]) != 0
    assert load_calls == [None, False, False]
    assert runtime_observed == {
        "model": "ollama/safe-user-model",
        "workspace_trusted": False,
    }


def test_startup_revocation_discards_initially_loaded_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.cli import main
    from ash.config import AshConfig

    stale = AshConfig(
        model="ollama/project-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    safe = stale.model_copy(update={"model": "ollama/safe-user-model"})
    load_calls: list[bool | None] = []
    runtime_observed: dict[str, object] = {}

    def fake_load_config_or_report(**kwargs):
        trust_override = kwargs.get("_workspace_trust_override")
        load_calls.append(trust_override)
        return (safe if trust_override is False else stale), 0

    def fake_build_runtime(config, _ui, **kwargs):
        runtime_observed["model"] = config.model
        runtime_observed["workspace_trusted"] = kwargs["workspace_trusted"]
        raise ValueError("stop after trust boundary")

    monkeypatch.setattr("ash.cli._load_config_or_report", fake_load_config_or_report)
    monkeypatch.setattr(
        "ash.safety.trust.workspace_trust_state",
        lambda _path: "untrusted",
    )
    monkeypatch.setattr(
        "ash.commands.setup._has_provider_configured",
        lambda _config: True,
    )
    monkeypatch.setattr("ash.runtime.build_runtime", fake_build_runtime)
    monkeypatch.setattr("ash.ui.terminal.TerminalUI", lambda **_kwargs: object())

    assert main([]) != 0
    assert load_calls == [None, False]
    assert runtime_observed == {
        "model": "ollama/safe-user-model",
        "workspace_trusted": False,
    }


def test_ci_forces_setup_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.cli import main
    import ash.commands.setup as setup_module

    observed: list[bool] = []

    def fake_setup(args) -> int:
        observed.append(bool(args.non_interactive))
        return 0

    monkeypatch.setattr(setup_module, "cmd_setup", fake_setup)

    assert main(["--ci", "setup", "model"]) == 0
    assert observed == [True]


def test_memory_hit_rendering_sanitizes_workspace_terminal_controls() -> None:
    from types import SimpleNamespace

    from ash.cli import _render_memory_hit

    rendered = _render_memory_hit(
        SimpleNamespace(
            score=0.5,
            file_path="evil\x1b[2J.py\nforged",
            content="line one\n\x1b]0;owned\x07line two\u202e",
        )
    )

    assert "\x1b" not in rendered
    assert "\n" not in rendered
    assert "\u202e" not in rendered
    assert "\\x1b" in rendered
    assert "forged" in rendered


def test_dynamic_local_model_static_capabilities_are_conservative() -> None:
    from ash.cli import _render_model_capabilities
    from ash.config import AshConfig

    config = AshConfig(model="vllm/local-model")

    rendered = _render_model_capabilities("vllm/local-model", config=config)

    assert rendered == "vllm/local-model: [local]"
