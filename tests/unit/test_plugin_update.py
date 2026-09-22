from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import ash.commands.extensions as extension_commands
import ash.plugins.lifecycle as lifecycle
from ash.cli import main
from ash.plugins.catalog import sign_catalog
from ash.plugins.lifecycle import (
    install_git_plugin,
    install_local_plugin,
    load_extension_state,
    load_plugin_install_records,
    set_plugin_enabled,
    user_plugin_root,
)


def _git_env() -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": "Ash",
        "GIT_AUTHOR_EMAIL": "ash@example.invalid",
        "GIT_COMMITTER_NAME": "Ash",
        "GIT_COMMITTER_EMAIL": "ash@example.invalid",
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
    }


def _commit_plugin(
    root: Path,
    *,
    name: str = "demo",
    version: str,
    tag: str | None = None,
) -> str:
    (root / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )
    (root / "README.md").write_text(f"{name} {version}\n", encoding="utf-8")
    env = _git_env()
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", version],
        check=True,
        env=env,
    )
    if tag is not None:
        subprocess.run(["git", "-C", str(root), "tag", tag], check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _git_plugin(
    root: Path,
    *,
    name: str = "demo",
    version: str = "1.0.0",
    tag: str | None = None,
) -> tuple[str, str, str]:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    digest = _commit_plugin(root, name=name, version=version, tag=tag)
    branch = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root.as_uri(), branch, digest


def _write_keys(root: Path, private_key: Ed25519PrivateKey) -> Path:
    public_key = private_key.public_key().public_bytes_raw()
    path = root / "keys.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "keyId": "update-key",
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


def _write_catalog(
    path: Path,
    private_key: Ed25519PrivateKey,
    *,
    publisher: str,
    source: str,
    version: str,
    ref: str,
    digest: str,
    sequence: int,
) -> Path:
    payload = {
        "version": 2,
        "publisher": publisher,
        "sequence": sequence,
        "entries": [
            {
                "name": "demo",
                "version": version,
                "source": source,
                "ref": ref,
                "digest": digest,
            }
        ],
    }
    encoded_private = (
        base64.urlsafe_b64encode(private_key.private_bytes_raw()).rstrip(b"=").decode()
    )
    path.write_text(
        json.dumps(
            {
                "catalog": payload,
                "keyId": "update-key",
                "algorithm": "ed25519",
                "signature": sign_catalog(payload, encoded_private),
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_legacy_catalog(
    path: Path,
    private_key: Ed25519PrivateKey,
    *,
    source: str,
    version: str,
    ref: str,
    digest: str,
    sequence: int,
) -> Path:
    payload = {
        "version": 1,
        "sequence": sequence,
        "entries": [
            {
                "name": "demo",
                "version": version,
                "source": source,
                "ref": ref,
                "digest": digest,
            }
        ],
    }
    encoded_private = (
        base64.urlsafe_b64encode(private_key.private_bytes_raw()).rstrip(b"=").decode()
    )
    path.write_text(
        json.dumps(
            {
                "catalog": payload,
                "keyId": "update-key",
                "algorithm": "ed25519",
                "signature": sign_catalog(payload, encoded_private),
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    return home


def test_extensions_update_direct_git_branch_replaces_with_new_commit(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, branch, first_digest = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref=branch)
    second_digest = _commit_plugin(tmp_path / "repo", version="2.0.0")
    assert second_digest != first_digest

    assert main(["extensions", "update", "demo", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["action"] == "update"
    assert result["status"] == "updated"
    assert result["previous_version"] == "1.0.0"
    assert result["version"] == "2.0.0"
    assert result["previous_digest"] == first_digest
    assert result["digest"] == second_digest
    assert json.loads((user_plugin_root() / "demo" / "plugin.json").read_text())[
        "version"
    ] == "2.0.0"
    assert load_plugin_install_records()["demo"].digest == second_digest


def test_extensions_update_direct_git_unchanged_is_true_noop(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, branch, digest = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref=branch)
    root = user_plugin_root() / "demo"
    before_inode = root.stat().st_ino
    before_record = load_plugin_install_records()["demo"]

    assert main(["extensions", "update", "demo", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "unchanged"
    assert result["digest"] == digest
    assert root.stat().st_ino == before_inode
    assert load_plugin_install_records()["demo"] == before_record


def test_extensions_update_direct_git_unchanged_rejects_tampered_installed_tree(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, branch, _digest = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref=branch)
    root = user_plugin_root() / "demo"
    record = load_plugin_install_records()["demo"]
    (root / "README.md").write_text("locally tampered\n", encoding="utf-8")

    assert main(["extensions", "update", "demo"]) == 2
    captured = capsys.readouterr()

    assert "differs from trusted source" in captured.err
    assert (root / "README.md").read_text(encoding="utf-8") == "locally tampered\n"
    assert load_plugin_install_records()["demo"] == record


def test_extensions_update_changed_commit_rejects_stale_provenance_race(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, branch, _ = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref=branch)
    _commit_plugin(tmp_path / "repo", version="2.0.0")
    local = tmp_path / "local-race"
    local.mkdir()
    (local / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "9.0.0"}), encoding="utf-8"
    )
    original_resolve = lifecycle._resolve_git_revision
    raced = False

    def race_then_resolve(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            install_local_plugin(local, replace=True)
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_resolve_git_revision", race_then_resolve)

    assert main(["extensions", "update", "demo"]) == 2
    captured = capsys.readouterr()

    assert raced is True
    assert "changed while update was in progress" in captured.err
    assert json.loads((user_plugin_root() / "demo" / "plugin.json").read_text())[
        "version"
    ] == "9.0.0"
    assert load_plugin_install_records() == {}


def test_extensions_update_unchanged_commit_rejects_stale_provenance_race(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, branch, _ = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref=branch)
    local = tmp_path / "local-race"
    local.mkdir()
    (local / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "9.0.0"}), encoding="utf-8"
    )
    original_resolve = lifecycle._resolve_git_revision
    raced = False

    def race_then_resolve(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            install_local_plugin(local, replace=True)
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_resolve_git_revision", race_then_resolve)

    assert main(["extensions", "update", "demo"]) == 2
    captured = capsys.readouterr()

    assert raced is True
    assert "changed while update was in progress" in captured.err
    assert json.loads((user_plugin_root() / "demo" / "plugin.json").read_text())[
        "version"
    ] == "9.0.0"
    assert load_plugin_install_records() == {}


def test_extensions_update_preserves_disabled_state(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, branch, _ = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref=branch)
    set_plugin_enabled("demo", enabled=False)
    _commit_plugin(tmp_path / "repo", version="2.0.0")

    assert main(["extensions", "update", "demo", "--json"]) == 0
    capsys.readouterr()

    assert "demo" in load_extension_state().disabled_plugins


def test_extensions_update_rejects_installed_version_drift(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, branch, _ = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref=branch)
    manifest = user_plugin_root() / "demo" / "plugin.json"
    manifest.write_text(
        json.dumps({"name": "demo", "version": "9.0.0"}), encoding="utf-8"
    )

    assert main(["extensions", "update", "demo"]) == 2
    captured = capsys.readouterr()

    assert "does not match installed plugin version" in captured.err
    assert load_plugin_install_records()["demo"].version == "1.0.0"


def test_extensions_update_rejects_untracked_local_plugin(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    (local / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8"
    )
    install_local_plugin(local)

    assert main(["extensions", "update", "demo", "--json"]) == 2
    captured = capsys.readouterr()

    assert "not tracked" in captured.err.casefold()
    assert json.loads((user_plugin_root() / "demo" / "plugin.json").read_text())[
        "version"
    ] == "1.0.0"


def test_extensions_update_fails_closed_for_ambiguous_v1_provenance(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, branch, digest = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref=branch)
    records_path = user_plugin_root() / ".ash-install-records.json"
    records_path.write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    "demo": {
                        "version": "1.0.0",
                        "source": source,
                        "ref": branch,
                        "digest": digest,
                        "publisher": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert main(["extensions", "update", "demo"]) == 2
    captured = capsys.readouterr()

    assert "legacy install provenance" in captured.err
    assert load_plugin_install_records()["demo"].origin == "legacy-unknown"


def test_extensions_update_direct_git_rejects_candidate_identity_change(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, branch, _ = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref=branch)
    before_record = load_plugin_install_records()["demo"]
    _commit_plugin(tmp_path / "repo", name="other", version="2.0.0")

    assert main(["extensions", "update", "demo", "--json"]) == 2
    captured = capsys.readouterr()

    assert "does not match" in captured.err.casefold()
    assert not (user_plugin_root() / "other").exists()
    assert json.loads((user_plugin_root() / "demo" / "plugin.json").read_text())[
        "version"
    ] == "1.0.0"
    assert load_plugin_install_records()["demo"] == before_record


def test_extensions_update_signed_publisher_uses_current_verified_entry(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _branch, first_digest = _git_plugin(
        tmp_path / "repo", tag="v1.0.0"
    )
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(
        tmp_path / "catalog.json",
        private_key,
        publisher="alpha",
        source=source,
        version="1.0.0",
        ref="v1.0.0",
        digest=first_digest,
        sequence=1,
    )
    assert (
        main(
            [
                "extensions",
                "install",
                "@alpha/demo",
                "--catalog",
                str(catalog),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    second_digest = _commit_plugin(
        tmp_path / "repo", version="2.0.0", tag="v2.0.0"
    )
    _write_catalog(
        catalog,
        private_key,
        publisher="alpha",
        source=source,
        version="2.0.0",
        ref="v2.0.0",
        digest=second_digest,
        sequence=2,
    )

    assert (
        main(
            [
                "extensions",
                "update",
                "demo",
                "--catalog",
                str(catalog),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "updated"
    assert result["publisher"] == "alpha"
    assert result["version"] == "2.0.0"
    record = load_plugin_install_records()["demo"]
    assert record.publisher == "alpha"
    assert record.ref == "v2.0.0"
    assert record.digest == second_digest


def test_extensions_update_uses_registered_marketplace_without_catalog_flag(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _branch, first_digest = _git_plugin(
        tmp_path / "repo", tag="v1.0.0"
    )
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(
        tmp_path / "catalog.json",
        private_key,
        publisher="alpha",
        source=source,
        version="1.0.0",
        ref="v1.0.0",
        digest=first_digest,
        sequence=1,
    )
    assert main(["marketplace", "add", str(catalog)]) == 0
    capsys.readouterr()
    assert main(["extensions", "install", "@alpha/demo"]) == 0
    capsys.readouterr()
    second_digest = _commit_plugin(
        tmp_path / "repo", version="2.0.0", tag="v2.0.0"
    )
    _write_catalog(
        catalog,
        private_key,
        publisher="alpha",
        source=source,
        version="2.0.0",
        ref="v2.0.0",
        digest=second_digest,
        sequence=2,
    )

    assert main(["extensions", "update", "demo", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "updated"
    assert result["publisher"] == "alpha"
    assert result["digest"] == second_digest


def test_extensions_update_signed_publisher_unchanged_is_noop(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _branch, digest = _git_plugin(tmp_path / "repo", tag="v1.0.0")
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(
        tmp_path / "catalog.json",
        private_key,
        publisher="alpha",
        source=source,
        version="1.0.0",
        ref="v1.0.0",
        digest=digest,
        sequence=1,
    )
    assert (
        main(
            [
                "extensions",
                "install",
                "@alpha/demo",
                "--catalog",
                str(catalog),
            ]
        )
        == 0
    )
    capsys.readouterr()
    root = user_plugin_root() / "demo"
    before_inode = root.stat().st_ino

    assert (
        main(
            [
                "extensions",
                "update",
                "demo",
                "--catalog",
                str(catalog),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "unchanged"
    assert root.stat().st_ino == before_inode


def test_extensions_update_catalog_noop_rejects_stale_provenance_race(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _branch, digest = _git_plugin(tmp_path / "repo", tag="v1.0.0")
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(
        tmp_path / "catalog.json",
        private_key,
        publisher="alpha",
        source=source,
        version="1.0.0",
        ref="v1.0.0",
        digest=digest,
        sequence=1,
    )
    assert (
        main(["extensions", "install", "@alpha/demo", "--catalog", str(catalog)])
        == 0
    )
    capsys.readouterr()
    local = tmp_path / "local-race"
    local.mkdir()
    (local / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "9.0.0"}), encoding="utf-8"
    )
    original_lookup = extension_commands.catalog_entry_for_name
    raced = False

    def race_then_lookup(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            install_local_plugin(local, replace=True)
        return original_lookup(*args, **kwargs)

    monkeypatch.setattr(extension_commands, "catalog_entry_for_name", race_then_lookup)

    assert (
        main(
            [
                "extensions",
                "update",
                "demo",
                "--catalog",
                str(catalog),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()

    assert raced is True
    assert "changed while update was in progress" in captured.err
    assert json.loads((user_plugin_root() / "demo" / "plugin.json").read_text())[
        "version"
    ] == "9.0.0"
    assert load_plugin_install_records() == {}


def test_extensions_update_catalog_noop_rejects_tampered_installed_tree(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _branch, digest = _git_plugin(tmp_path / "repo", tag="v1.0.0")
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(
        tmp_path / "catalog.json",
        private_key,
        publisher="alpha",
        source=source,
        version="1.0.0",
        ref="v1.0.0",
        digest=digest,
        sequence=1,
    )
    assert (
        main(["extensions", "install", "@alpha/demo", "--catalog", str(catalog)])
        == 0
    )
    capsys.readouterr()
    root = user_plugin_root() / "demo"
    record = load_plugin_install_records()["demo"]
    (root / "README.md").write_text("locally tampered\n", encoding="utf-8")

    assert (
        main(
            [
                "extensions",
                "update",
                "demo",
                "--catalog",
                str(catalog),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()

    assert "differs from trusted source" in captured.err
    assert (root / "README.md").read_text(encoding="utf-8") == "locally tampered\n"
    assert load_plugin_install_records()["demo"] == record


def test_extensions_update_publisherless_signed_v1_catalog_stays_verified(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _branch, first_digest = _git_plugin(
        tmp_path / "repo", tag="v1.0.0"
    )
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_legacy_catalog(
        tmp_path / "catalog-v1.json",
        private_key,
        source=source,
        version="1.0.0",
        ref="v1.0.0",
        digest=first_digest,
        sequence=1,
    )
    assert (
        main(
            [
                "extensions",
                "install",
                "demo",
                "--catalog",
                str(catalog),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert load_plugin_install_records()["demo"].origin == "catalog"
    second_digest = _commit_plugin(
        tmp_path / "repo", version="2.0.0", tag="v2.0.0"
    )
    _write_legacy_catalog(
        catalog,
        private_key,
        source=source,
        version="2.0.0",
        ref="v2.0.0",
        digest=second_digest,
        sequence=2,
    )

    assert (
        main(
            [
                "extensions",
                "update",
                "demo",
                "--catalog",
                str(catalog),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "updated"
    assert "publisher" not in result
    record = load_plugin_install_records()["demo"]
    assert record.origin == "catalog"
    assert record.publisher is None
    assert record.ref == "v2.0.0"
    assert record.digest == second_digest


def test_extensions_update_publisher_drift_fails_without_mutation(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _branch, digest = _git_plugin(tmp_path / "repo", tag="v1.0.0")
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(_write_keys(tmp_path, private_key)))
    catalog = _write_catalog(
        tmp_path / "catalog.json",
        private_key,
        publisher="alpha",
        source=source,
        version="1.0.0",
        ref="v1.0.0",
        digest=digest,
        sequence=1,
    )
    assert (
        main(["extensions", "install", "@alpha/demo", "--catalog", str(catalog)])
        == 0
    )
    capsys.readouterr()
    before_record = load_plugin_install_records()["demo"]
    before_inode = (user_plugin_root() / "demo").stat().st_ino
    _write_catalog(
        catalog,
        private_key,
        publisher="beta",
        source=source,
        version="1.0.0",
        ref="v1.0.0",
        digest=digest,
        sequence=2,
    )

    assert (
        main(
            [
                "extensions",
                "update",
                "demo",
                "--catalog",
                str(catalog),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()

    assert "alpha" in captured.err.casefold()
    assert (user_plugin_root() / "demo").stat().st_ino == before_inode
    assert load_plugin_install_records()["demo"] == before_record


def test_extensions_update_rejects_install_only_flags(capsys) -> None:
    assert main(["extensions", "update", "demo", "--replace"]) == 2
    assert "--replace" in capsys.readouterr().err
    assert main(["extensions", "update", "demo", "--ref", "main"]) == 2
    assert "--ref" in capsys.readouterr().err


def test_extensions_update_all_empty_is_successful_noop(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["extensions", "update", "--all", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "action": "update-all",
        "errors": 0,
        "results": [],
        "unchanged": 0,
        "updated": 0,
    }


def test_extensions_update_all_empty_human_summary(
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["extensions", "update", "--all"]) == 0
    output = capsys.readouterr().out

    assert "No tracked plugins to update." in output
    assert "0 updated, 0 unchanged, 0 failed" in output


def test_bulk_update_human_errors_are_terminal_safe() -> None:
    rendered = extension_commands.render_plugin_update_all(
        {
            "action": "update-all",
            "updated": 0,
            "unchanged": 0,
            "errors": 1,
            "results": [
                {
                    "action": "update",
                    "name": "demo",
                    "status": "error",
                    "error": "bad\x1b]8;;https://evil.example\x07link\x1b]8;;\x07",
                }
            ],
        },
        json_output=False,
    )

    assert "\x1b" not in rendered
    assert "Failed to update demo:" in rendered


def test_extensions_update_all_is_deterministic_and_reports_each_outcome(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    beta_source, beta_branch, beta_digest = _git_plugin(
        tmp_path / "beta-repo", name="beta"
    )
    alpha_source, alpha_branch, alpha_digest = _git_plugin(
        tmp_path / "alpha-repo", name="alpha"
    )
    install_git_plugin(beta_source, ref=beta_branch)
    install_git_plugin(alpha_source, ref=alpha_branch)
    alpha_new_digest = _commit_plugin(
        tmp_path / "alpha-repo", name="alpha", version="2.0.0"
    )

    assert main(["extensions", "update", "--all", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [item["name"] for item in payload["results"]] == ["alpha", "beta"]
    assert [item["status"] for item in payload["results"]] == [
        "updated",
        "unchanged",
    ]
    assert payload["updated"] == 1
    assert payload["unchanged"] == 1
    assert payload["errors"] == 0
    records = load_plugin_install_records()
    assert records["alpha"].digest == alpha_new_digest
    assert records["beta"].digest == beta_digest
    assert alpha_new_digest != alpha_digest


def test_extensions_update_all_continues_after_error_and_returns_failure(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    alpha_source, alpha_branch, _ = _git_plugin(
        tmp_path / "alpha-repo", name="alpha"
    )
    beta_source, beta_branch, _ = _git_plugin(
        tmp_path / "beta-repo", name="beta"
    )
    install_git_plugin(alpha_source, ref=alpha_branch)
    install_git_plugin(beta_source, ref=beta_branch)
    (user_plugin_root() / "alpha" / "plugin.json").write_text(
        json.dumps({"name": "alpha", "version": "9.0.0"}), encoding="utf-8"
    )
    beta_new_digest = _commit_plugin(
        tmp_path / "beta-repo", name="beta", version="2.0.0"
    )

    assert main(["extensions", "update", "--all", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert [item["name"] for item in payload["results"]] == ["alpha", "beta"]
    assert payload["results"][0]["status"] == "error"
    assert "does not match installed plugin version" in payload["results"][0]["error"]
    assert payload["results"][1]["status"] == "updated"
    assert payload["updated"] == 1
    assert payload["unchanged"] == 0
    assert payload["errors"] == 1
    assert load_plugin_install_records()["beta"].digest == beta_new_digest


def test_extensions_update_all_ignores_untracked_local_plugins(
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    (local / "plugin.json").write_text(
        json.dumps({"name": "local-only", "version": "1.0.0"}), encoding="utf-8"
    )
    install_local_plugin(local)

    assert main(["extensions", "update", "--all", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["results"] == []
    assert (user_plugin_root() / "local-only" / "plugin.json").is_file()


def test_extensions_update_all_rejects_target_and_wrong_actions(capsys) -> None:
    assert main(["extensions", "update", "demo", "--all"]) == 2
    assert "--all" in capsys.readouterr().err
    assert main(["extensions", "install", "demo", "--all"]) == 2
    assert "--all" in capsys.readouterr().err
