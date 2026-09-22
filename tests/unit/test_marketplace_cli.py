from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ash.cli import main
from ash.config import AshConfig
from ash.plugins.catalog import sign_catalog


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from ash.commands import config as cli_config

    missing = object()
    old_profile: str | object = os.environ.get("ASH_PROFILE", missing)
    home = tmp_path / "home"
    ash_dir = home / ".ash"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ASH_PROFILE", raising=False)
    monkeypatch.delenv("ASH_PLUGIN_CATALOG", raising=False)
    monkeypatch.delenv("ASH_PLUGIN_MARKETPLACES", raising=False)
    old_paths = (cli_config.ASH_DIR, cli_config.ENV_FILE, cli_config.CONFIG_FILE)
    cli_config.ASH_DIR = ash_dir
    cli_config.ENV_FILE = ash_dir / ".env"
    cli_config.CONFIG_FILE = ash_dir / "ash.toml"
    old_toml = AshConfig.model_config.get("toml_file")
    old_env = AshConfig.model_config.get("env_file")
    AshConfig.model_config["toml_file"] = str(ash_dir / "ash.toml")
    AshConfig.model_config["env_file"] = str(ash_dir / ".env")
    try:
        yield
    finally:
        cli_config.ASH_DIR, cli_config.ENV_FILE, cli_config.CONFIG_FILE = old_paths
        if old_profile is missing:
            os.environ.pop("ASH_PROFILE", None)
        else:
            os.environ["ASH_PROFILE"] = str(old_profile)
        AshConfig.model_config["toml_file"] = old_toml
        AshConfig.model_config["env_file"] = old_env


