from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ash.plugins.catalog import (
    PluginCatalogError,
    CatalogEntry,
    generate_catalog_signing_key,
    parse_and_verify_catalog,
    sign_catalog,
)
from ash.plugins.lifecycle import install_git_plugin


def _write_catalog(
    path: Path,
    *,
    private_key: str | None = None,
    key_id: str = "ash-catalog-key",
    sequence: int = 1,
    mutate_catalog: bool = False,
) -> dict:
    if private_key is None:
        private_key, public_key, key_id = generate_catalog_signing_key()
        keys_path = path.parent / "keys.json"
        keys_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "keys": [
                        {
                            "keyId": key_id,
                            "algorithm": "ed25519",
                            "publicKey": public_key,
                        }
                    ],
                }
            )
        )
    else:
        keys_path = path.parent / "keys.json"
    source = "https://plugins.example/demo.git"
    digest = "0" * 64
    catalog = {
        "version": 1,
        "sequence": sequence,
        "entries": [
            {
                "name": "demo",
                "version": "1.2.3",
                "source": source,
                "ref": "v1.2.3",
                "digest": digest,
            }
        ],
    }
    signature = sign_catalog(catalog, private_key)
    if mutate_catalog:
        catalog["sequence"] = sequence + 1
    path.write_text(
        json.dumps(
            {
                "catalog": catalog,
                "keyId": key_id,
                "algorithm": "ed25519",
                "signature": signature,
            }
        )
    )
    return {"catalog_path": path, "keys_path": keys_path}


def test_signed_catalog_round_trip(tmp_path: Path) -> None:
    files = _write_catalog(tmp_path / "catalog.json")
    verified = parse_and_verify_catalog(
        files["catalog_path"], trusted_keys_path=files["keys_path"]
    )
    assert verified.sequence == 1
    assert verified.entries["demo"].name == "demo"


def test_rejects_modified_catalog_after_signing(tmp_path: Path) -> None:
    files = _write_catalog(tmp_path / "catalog.json", mutate_catalog=True)
    with pytest.raises(PluginCatalogError, match="signature is invalid"):
        parse_and_verify_catalog(
            files["catalog_path"], trusted_keys_path=files["keys_path"]
        )


def test_rejects_unknown_key(tmp_path: Path) -> None:
    files = _write_catalog(tmp_path / "catalog.json")
    other_private = Ed25519PrivateKey.generate()
    other_public = other_private.public_key().public_bytes_raw()
    (tmp_path / "other-keys.json").write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "keyId": "other-key",
                        "algorithm": "ed25519",
                        "publicKey": base64.urlsafe_b64encode(other_public)
                        .rstrip(b"=")
                        .decode(),
                    }
                ],
            }
        )
    )
    with pytest.raises(PluginCatalogError, match="unknown plugin catalog signing key"):
        parse_and_verify_catalog(
            files["catalog_path"], trusted_keys_path=tmp_path / "other-keys.json"
        )


def test_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    files = _write_catalog(tmp_path / "catalog.json")
    raw = files["catalog_path"].read_text()
    raw = raw[:-1] + ',"keyId":"duplicate"}'
    files["catalog_path"].write_text(raw)
    with pytest.raises(PluginCatalogError, match="duplicate JSON object key"):
        parse_and_verify_catalog(
            files["catalog_path"], trusted_keys_path=files["keys_path"]
        )


def test_git_install_verifies_catalog_revision(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.2.3",
                "description": "Demo plugin",
            }
        )
    )
    subprocess.run(["git", "init", "-q"], cwd=plugin_root, check=True)
    subprocess.run(
        ["git", "-C", str(plugin_root), "add", "plugin.json"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(plugin_root),
            "-c",
            "user.name=Ash",
            "-c",
            "user.email=ash@example.invalid",
            "commit",
            "-m",
            "demo",
        ],
        check=True,
    )
    digest = subprocess.run(
        ["git", "-C", str(plugin_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(plugin_root), "tag", "v1.2.3"],
        check=True,
    )
    subprocess.run(
        ["git", "clone", "-q", str(plugin_root), str(repository)], check=True
    )

    source = repository.as_uri()
    expected = CatalogEntry(
        name="demo",
        version="1.2.3",
        source=source,
        ref="v1.2.3",
        digest=digest,
    )

    installed = install_git_plugin(
        source,
        ref="v1.2.3",
        destination_root=tmp_path / "installed",
        replace=False,
        validator=lambda root, manifest: None,
        expected=expected,
    )

    assert installed.name == "demo"

    bad_entry = CatalogEntry(
        name="demo",
        version="1.2.3",
        source=source,
        ref="v1.2.3",
        digest="f" * 64,
    )
    with pytest.raises(PluginCatalogError, match="digest does not match"):
        install_git_plugin(
            source,
            ref="v1.2.3",
            destination_root=tmp_path / "installed-bad",
            replace=False,
            validator=lambda root, manifest: None,
            expected=bad_entry,
        )
