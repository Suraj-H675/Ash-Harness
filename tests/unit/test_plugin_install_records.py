from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import ash.plugins.lifecycle as lifecycle
from ash.plugins.catalog import CatalogEntry
from ash.plugins.lifecycle import (
    PluginLifecycleError,
    install_git_plugin,
    install_local_plugin,
    load_plugin_install_records,
    uninstall_local_plugin,
)


def _git_plugin(
    root: Path,
    *,
    name: str = "demo",
    version: str = "1.0.0",
    tag: str = "v1.0.0",
) -> tuple[str, str]:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )
    (root / "README.md").write_text(f"{name} {version}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Ash",
            "-c",
            "user.email=ash@example.invalid",
            "commit",
            "-q",
            "-m",
            version,
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "tag", tag], check=True)
    digest = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root.as_uri(), digest


def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_managed_git_install_persists_exact_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    source, digest = _git_plugin(tmp_path / "repo")

    installed = install_git_plugin(source, ref="v1.0.0")
    records = load_plugin_install_records()

    assert installed.name == "demo"
    assert records["demo"].name == "demo"
    assert records["demo"].version == "1.0.0"
    assert records["demo"].source == source
    assert records["demo"].ref == "v1.0.0"
    assert records["demo"].digest == digest
    assert records["demo"].publisher is None
    assert records["demo"].origin == "git"


def test_managed_git_install_preserves_existing_long_version_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    version = "1+" + ("a" * 300)
    source, _ = _git_plugin(tmp_path / "repo", version=version)

    installed = install_git_plugin(source, ref="v1.0.0")

    assert installed.version == version
    assert load_plugin_install_records()["demo"].version == version


def test_managed_catalog_install_persists_signed_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    source, digest = _git_plugin(tmp_path / "repo")
    expected = CatalogEntry(
        name="demo",
        version="1.0.0",
        source=source,
        ref="v1.0.0",
        digest=digest,
        publisher="alpha",
    )

    install_git_plugin(source, ref="v1.0.0", expected=expected)

    assert load_plugin_install_records()["demo"].publisher == "alpha"
    assert load_plugin_install_records()["demo"].origin == "catalog"


def test_publisherless_catalog_install_is_still_tracked_as_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    source, digest = _git_plugin(tmp_path / "repo")
    expected = CatalogEntry(
        name="demo",
        version="1.0.0",
        source=source,
        ref="v1.0.0",
        digest=digest,
        publisher=None,
    )

    install_git_plugin(source, ref="v1.0.0", expected=expected)

    record = load_plugin_install_records()["demo"]
    assert record.publisher is None
    assert record.origin == "catalog"


def test_install_record_updates_preserve_other_managed_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    first_source, _ = _git_plugin(
        tmp_path / "first", name="first", version="1.0.0", tag="v1.0.0"
    )
    second_source, _ = _git_plugin(
        tmp_path / "second", name="second", version="2.0.0", tag="v2.0.0"
    )

    install_git_plugin(first_source, ref="v1.0.0")
    first_record = load_plugin_install_records()["first"]
    install_git_plugin(second_source, ref="v2.0.0")
    records = load_plugin_install_records()

    assert records["first"] == first_record
    assert records["second"].source == second_source
    assert set(records) == {"first", "second"}


def test_custom_destination_root_does_not_create_managed_provenance(
    tmp_path: Path,
) -> None:
    source, _ = _git_plugin(tmp_path / "repo")
    destination = tmp_path / "custom-plugins"

    install_git_plugin(source, ref="v1.0.0", destination_root=destination)

    assert not (destination / lifecycle.PLUGIN_INSTALL_RECORDS_FILENAME).exists()
    assert load_plugin_install_records(destination) == {}


def test_managed_local_replacement_clears_stale_git_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    source, _ = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref="v1.0.0")
    local = tmp_path / "local"
    local.mkdir()
    (local / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "2.0.0"}), encoding="utf-8"
    )

    install_local_plugin(local, replace=True)

    assert load_plugin_install_records() == {}
    manifest = json.loads(
        (lifecycle.user_plugin_root() / "demo" / "plugin.json").read_text()
    )
    assert manifest["version"] == "2.0.0"