def _write_keys(root: Path, private_key: Ed25519PrivateKey) -> Path:
    public_key = private_key.public_key().public_bytes_raw()
    path = root / "keys.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "keyId": "marketplace-key",
                        "algorithm": "ed25519",
                        "publicKey": base64.urlsafe_b64encode(public_key)
                        .rstrip(b"=")
                        .decode(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_keyring(
    root: Path,
    entries: list[tuple[str, Ed25519PrivateKey]],
) -> Path:
    path = root / "keys.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "keyId": key_id,
                        "algorithm": "ed25519",
                        "publicKey": base64.urlsafe_b64encode(
                            private_key.public_key().public_bytes_raw()
                        )
                        .rstrip(b"=")
                        .decode(),
                    }
                    for key_id, private_key in entries
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_catalog(
    path: Path,
    private_key: Ed25519PrivateKey,
    *,
    publisher: str | None,
    name: str = "demo",
    sequence: int = 1,
    key_id: str = "marketplace-key",
) -> Path:
    encoded_private = (
        base64.urlsafe_b64encode(private_key.private_bytes_raw()).rstrip(b"=").decode()
    )
    payload = {
        "version": 2 if publisher is not None else 1,
        "sequence": sequence,
        "entries": [
            {
                "name": name,
                "version": "1.0.0",
                "source": f"https://plugins.example/{name}.git",
                "ref": "v1.0.0",
                "digest": ("a" if publisher != "beta" else "b") * 64,
            }
        ],
    }
    if publisher is not None:
        payload["publisher"] = publisher
    path.write_text(
        json.dumps(
            {
                "catalog": payload,
                "keyId": key_id,
                "algorithm": "ed25519",
                "signature": sign_catalog(payload, encoded_private),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_registered_marketplace_rejects_publisher_signed_by_different_trusted_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    first_key = Ed25519PrivateKey.generate()
    second_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        "ASH_CATALOG_KEYS",
        str(
            _write_keyring(
                tmp_path,
                [("key-a", first_key), ("key-b", second_key)],
            )
        ),
    )
    catalog = _write_catalog(
        tmp_path / "alpha.json",
        first_key,
        publisher="alpha",
        key_id="key-a",
    )

    assert main(["marketplace", "add", str(catalog)]) == 0
    capsys.readouterr()

    _write_catalog(
        catalog,
        second_key,
        publisher="alpha",
        sequence=2,
        key_id="key-b",
    )
    assert main(["extensions", "search", "", "--json"]) == 2
    captured = capsys.readouterr()
    assert "signing key" in captured.err.casefold()
    assert captured.out == ""


def test_marketplace_signer_rotation_requires_explicit_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands import config as cli_config

    first_key = Ed25519PrivateKey.generate()
    second_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        "ASH_CATALOG_KEYS",
        str(
            _write_keyring(
                tmp_path,
                [("key-a", first_key), ("key-b", second_key)],
            )
        ),
    )
    catalog = _write_catalog(
        tmp_path / "alpha.json",
        first_key,
        publisher="alpha",
        key_id="key-a",
    )
    assert main(["marketplace", "add", str(catalog)]) == 0
    capsys.readouterr()

    _write_catalog(
        catalog,
        second_key,
        publisher="alpha",
        sequence=2,
        key_id="key-b",
    )
    assert main(["marketplace", "add", str(catalog)]) == 2
    assert "--replace" in capsys.readouterr().err
    assert cli_config.load_config(strict=True)["plugin_marketplace_key_ids"] == {
        "alpha": "key-a"
    }

    assert main(["marketplace", "add", str(catalog), "--replace"]) == 0
    capsys.readouterr()
    assert cli_config.load_config(strict=True)["plugin_marketplace_key_ids"] == {
        "alpha": "key-b"
    }
    assert main(["extensions", "search", "", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["publisher"] == "alpha"
    assert result["sequence"] == 2


def test_legacy_registered_marketplace_without_signer_binding_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands import config as cli_config

    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(tmp_path / "alpha.json", private_key, publisher="alpha")
    cli_config.ensure_ash_dir()
    cli_config.save_config(
        {"plugin_marketplaces": {"alpha": str(catalog.resolve())}}
    )

    assert main(["extensions", "search", "", "--json"]) == 2
    captured = capsys.readouterr()
    assert "signer binding" in captured.err.casefold()
    assert "re-register" in captured.err.casefold()
    assert captured.out == ""


def test_marketplace_add_persists_verified_v2_publisher_and_lists_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands import config as cli_config

    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(tmp_path / "alpha.json", private_key, publisher="alpha")

    assert main(["marketplace", "add", str(catalog), "--json"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added == {
        "action": "add",
        "publisher": "alpha",
        "source": str(catalog.resolve()),
    }
    assert cli_config.load_config(strict=True)["plugin_marketplaces"] == {
        "alpha": str(catalog.resolve())
    }
    assert cli_config.load_config(strict=True)["plugin_marketplace_key_ids"] == {
        "alpha": "marketplace-key"
    }

    assert main(["marketplace", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed == {
        "marketplaces": [
            {"publisher": "alpha", "source": str(catalog.resolve())}
        ]
    }


def test_marketplace_mutation_preserves_unrelated_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import config as cli_config

    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(tmp_path / "alpha.json", private_key, publisher="alpha")
    cli_config.ensure_ash_dir()
    cli_config.save_config(
        {
            "model": "openai/gpt-5.2",
            "approval_diff_mode": "side-by-side",
            "custom_providers": {
                "private": {
                    "base_url": "https://provider.example/v1",
                    "models": ["demo-model"],
                }
            },
        }
    )

    assert main(["marketplace", "add", str(catalog)]) == 0
    after_add = cli_config.load_config(strict=True)
    assert after_add["model"] == "openai/gpt-5.2"
    assert after_add["approval_diff_mode"] == "side-by-side"
    assert after_add["custom_providers"]["private"]["models"] == ["demo-model"]

    assert main(["marketplace", "remove", "alpha"]) == 0
    after_remove = cli_config.load_config(strict=True)
    assert after_remove == {
        "model": "openai/gpt-5.2",
        "approval_diff_mode": "side-by-side",
        "custom_providers": {
            "private": {
                "base_url": "https://provider.example/v1",
                "models": ["demo-model"],
            }
        },
    }


def test_marketplace_add_rejects_v1_and_requires_replace_for_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    legacy = _write_catalog(tmp_path / "legacy.json", private_key, publisher=None)
    first = _write_catalog(tmp_path / "alpha-one.json", private_key, publisher="alpha")
    second = _write_catalog(
        tmp_path / "alpha-two.json", private_key, publisher="alpha", sequence=2
    )

    assert main(["marketplace", "add", str(legacy)]) == 2
    assert "version 2" in capsys.readouterr().err

    assert main(["marketplace", "add", str(first)]) == 0
    capsys.readouterr()
    assert main(["marketplace", "add", str(second)]) == 2
    assert "--replace" in capsys.readouterr().err
    assert main(["marketplace", "add", str(second), "--replace"]) == 0


def test_marketplace_remove_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(tmp_path / "alpha.json", private_key, publisher="alpha")
    assert main(["marketplace", "add", str(catalog)]) == 0
    capsys.readouterr()

    assert main(["marketplace", "remove", "alpha", "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed == {"action": "remove", "publisher": "alpha", "removed": True}
    assert main(["marketplace", "remove", "alpha", "--json"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again == {"action": "remove", "publisher": "alpha", "removed": False}


def test_marketplace_registry_is_profile_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands import config as cli_config

    home = tmp_path / "profile-home"
    monkeypatch.setenv("HOME", str(home))
    cli_config.ASH_DIR, cli_config.ENV_FILE, cli_config.CONFIG_FILE = (
        cli_config._INITIAL_PATHS
    )
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(tmp_path / "alpha.json", private_key, publisher="alpha")

    assert (
        main(["--profile", "work", "marketplace", "add", str(catalog), "--json"])
        == 0
    )
    capsys.readouterr()
    assert main(["--profile", "work", "marketplace", "list", "--json"]) == 0
    work = json.loads(capsys.readouterr().out)
    assert [item["publisher"] for item in work["marketplaces"]] == ["alpha"]

    assert main(["--profile", "default", "marketplace", "list", "--json"]) == 0
    default = json.loads(capsys.readouterr().out)
    assert default == {"marketplaces": []}


def test_extensions_use_registered_marketplaces_and_explicit_catalog_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    alpha = _write_catalog(tmp_path / "alpha.json", private_key, publisher="alpha")
    beta = _write_catalog(
        tmp_path / "beta.json", private_key, publisher="beta", name="other"
    )
    assert main(["marketplace", "add", str(alpha)]) == 0
    capsys.readouterr()

    assert main(["extensions", "search", "", "--json"]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered["publisher"] == "alpha"
    assert [item["name"] for item in registered["plugins"]] == ["demo"]

    assert (
        main(
            [
                "extensions",
                "search",
                "",
                "--catalog",
                str(beta),
                "--json",
            ]
        )
        == 0
    )
    explicit = json.loads(capsys.readouterr().out)
    assert explicit["publisher"] == "beta"
    assert [item["name"] for item in explicit["plugins"]] == ["other"]


def test_registered_marketplace_publisher_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(tmp_path / "market.json", private_key, publisher="alpha")
    assert main(["marketplace", "add", str(catalog)]) == 0
    capsys.readouterr()

    _write_catalog(catalog, private_key, publisher="beta")
    assert main(["extensions", "search", "", "--json"]) == 2
    assert "publisher" in capsys.readouterr().err.casefold()


def test_registered_local_marketplace_symlink_swap_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(tmp_path / "market.json", private_key, publisher="alpha")
    outside = _write_catalog(
        tmp_path / "outside.json",
        private_key,
        publisher="alpha",
        name="outside-secret",
    )
    assert main(["marketplace", "add", str(catalog)]) == 0
    capsys.readouterr()
    catalog.unlink()
    try:
        catalog.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    assert main(["extensions", "search", "", "--json"]) == 2
    captured = capsys.readouterr()
    assert "symlink" in captured.err.casefold()
    assert "outside-secret" not in captured.out


def test_registered_marketplace_drives_publisher_qualified_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from types import SimpleNamespace

    from ash.plugins.lifecycle import InstalledPlugin

    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(tmp_path / "alpha.json", private_key, publisher="alpha")
    assert main(["marketplace", "add", str(catalog)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        "ash.commands.extensions.load_extension_state",
        lambda: SimpleNamespace(disabled_plugins=frozenset()),
    )
    monkeypatch.setattr(
        "ash.commands.extensions.set_plugin_enabled", lambda *args, **kwargs: None
    )
    observed: dict[str, object] = {}

    def install(source: str, **kwargs):
        observed["source"] = source
        observed.update(kwargs)
        return InstalledPlugin("demo", "1.0.0", tmp_path / "installed" / "demo")

    monkeypatch.setattr("ash.commands.extensions.install_git_plugin", install)

    assert main(["extensions", "install", "@alpha/demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    expected = observed["expected"]

    assert payload["name"] == "demo"
    assert observed["source"] == "https://plugins.example/demo.git"
    assert expected.publisher == "alpha"


def test_empty_registry_preserves_legacy_catalog_environment_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    legacy = _write_catalog(tmp_path / "legacy.json", private_key, publisher=None)
    monkeypatch.setenv("ASH_PLUGIN_CATALOG", str(legacy))

    assert main(["extensions", "search", "demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["sequence"] == 1
    assert "publisher" not in payload
    assert payload["plugins"][0]["name"] == "demo"

