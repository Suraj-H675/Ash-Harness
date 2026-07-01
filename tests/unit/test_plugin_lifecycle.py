from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plugins.lifecycle import (
    PluginLifecycleError,
    install_local_plugin,
    load_extension_state,
    set_plugin_enabled,
    uninstall_local_plugin,
)


def _plugin(root: Path, name: str = "example", version: str = "1.0.0") -> Path:
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )
    (root / "README.md").write_text("plugin contents", encoding="utf-8")
    return root


def test_install_local_plugin_copies_validated_tree(tmp_path) -> None:
    source = _plugin(tmp_path / "source")
    destination_root = tmp_path / "installed"

    installed = install_local_plugin(source, destination_root=destination_root)

    assert installed.name == "example"
    assert installed.version == "1.0.0"
    assert installed.root == destination_root / "example"
    assert (installed.root / "README.md").read_text() == "plugin contents"


def test_install_local_plugin_requires_replace_and_updates_atomically(tmp_path) -> None:
    destination_root = tmp_path / "installed"
    install_local_plugin(
        _plugin(tmp_path / "first", version="1.0.0"),
        destination_root=destination_root,
    )
    source = _plugin(tmp_path / "second", version="2.0.0")

    with pytest.raises(PluginLifecycleError, match="already installed"):
        install_local_plugin(source, destination_root=destination_root)

    installed = install_local_plugin(
        source, destination_root=destination_root, replace=True
    )
    assert installed.version == "2.0.0"
    payload = json.loads((installed.root / "plugin.json").read_text())
    assert payload["version"] == "2.0.0"


def test_install_local_plugin_rejects_symlinks(tmp_path) -> None:
    source = _plugin(tmp_path / "source")
    target = tmp_path / "outside"
    target.write_text("secret")
    try:
        (source / "linked").symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(PluginLifecycleError, match="contains a link"):
        install_local_plugin(source, destination_root=tmp_path / "installed")


def test_install_local_plugin_rejects_linked_source_directory(tmp_path) -> None:
    source = _plugin(tmp_path / "source")
    linked = tmp_path / "linked-source"
    try:
        linked.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(PluginLifecycleError, match="source cannot be a link"):
        install_local_plugin(linked, destination_root=tmp_path / "installed")


@pytest.mark.parametrize("name", [".", "bad name", "../outside", "bad/name"])
def test_install_local_plugin_rejects_unsafe_names(tmp_path, name: str) -> None:
    source = _plugin(tmp_path / "source", name=name)

    with pytest.raises(PluginLifecycleError, match="invalid plugin manifest"):
        install_local_plugin(source, destination_root=tmp_path / "installed")


def test_enable_disable_state_is_atomic_and_private(tmp_path) -> None:
    state_path = tmp_path / "ash" / "extensions.json"

    disabled = set_plugin_enabled("example", enabled=False, path=state_path)
    assert disabled.disabled_plugins == frozenset({"example"})
    assert load_extension_state(state_path) == disabled
    enabled = set_plugin_enabled("example", enabled=True, path=state_path)
    assert enabled.disabled_plugins == frozenset()
    if os.name != "nt":
        assert state_path.stat().st_mode & 0o777 == 0o600


def test_uninstall_requires_confirmation_and_clears_disabled_state(tmp_path) -> None:
    destination_root = tmp_path / "installed"
    state_path = tmp_path / "extensions.json"
    install_local_plugin(
        _plugin(tmp_path / "source"), destination_root=destination_root
    )
    set_plugin_enabled("example", enabled=False, path=state_path)

    with pytest.raises(PluginLifecycleError, match="confirmation"):
        uninstall_local_plugin(
            "example",
            destination_root=destination_root,
            state_path=state_path,
        )

    removed = uninstall_local_plugin(
        "example",
        destination_root=destination_root,
        confirmed=True,
        state_path=state_path,
    )
    assert not removed.exists()
    assert load_extension_state(state_path).disabled_plugins == frozenset()


def test_invalid_extension_state_is_rejected(tmp_path) -> None:
    state_path = tmp_path / "extensions.json"
    state_path.write_text('{"version": 999}', encoding="utf-8")

    with pytest.raises(PluginLifecycleError, match="invalid extension state"):
        load_extension_state(state_path)