def test_provenance_write_failure_rolls_back_plugin_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    first_source, first_digest = _git_plugin(tmp_path / "repo-one")
    second_source, _ = _git_plugin(
        tmp_path / "repo-two", version="2.0.0", tag="v2.0.0"
    )
    install_git_plugin(first_source, ref="v1.0.0")
    before = load_plugin_install_records()["demo"]

    def fail_save(*args, **kwargs):
        raise OSError("record write failed")

    monkeypatch.setattr(lifecycle, "_save_plugin_install_records_at", fail_save)

    with pytest.raises(PluginLifecycleError, match="record write failed"):
        install_git_plugin(second_source, ref="v2.0.0", replace=True)

    manifest = json.loads(
        (lifecycle.user_plugin_root() / "demo" / "plugin.json").read_text()
    )
    assert manifest["version"] == "1.0.0"
    assert before.digest == first_digest
    assert load_plugin_install_records()["demo"] == before


def test_uninstall_removes_managed_install_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    source, _ = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref="v1.0.0")

    uninstall_local_plugin("demo", confirmed=True)

    assert load_plugin_install_records() == {}
    assert not (lifecycle.user_plugin_root() / "demo").exists()


def test_uninstall_provenance_failure_restores_plugin_and_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    source, _ = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref="v1.0.0")
    before = load_plugin_install_records()["demo"]

    def fail_save(*args, **kwargs):
        raise OSError("record removal failed")

    monkeypatch.setattr(lifecycle, "_save_plugin_install_records_at", fail_save)

    with pytest.raises(PluginLifecycleError, match="record removal failed"):
        uninstall_local_plugin("demo", confirmed=True)

    assert (lifecycle.user_plugin_root() / "demo" / "plugin.json").is_file()
    assert load_plugin_install_records()["demo"] == before


def test_post_delete_sync_failure_does_not_restore_stale_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    source, _ = _git_plugin(tmp_path / "repo")
    install_git_plugin(source, ref="v1.0.0")
    root = lifecycle.user_plugin_root()
    original_sync = lifecycle.AnchoredDirectory.sync
    failed = False

    def fail_after_quarantine_removal(directory: lifecycle.AnchoredDirectory) -> None:
        nonlocal failed
        names = set(directory.list_names()) if directory.path == root else set()
        quarantine_present = any(name.startswith(".demo.uninstall-") for name in names)
        if (
            directory.path == root
            and "demo" not in names
            and not quarantine_present
            and not failed
        ):
            failed = True
            raise OSError("post-delete sync failed")
        original_sync(directory)

    monkeypatch.setattr(lifecycle.AnchoredDirectory, "sync", fail_after_quarantine_removal)

    with pytest.raises(PluginLifecycleError, match="post-delete sync failed"):
        uninstall_local_plugin("demo", confirmed=True)

    assert failed is True
    assert not (root / "demo").exists()
    assert load_plugin_install_records() == {}


def test_install_records_reject_symlinked_state_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = lifecycle.user_plugin_root()
    root.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"version": 1, "plugins": {}}), encoding="utf-8"
    )
    state = root / lifecycle.PLUGIN_INSTALL_RECORDS_FILENAME
    try:
        state.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(PluginLifecycleError, match="not a regular file"):
        load_plugin_install_records()


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 999, "plugins": {}},
        {"version": 1, "plugins": {"../escape": {}}},
        {
            "version": 1,
            "plugins": {
                "demo": {
                    "version": "1.0.0",
                    "source": "https://plugins.example/demo.git",
                    "ref": "main",
                    "digest": "not-a-git-digest",
                    "publisher": None,
                }
            },
        },
    ],
)
def test_install_records_reject_malformed_or_tampered_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = lifecycle.user_plugin_root()
    root.mkdir(parents=True)
    (root / lifecycle.PLUGIN_INSTALL_RECORDS_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(PluginLifecycleError, match="install record"):
        load_plugin_install_records()


def test_v1_install_records_migrate_origin_conservatively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    root = lifecycle.user_plugin_root()
    root.mkdir(parents=True)
    digest = "a" * 40
    payload = {
        "version": 1,
        "plugins": {
            "direct": {
                "version": "1.0.0",
                "source": "https://plugins.example/direct.git",
                "ref": "main",
                "digest": digest,
                "publisher": None,
            },
            "signed": {
                "version": "1.0.0",
                "source": "https://plugins.example/signed.git",
                "ref": "v1.0.0",
                "digest": digest,
                "publisher": "alpha",
            },
        },
    }
    (root / lifecycle.PLUGIN_INSTALL_RECORDS_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    records = load_plugin_install_records()

    assert records["direct"].origin == "legacy-unknown"
    assert records["signed"].origin == "catalog"
