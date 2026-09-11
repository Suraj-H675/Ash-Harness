from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading

import pytest

import ash.plugins.anchored_fs as anchored_fs
import ash.commands.extensions as extensions
import ash.plugins.lifecycle as lifecycle
from ash.commands.extensions import discover_extensions
from ash.plugins.anchored_fs import AnchoredDirectory
from ash.plugins.lifecycle import (
    MAX_EXTENSION_STATE_BYTES,
    ExtensionState,
    PluginLifecycleError,
    install_git_plugin,
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


def _swap_ancestor(
    visible_parent: Path,
    displaced_parent: Path,
    outside_parent: Path,
) -> None:
    visible_parent.rename(displaced_parent)
    visible_parent.symlink_to(outside_parent, target_is_directory=True)


def _require_anchored_platform() -> None:
    if not anchored_fs.supports_anchored_mutation():
        pytest.skip("descriptor-relative lifecycle mutation is unavailable")


def test_install_local_plugin_copies_validated_tree(tmp_path) -> None:
    source = _plugin(tmp_path / "source")
    destination_root = tmp_path / "installed"
    if os.name != "nt":
        source.chmod(0o755)

    installed = install_local_plugin(source, destination_root=destination_root)

    assert installed.name == "example"
    assert installed.version == "1.0.0"
    assert installed.root == destination_root / "example"
    assert (installed.root / "README.md").read_text() == "plugin contents"
    if os.name != "nt":
        assert source.stat().st_mode & 0o777 == 0o755


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


def test_plugin_lifecycle_rejects_symlinked_user_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    try:
        (home / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    monkeypatch.setenv("HOME", str(home))
    source = _plugin(tmp_path / "source", name="example")

    with pytest.raises(PluginLifecycleError, match="cannot traverse a link"):
        install_local_plugin(source)
    with pytest.raises(PluginLifecycleError, match="cannot traverse a link"):
        set_plugin_enabled("example", enabled=False)

    assert not (outside / "plugins").exists()
    assert not (outside / "extensions.json").exists()


def test_uninstall_rejects_symlinked_plugin_destination_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _plugin(outside / "example", name="example")
    linked_root = tmp_path / "plugins"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(PluginLifecycleError, match="cannot traverse a link"):
        uninstall_local_plugin(
            "example",
            destination_root=linked_root,
            confirmed=True,
            state_path=tmp_path / "extensions.json",
        )

    assert victim.is_dir()
    assert (victim / "README.md").read_text(encoding="utf-8") == "plugin contents"


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
        assert state_path.parent.stat().st_mode & 0o777 == 0o700
        assert state_path.stat().st_mode & 0o777 == 0o600


def test_loading_extension_state_does_not_change_directory_mode(tmp_path) -> None:
    _require_anchored_platform()
    state_path = tmp_path / "state" / "extensions.json"
    state_path.parent.mkdir()
    state_path.parent.chmod(0o755)
    state_path.write_text(
        json.dumps({"version": 1, "disabled_plugins": ["example"]}),
        encoding="utf-8",
    )

    assert load_extension_state(state_path).disabled_plugins == frozenset({"example"})
    assert state_path.parent.stat().st_mode & 0o777 == 0o755


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


def test_extension_state_rejects_duplicate_json_keys(tmp_path) -> None:
    state_path = tmp_path / "extensions.json"
    state_path.write_text(
        '{"version":1,"disabled_plugins":["first"],"disabled_plugins":[]}',
        encoding="utf-8",
    )

    with pytest.raises(PluginLifecycleError, match="duplicate JSON object key"):
        load_extension_state(state_path)


def test_oversized_extension_state_is_rejected(tmp_path) -> None:
    state_path = tmp_path / "extensions.json"
    state_path.write_bytes(b" " * (MAX_EXTENSION_STATE_BYTES + 1))

    with pytest.raises(PluginLifecycleError, match="exceeds"):
        load_extension_state(state_path)


def test_install_requires_local_plugin_dependencies_first(tmp_path) -> None:
    destination_root = tmp_path / "installed"
    dependent = _plugin(tmp_path / "dependent", name="dependent")
    (dependent / "plugin.json").write_text(
        json.dumps(
            {
                "name": "dependent",
                "dependencies": [{"name": "base", "version": ">=1.0"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PluginLifecycleError, match="Missing dependency: base"):
        install_local_plugin(dependent, destination_root=destination_root)

    install_local_plugin(
        _plugin(tmp_path / "base", name="base"),
        destination_root=destination_root,
    )
    installed = install_local_plugin(dependent, destination_root=destination_root)
    assert installed.name == "dependent"


def test_concurrent_no_replace_installs_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    destination_root = tmp_path / "installed"
    first_source = _plugin(tmp_path / "first", version="1.0.0")
    second_source = _plugin(tmp_path / "second", version="2.0.0")
    original_lock = AnchoredDirectory.lock
    first_acquired = threading.Event()
    second_attempted = threading.Event()
    release_first = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    @contextmanager
    def controlled_lock(directory: AnchoredDirectory, name: str):
        nonlocal calls
        with call_lock:
            calls += 1
            call = calls
        if call == 1:
            with original_lock(directory, name):
                first_acquired.set()
                if not release_first.wait(5):
                    raise AssertionError("timed out waiting to release first install")
                yield
        else:
            second_attempted.set()
            with original_lock(directory, name):
                yield

    monkeypatch.setattr(AnchoredDirectory, "lock", controlled_lock)
    results: list[tuple[str, str]] = []
    results_lock = threading.Lock()

    def install(source: Path) -> None:
        try:
            installed = install_local_plugin(source, destination_root=destination_root)
            result = ("success", installed.version)
        except PluginLifecycleError as exc:
            result = ("error", str(exc))
        with results_lock:
            results.append(result)

    first = threading.Thread(target=install, args=(first_source,))
    second = threading.Thread(target=install, args=(second_source,))
    first.start()
    assert first_acquired.wait(5)
    second.start()
    assert second_attempted.wait(5)
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(result[0] for result in results) == ["error", "success"]
    assert any(
        "already installed" in result[1] for result in results if result[0] == "error"
    )
    assert json.loads((destination_root / "example" / "plugin.json").read_text())[
        "version"
    ] in {"1.0.0", "2.0.0"}


def test_concurrent_state_updates_preserve_both_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    state_path = tmp_path / "state" / "extensions.json"
    original_lock = AnchoredDirectory.lock
    first_acquired = threading.Event()
    second_attempted = threading.Event()
    release_first = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    @contextmanager
    def controlled_lock(directory: AnchoredDirectory, name: str):
        nonlocal calls
        with call_lock:
            calls += 1
            call = calls
        if call == 1:
            with original_lock(directory, name):
                first_acquired.set()
                if not release_first.wait(5):
                    raise AssertionError(
                        "timed out waiting to release first state update"
                    )
                yield
        else:
            second_attempted.set()
            with original_lock(directory, name):
                yield

    monkeypatch.setattr(AnchoredDirectory, "lock", controlled_lock)
    results: list[ExtensionState] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def disable(name: str) -> None:
        try:
            state = set_plugin_enabled(name, enabled=False, path=state_path)
            with results_lock:
                results.append(state)
        except BaseException as exc:  # pragma: no cover - diagnostic assertion
            with results_lock:
                errors.append(exc)

    first = threading.Thread(target=disable, args=("alpha",))
    second = threading.Thread(target=disable, args=("beta",))
    first.start()
    assert first_acquired.wait(5)
    second.start()
    assert second_attempted.wait(5)
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert len(results) == 2
    assert load_extension_state(state_path).disabled_plugins == frozenset(
        {"alpha", "beta"}
    )


def test_install_ancestor_swap_cannot_redirect_staged_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    source = _plugin(tmp_path / "source")
    (source / "plugin.json").write_text(
        json.dumps({"name": "example", "skills": ["README.md"]}),
        encoding="utf-8",
    )
    visible_parent = tmp_path / "container"
    destination_root = visible_parent / "plugins"
    visible_parent.mkdir()
    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    displaced_parent = tmp_path / "displaced"
    swapped = False
    original_create_child = AnchoredDirectory.create_child

    def swap_before_staging(
        directory: AnchoredDirectory, name: str
    ) -> AnchoredDirectory:
        nonlocal swapped
        if directory.path == destination_root and not swapped:
            _swap_ancestor(visible_parent, displaced_parent, outside_parent)
            swapped = True
        return original_create_child(directory, name)

    monkeypatch.setattr(AnchoredDirectory, "create_child", swap_before_staging)

    observed: list[Path] = []

    def validate_staged(root: Path, manifest: object) -> None:
        del manifest
        observed.append(root)
        assert (root / "README.md").read_text(encoding="utf-8") == "plugin contents"

    with pytest.raises(PluginLifecycleError, match="displaced"):
        install_local_plugin(
            source,
            destination_root=destination_root,
            validator=validate_staged,
        )

    assert swapped
    assert observed
    assert observed[0].is_relative_to(displaced_parent)
    assert not (outside_parent / "plugins" / "example").exists()
    assert not (displaced_parent / "plugins" / "example").exists()


def test_replace_ancestor_swap_keeps_outside_plugin_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    visible_parent = tmp_path / "container"
    destination_root = visible_parent / "plugins"
    visible_parent.mkdir()
    install_local_plugin(
        _plugin(tmp_path / "first", version="1.0.0"),
        destination_root=destination_root,
    )
    outside_parent = tmp_path / "outside"
    outside_plugin = outside_parent / "plugins" / "example"
    outside_plugin.parent.mkdir(parents=True)
    _plugin(outside_plugin, version="outside")
    displaced_parent = tmp_path / "displaced"
    swapped = False
    original_create_child = AnchoredDirectory.create_child

    def swap_before_staging(
        directory: AnchoredDirectory, name: str
    ) -> AnchoredDirectory:
        nonlocal swapped
        if directory.path == destination_root and not swapped:
            _swap_ancestor(visible_parent, displaced_parent, outside_parent)
            swapped = True
        return original_create_child(directory, name)

    monkeypatch.setattr(AnchoredDirectory, "create_child", swap_before_staging)

    with pytest.raises(PluginLifecycleError, match="displaced"):
        install_local_plugin(
            _plugin(tmp_path / "second", version="2.0.0"),
            destination_root=destination_root,
            replace=True,
        )

    assert swapped
    assert (
        json.loads((outside_plugin / "plugin.json").read_text())["version"] == "outside"
    )
    assert (
        json.loads(
            (displaced_parent / "plugins" / "example" / "plugin.json").read_text()
        )["version"]
        == "1.0.0"
    )


def test_uninstall_ancestor_swap_cannot_delete_outside_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    visible_parent = tmp_path / "container"
    destination_root = visible_parent / "plugins"
    visible_parent.mkdir()
    install_local_plugin(
        _plugin(tmp_path / "source"),
        destination_root=destination_root,
    )
    outside_parent = tmp_path / "outside"
    outside_plugin = outside_parent / "plugins" / "example"
    outside_plugin.parent.mkdir(parents=True)
    _plugin(outside_plugin, version="outside")
    displaced_parent = tmp_path / "displaced"
    state_path = tmp_path / "state" / "extensions.json"
    swapped = False
    original_child = AnchoredDirectory.child

    def swap_before_manifest_open(
        directory: AnchoredDirectory,
        name: str,
        *,
        create: bool = False,
        expected: os.stat_result | None = None,
    ) -> AnchoredDirectory:
        nonlocal swapped
        if directory.path == destination_root and name == "example" and not swapped:
            _swap_ancestor(visible_parent, displaced_parent, outside_parent)
            swapped = True
        return original_child(directory, name, create=create, expected=expected)

    monkeypatch.setattr(AnchoredDirectory, "child", swap_before_manifest_open)

    uninstall_local_plugin(
        "example",
        destination_root=destination_root,
        confirmed=True,
        state_path=state_path,
    )

    assert swapped
    assert not (displaced_parent / "plugins" / "example").exists()
    assert (outside_plugin / "plugin.json").is_file()
    assert (
        json.loads((outside_plugin / "plugin.json").read_text())["version"] == "outside"
    )


def test_extension_state_ancestor_swap_stays_on_held_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    visible_parent = tmp_path / "container"
    state_directory = visible_parent / "state"
    state_path = state_directory / "extensions.json"
    visible_parent.mkdir()
    outside_parent = tmp_path / "outside"
    outside_state = outside_parent / "state" / "extensions.json"
    outside_state.parent.mkdir(parents=True)
    outside_state.write_text(
        json.dumps({"version": 1, "disabled_plugins": ["outside"]}),
        encoding="utf-8",
    )
    displaced_parent = tmp_path / "displaced"
    swapped = False
    original_create_file = AnchoredDirectory.create_file

    def swap_before_state_file(
        directory: AnchoredDirectory,
        name: str,
        *,
        mode: int = 0o600,
    ) -> int:
        nonlocal swapped
        if directory.path == state_directory and not swapped:
            _swap_ancestor(visible_parent, displaced_parent, outside_parent)
            swapped = True
        return original_create_file(directory, name, mode=mode)

    monkeypatch.setattr(AnchoredDirectory, "create_file", swap_before_state_file)

    state = set_plugin_enabled("example", enabled=False, path=state_path)

    assert swapped
    assert state.disabled_plugins == frozenset({"example"})
    assert load_extension_state(
        displaced_parent / "state" / "extensions.json"
    ).disabled_plugins == frozenset({"example"})
    assert json.loads(outside_state.read_text())["disabled_plugins"] == ["outside"]


def test_extension_state_rejects_final_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    _require_anchored_platform()
    state_path = tmp_path / "state" / "extensions.json"
    outside = tmp_path / "outside.json"
    state_path.parent.mkdir()
    outside.write_text(
        json.dumps({"version": 1, "disabled_plugins": ["outside"]}),
        encoding="utf-8",
    )
    try:
        state_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(PluginLifecycleError, match="regular file"):
        set_plugin_enabled("example", enabled=False, path=state_path)

    assert json.loads(outside.read_text())["disabled_plugins"] == ["outside"]


def test_extension_state_cleans_temporary_file_after_failed_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    state_path = tmp_path / "state" / "extensions.json"
    original_rename = AnchoredDirectory.rename

    def fail_final_rename(
        directory: AnchoredDirectory,
        source: str,
        destination: str,
        **kwargs: object,
    ) -> None:
        if destination == state_path.name:
            raise OSError("injected rename failure")
        original_rename(directory, source, destination, **kwargs)

    monkeypatch.setattr(AnchoredDirectory, "rename", fail_final_rename)

    with pytest.raises(PluginLifecycleError, match="injected rename failure"):
        set_plugin_enabled("example", enabled=False, path=state_path)

    assert list(state_path.parent.iterdir()) == []


def test_replace_failure_preserves_previous_plugin_when_destination_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    destination_root = tmp_path / "installed"
    install_local_plugin(
        _plugin(tmp_path / "first", version="1.0.0"),
        destination_root=destination_root,
    )
    outside = _plugin(tmp_path / "outside", version="outside")
    displaced = tmp_path / "displaced-original"
    original_rename = AnchoredDirectory.rename
    injected = False

    def fail_publish(
        directory: AnchoredDirectory,
        source: str,
        destination: str,
        **kwargs: object,
    ) -> None:
        nonlocal injected
        if source == "example" and destination.startswith(".example.backup-"):
            (destination_root / "example").rename(displaced)
            outside.rename(destination_root / "example")
            injected = True
        original_rename(directory, source, destination, **kwargs)

    monkeypatch.setattr(AnchoredDirectory, "rename", fail_publish)

    with pytest.raises(PluginLifecycleError, match="changed"):
        install_local_plugin(
            _plugin(tmp_path / "second", version="2.0.0"),
            destination_root=destination_root,
            replace=True,
        )

    assert injected
    assert (
        json.loads((displaced / "plugin.json").read_text())["version"] == "1.0.0"
    )
    assert json.loads((destination_root / "example" / "plugin.json").read_text())[
        "version"
    ] == "outside"


def test_replace_leaf_substitution_does_not_delete_unexpected_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    destination_root = tmp_path / "installed"
    install_local_plugin(
        _plugin(tmp_path / "first", version="1.0.0"),
        destination_root=destination_root,
    )
    outside = _plugin(tmp_path / "outside", version="9.0.0")
    displaced = tmp_path / "displaced-original"
    original_rename = AnchoredDirectory.rename
    injected = False

    def substitute_destination(
        directory: AnchoredDirectory,
        source: str,
        destination: str,
        **kwargs: object,
    ) -> None:
        nonlocal injected
        if source == "example" and destination.startswith(".example.backup-"):
            (destination_root / "example").rename(displaced)
            outside.rename(destination_root / "example")
            injected = True
        original_rename(directory, source, destination, **kwargs)

    monkeypatch.setattr(AnchoredDirectory, "rename", substitute_destination)

    with pytest.raises(PluginLifecycleError, match="changed"):
        install_local_plugin(
            _plugin(tmp_path / "second", version="2.0.0"),
            destination_root=destination_root,
            replace=True,
        )

    assert injected
    assert json.loads((displaced / "plugin.json").read_text())["version"] == "1.0.0"
    assert json.loads((destination_root / "example" / "plugin.json").read_text())[
        "version"
    ] == "9.0.0"


def test_uninstall_leaf_substitution_does_not_delete_unexpected_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    destination_root = tmp_path / "installed"
    install_local_plugin(
        _plugin(tmp_path / "source", version="1.0.0"),
        destination_root=destination_root,
    )
    outside = _plugin(tmp_path / "outside", version="9.0.0")
    displaced = tmp_path / "displaced-original"
    original_rename = AnchoredDirectory.rename
    injected = False

    def substitute_destination(
        directory: AnchoredDirectory,
        source: str,
        destination: str,
        **kwargs: object,
    ) -> None:
        nonlocal injected
        if source == "example" and destination.startswith(".example.uninstall-"):
            (destination_root / "example").rename(displaced)
            outside.rename(destination_root / "example")
            injected = True
        original_rename(directory, source, destination, **kwargs)

    monkeypatch.setattr(AnchoredDirectory, "rename", substitute_destination)

    with pytest.raises(PluginLifecycleError, match="changed"):
        uninstall_local_plugin(
            "example",
            destination_root=destination_root,
            confirmed=True,
            state_path=tmp_path / "state" / "extensions.json",
        )

    assert injected
    assert json.loads((displaced / "plugin.json").read_text())["version"] == "1.0.0"
    assert json.loads((destination_root / "example" / "plugin.json").read_text())[
        "version"
    ] == "9.0.0"


def test_failed_install_does_not_delete_substituted_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    destination_root = tmp_path / "installed"
    source = _plugin(tmp_path / "source", version="2.0.0")
    outside = _plugin(tmp_path / "outside", version="9.0.0")
    displaced = tmp_path / "displaced-staging"
    injected = False

    def replace_stage(
        stage: Path,
        manifest: object,
    ) -> None:
        nonlocal injected
        del manifest
        stage.rename(displaced)
        outside.rename(stage)
        injected = True

    with pytest.raises(PluginLifecycleError, match="cleanup"):
        install_local_plugin(
            source,
            destination_root=destination_root,
            validator=replace_stage,
        )

    assert injected
    assert json.loads((displaced / "plugin.json").read_text())["version"] == "2.0.0"
    unexpected = list(destination_root.glob(".install-*/plugin.json"))
    assert len(unexpected) == 1
    assert json.loads(unexpected[0].read_text())["version"] == "9.0.0"
    assert json.loads((destination_root / "example" / "plugin.json").read_text())[
        "version"
    ] == "2.0.0"


def test_uninstall_failure_preserves_plugin_when_destination_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    destination_root = tmp_path / "installed"
    install_local_plugin(
        _plugin(tmp_path / "source"),
        destination_root=destination_root,
    )
    outside = _plugin(tmp_path / "outside", version="outside")
    original_remove_tree = AnchoredDirectory.remove_tree
    injected = False

    def fail_quarantine_cleanup(
        directory: AnchoredDirectory,
        name: str,
        *,
        expected_descriptor: int | None = None,
    ) -> None:
        nonlocal injected
        if name.startswith(".example.uninstall-") and not injected:
            (destination_root / "example").symlink_to(outside, target_is_directory=True)
            injected = True
            raise OSError("injected quarantine race")
        original_remove_tree(
            directory,
            name,
            expected_descriptor=expected_descriptor,
        )

    monkeypatch.setattr(AnchoredDirectory, "remove_tree", fail_quarantine_cleanup)

    with pytest.raises(PluginLifecycleError, match="injected quarantine race"):
        uninstall_local_plugin(
            "example",
            destination_root=destination_root,
            confirmed=True,
            state_path=tmp_path / "state" / "extensions.json",
        )

    assert injected
    assert (destination_root / "example" / "plugin.json").is_file()
    assert (
        json.loads((destination_root / "example" / "plugin.json").read_text())[
            "version"
        ]
        == "1.0.0"
    )
    assert json.loads((outside / "plugin.json").read_text())["version"] == "outside"
    conflicts = list(destination_root.glob(".example.uninstall-conflict-*"))
    assert len(conflicts) == 1
    assert conflicts[0].is_symlink()


def test_plugin_mutations_fail_closed_without_anchored_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _plugin(tmp_path / "source")
    destination_root = tmp_path / "installed"
    existing = _plugin(destination_root / "example")
    state_path = tmp_path / "state" / "extensions.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps({"version": 1, "disabled_plugins": ["existing"]}),
        encoding="utf-8",
    )
    original_state = state_path.read_bytes()

    monkeypatch.setattr(anchored_fs, "supports_anchored_mutation", lambda: False)
    monkeypatch.setattr(lifecycle, "supports_anchored_mutation", lambda: False)

    with pytest.raises(PluginLifecycleError, match="unavailable"):
        install_local_plugin(source, destination_root=tmp_path / "new-installed")
    with pytest.raises(PluginLifecycleError, match="unavailable"):
        set_plugin_enabled("example", enabled=False, path=state_path)
    with pytest.raises(PluginLifecycleError, match="unavailable"):
        uninstall_local_plugin(
            "example",
            destination_root=destination_root,
            confirmed=True,
            state_path=tmp_path / "new-state" / "extensions.json",
        )

    assert existing.is_dir()
    assert state_path.read_bytes() == original_state


def test_existing_directory_identity_is_verified_before_descriptor_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    displaced = tmp_path / "displaced-child"
    outside = tmp_path / "outside-child"
    outside.mkdir()
    (outside / "marker").write_text("attacker", encoding="utf-8")
    with AnchoredDirectory.open(root, create=False) as directory:
        original_open = anchored_fs.os.open
        swapped = False

        def swap_before_open(
            path: str | os.PathLike[str],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            if path == "child" and dir_fd == directory.descriptor and not swapped:
                child.rename(displaced)
                outside.rename(child)
                swapped = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(anchored_fs.os, "open", swap_before_open)
        with pytest.raises(anchored_fs.AnchoredFilesystemError, match="changed"):
            directory.child("child")

    assert swapped
    assert displaced.is_dir()
    assert (child / "marker").read_text(encoding="utf-8") == "attacker"


def test_created_directory_identity_is_verified_before_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_text("attacker", encoding="utf-8")
    displaced = tmp_path / "displaced-created"
    with AnchoredDirectory.open(root, create=False) as directory:
        original_child = AnchoredDirectory.child
        swapped = False

        def swap_after_create(
            current: AnchoredDirectory,
            name: str,
            *,
            create: bool = False,
            expected: os.stat_result | None = None,
        ) -> AnchoredDirectory:
            nonlocal swapped
            if current is directory and name == "created" and not swapped:
                (root / name).rename(displaced)
                outside.rename(root / name)
                swapped = True
            return original_child(
                current,
                name,
                create=create,
                expected=expected,
            )

        monkeypatch.setattr(AnchoredDirectory, "child", swap_after_create)
        with pytest.raises(anchored_fs.AnchoredFilesystemError, match="changed"):
            directory.create_child("created")

    assert swapped
    assert displaced.is_dir()
    assert (root / "created" / "marker").read_text(encoding="utf-8") == "attacker"


def test_source_substitution_after_validation_cannot_change_install_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    source = _plugin(tmp_path / "source", version="1.0.0")
    replacement = _plugin(tmp_path / "replacement", version="9.0.0")
    displaced = tmp_path / "validated-source"
    destination_root = tmp_path / "installed"

    original_capture = lifecycle.PluginSnapshot.capture

    def capture_after_source_read(source_directory, **kwargs):
        snapshot = original_capture(source_directory, **kwargs)
        source.rename(displaced)
        replacement.rename(source)
        return snapshot

    monkeypatch.setattr(lifecycle.PluginSnapshot, "capture", capture_after_source_read)
    installed = install_local_plugin(source, destination_root=destination_root)

    assert json.loads((installed.root / "plugin.json").read_text())["version"] == "1.0.0"
    assert json.loads((source / "plugin.json").read_text())["version"] == "9.0.0"
    assert json.loads((displaced / "plugin.json").read_text())["version"] == "1.0.0"


def test_semantic_validation_and_publication_use_snapshot_bytes(
    tmp_path: Path,
) -> None:
    _require_anchored_platform()
    source = _plugin(tmp_path / "source")
    command = source / "commands" / "review.md"
    command.parent.mkdir()
    approved = b"Review the staged plugin.\n"
    command.write_bytes(approved)
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": "1.0.0",
                "commands": ["commands"],
            }
        ),
        encoding="utf-8",
    )

    original_validator = extensions._validate_plugin_contents_at

    def validate_then_mutate_source(snapshot, manifest) -> None:
        original_validator(snapshot, manifest)
        command.write_bytes(b"unvalidated replacement\n")

    installed = install_local_plugin(
        source,
        destination_root=tmp_path / "installed",
        _validator_at=validate_then_mutate_source,
    )

    assert (installed.root / "commands" / "review.md").read_bytes() == approved
    assert command.read_bytes() == b"unvalidated replacement\n"


def test_publication_ignores_mutated_legacy_stage_after_validation(
    tmp_path: Path,
) -> None:
    _require_anchored_platform()
    source = _plugin(tmp_path / "source")
    command = source / "commands" / "review.md"
    command.parent.mkdir()
    approved = b"Review the staged plugin.\n"
    command.write_bytes(approved)
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": "1.0.0",
                "commands": ["commands"],
            }
        ),
        encoding="utf-8",
    )

    def validate_then_mutate_stage(stage: Path, manifest: object) -> None:
        extensions._validate_plugin_contents(stage, manifest)
        (stage / "commands" / "review.md").write_bytes(
            b"unvalidated replacement\n"
        )

    installed = install_local_plugin(
        source,
        destination_root=tmp_path / "installed",
        validator=validate_then_mutate_stage,
    )

    assert (installed.root / "commands" / "review.md").read_bytes() == approved


def test_snapshot_validator_covers_all_plugin_component_bytes(tmp_path: Path) -> None:
    _require_anchored_platform()
    source = _plugin(tmp_path / "source")
    skill = source / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: review\ndescription: Review source\n---\nReview source.\n",
        encoding="utf-8",
    )
    command = source / "commands" / "review.md"
    command.parent.mkdir()
    command.write_text("Review $ARGUMENTS.\n", encoding="utf-8")
    agent = source / "agents" / "reviewer.md"
    agent.parent.mkdir()
    agent.write_text(
        "---\ndescription: Review changes\nbase-role: general\n---\nReview changes.\n",
        encoding="utf-8",
    )
    hook = source / "hooks" / "hooks.json"
    hook.parent.mkdir()
    hook.write_text(
        json.dumps({"session_start": [{"command": ["echo", "ready"]}]}),
        encoding="utf-8",
    )
    (source / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {
                        "transport": "http",
                        "url": "https://mcp.example.test",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": "1.0.0",
                "skills": ["skills/review/SKILL.md"],
                "commands": ["commands/review.md"],
                "agents": ["agents/reviewer.md"],
                "hooks": ["hooks/hooks.json"],
                "mcpServers": [".mcp.json"],
            }
        ),
        encoding="utf-8",
    )

    installed = install_local_plugin(
        source,
        destination_root=tmp_path / "installed",
        _validator_at=extensions._validate_plugin_contents_at,
    )

    assert (installed.root / "skills" / "review" / "SKILL.md").is_file()
    assert (installed.root / "commands" / "review.md").read_text() == "Review $ARGUMENTS.\n"
    assert (installed.root / "agents" / "reviewer.md").is_file()
    assert (installed.root / "hooks" / "hooks.json").is_file()
    assert (installed.root / ".mcp.json").is_file()


def test_snapshot_validator_accepts_a_root_level_skill(tmp_path: Path) -> None:
    _require_anchored_platform()
    source = _plugin(tmp_path / "source")
    (source / "SKILL.md").write_text(
        "---\nname: example\ndescription: Review files\n---\n"
        "Review the requested files.\n",
        encoding="utf-8",
    )

    installed = install_local_plugin(
        source,
        destination_root=tmp_path / "installed",
        _validator_at=extensions._validate_plugin_contents_at,
    )

    assert (installed.root / "SKILL.md").is_file()


def test_strict_created_directory_boundary_fails_before_first_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    root = tmp_path / "root"
    root.mkdir()
    mkdir_called = False
    original_mkdir = anchored_fs.os.mkdir

    def record_mkdir(*args: object, **kwargs: object) -> None:
        nonlocal mkdir_called
        mkdir_called = True
        original_mkdir(*args, **kwargs)

    with AnchoredDirectory.open(root, create=False) as directory:
        monkeypatch.setattr(anchored_fs.os, "mkdir", record_mkdir)
        with pytest.raises(
            anchored_fs.AnchoredFilesystemUnavailable,
            match="strict same-principal",
        ):
            directory.create_child_strict("created")

    assert not mkdir_called
    assert not (root / "created").exists()


def test_strict_regular_file_cleanup_boundary_leaves_replacement_untouched(
    tmp_path: Path,
) -> None:
    _require_anchored_platform()
    root = tmp_path / "root"
    root.mkdir()
    target = root / "record"
    target.write_text("original", encoding="utf-8")
    displaced = tmp_path / "displaced-record"
    replacement = tmp_path / "replacement-record"
    replacement.write_text("attacker", encoding="utf-8")

    with AnchoredDirectory.open(root, create=False) as directory:
        expected = directory.stat("record")
        assert expected is not None
        target.rename(displaced)
        replacement.rename(target)
        with pytest.raises(
            anchored_fs.AnchoredFilesystemUnavailable,
            match="strict same-principal",
        ):
            directory.unlink_strict("record", expected=expected)

    assert displaced.read_text(encoding="utf-8") == "original"
    assert target.read_text(encoding="utf-8") == "attacker"


def test_strict_directory_cleanup_boundary_leaves_replacement_untouched(
    tmp_path: Path,
) -> None:
    _require_anchored_platform()
    root = tmp_path / "root"
    root.mkdir()
    target = root / "tree"
    target.mkdir()
    (target / "original").write_text("original", encoding="utf-8")
    displaced = tmp_path / "displaced-tree"
    replacement = tmp_path / "replacement-tree"
    replacement.mkdir()
    (replacement / "attacker").write_text("attacker", encoding="utf-8")

    with AnchoredDirectory.open(root, create=False) as directory:
        with directory.child("tree") as held_tree:
            target.rename(displaced)
            replacement.rename(target)
            with pytest.raises(
                anchored_fs.AnchoredFilesystemUnavailable,
                match="strict same-principal",
            ):
                directory.remove_tree_strict(
                    "tree",
                    expected_descriptor=held_tree.descriptor,
                )

    assert (displaced / "original").read_text(encoding="utf-8") == "original"
    assert (target / "attacker").read_text(encoding="utf-8") == "attacker"


def test_changed_destination_content_cannot_be_published_after_snapshot_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    source = _plugin(tmp_path / "source")
    command = source / "commands" / "review.md"
    command.parent.mkdir()
    approved = b"Review the staged plugin.\n"
    command.write_bytes(approved)
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": "1.0.0",
                "commands": ["commands"],
            }
        ),
        encoding="utf-8",
    )
    replacement = b"X" * len(approved)
    original_fsync = anchored_fs.os.fsync
    swapped = False

    def mutate_destination_after_sync(descriptor: int) -> None:
        nonlocal swapped
        original_fsync(descriptor)
        try:
            visible = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            return
        if visible.parent.name == "commands" and visible.parent.parent.name == "example":
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, replacement)
            swapped = True

    monkeypatch.setattr(anchored_fs.os, "fsync", mutate_destination_after_sync)

    with pytest.raises(PluginLifecycleError, match="materialized plugin"):
        install_local_plugin(
            source,
            destination_root=tmp_path / "installed",
            _validator_at=extensions._validate_plugin_contents_at,
        )

    assert swapped
    assert not (tmp_path / "installed" / "example").exists()


def test_recursive_cleanup_leaves_replaced_regular_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    root = tmp_path / "root"
    target = root / "tree"
    target.mkdir(parents=True)
    victim = target / "victim"
    victim.write_text("original", encoding="utf-8")
    displaced = tmp_path / "displaced-victim"
    outside = tmp_path / "outside-victim"
    outside.write_text("attacker", encoding="utf-8")

    with AnchoredDirectory.open(root, create=False) as directory:
        with directory.child("tree") as held_tree:
            original_unlink = AnchoredDirectory.unlink
            swapped = False

            def substitute_file(
                current: AnchoredDirectory,
                name: str,
                **kwargs: object,
            ) -> None:
                nonlocal swapped
                if current.path == target and name == "victim" and not swapped:
                    victim.rename(displaced)
                    outside.rename(victim)
                    swapped = True
                original_unlink(current, name, **kwargs)

            monkeypatch.setattr(AnchoredDirectory, "unlink", substitute_file)
            with pytest.raises(anchored_fs.AnchoredFilesystemError, match="changed"):
                directory.remove_tree(
                    "tree",
                    expected_descriptor=held_tree.descriptor,
                )

    assert swapped
    assert displaced.read_text(encoding="utf-8") == "original"
    assert victim.read_text(encoding="utf-8") == "attacker"


def test_extension_state_cleanup_identity_check_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    state_path = tmp_path / "state" / "extensions.json"
    outside = tmp_path / "outside-state-temp"
    outside.write_text("attacker", encoding="utf-8")
    displaced = tmp_path / "displaced-state-temp"
    original_rename = AnchoredDirectory.rename
    swapped = False

    def fail_and_swap(
        directory: AnchoredDirectory,
        source: str,
        destination: str,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        if destination == state_path.name and not swapped:
            (state_path.parent / source).rename(displaced)
            outside.rename(state_path.parent / source)
            swapped = True
            raise OSError("injected state rename failure")
        original_rename(directory, source, destination, **kwargs)

    monkeypatch.setattr(AnchoredDirectory, "rename", fail_and_swap)
    with pytest.raises(PluginLifecycleError, match="injected state rename failure"):
        set_plugin_enabled("example", enabled=False, path=state_path)

    assert swapped
    temporary_entries = list(state_path.parent.glob("*.tmp"))
    assert len(temporary_entries) == 1
    assert temporary_entries[0].read_text(encoding="utf-8") == "attacker"
    assert displaced.read_text(encoding="utf-8").startswith("{")


def test_cleanup_failure_preserves_primary_error_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    source = _plugin(tmp_path / "source")
    destination_root = tmp_path / "installed"
    original_remove_tree = AnchoredDirectory.remove_tree

    def fail_stage_cleanup(
        directory: AnchoredDirectory,
        name: str,
        **kwargs: object,
    ) -> None:
        if name.startswith(".install-"):
            raise OSError("injected cleanup failure")
        original_remove_tree(directory, name, **kwargs)

    def fail_validation(root: Path, manifest: object) -> None:
        del root, manifest
        raise ValueError("primary validation failure")

    monkeypatch.setattr(AnchoredDirectory, "remove_tree", fail_stage_cleanup)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(PluginLifecycleError, match="primary validation failure") as error:
        install_local_plugin(
            source,
            destination_root=destination_root,
            validator=fail_validation,
        )
    after = set(os.listdir("/proc/self/fd"))

    assert before == after
    assert any("cleanup failure" in note for note in error.value.__notes__)


def test_extension_state_load_fails_closed_when_anchoring_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state" / "extensions.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps({"version": 1, "disabled_plugins": ["safe"]}),
        encoding="utf-8",
    )
    original = state_path.read_bytes()
    monkeypatch.setattr(anchored_fs, "supports_anchored_mutation", lambda: False)
    monkeypatch.setattr(lifecycle, "supports_anchored_mutation", lambda: False)

    with pytest.raises(PluginLifecycleError, match="unavailable"):
        load_extension_state(state_path)
    assert state_path.read_bytes() == original


def test_extension_inventory_does_not_activate_plugins_without_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugin_root = home / ".ash" / "plugins"
    plugin_root.mkdir(parents=True)
    _plugin(plugin_root / "example")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        extensions,
        "load_extension_state",
        lambda: (_ for _ in ()).throw(
            PluginLifecycleError("secure extension state unavailable")
        ),
    )

    inventory = discover_extensions(workspace)

    assert inventory.plugins == ()
    assert not any("plugin:example" in hook.source for hook in inventory.hooks)
    assert any("state unavailable" in error for error in inventory.errors)


def test_git_metadata_cleanup_does_not_follow_replaced_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    checkout = tmp_path / "checkout"
    git_directory = checkout / ".git"
    git_directory.mkdir(parents=True)
    (git_directory / "HEAD").write_text("original", encoding="utf-8")
    displaced = tmp_path / "displaced-git"
    outside = tmp_path / "outside-git"
    outside.mkdir()
    (outside / "victim").write_text("attacker", encoding="utf-8")
    original_remove_tree = AnchoredDirectory.remove_tree
    swapped = False

    def substitute_git(
        directory: AnchoredDirectory,
        name: str,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        if name == ".git" and not swapped:
            git_directory.rename(displaced)
            outside.rename(git_directory)
            swapped = True
        original_remove_tree(directory, name, **kwargs)

    monkeypatch.setattr(AnchoredDirectory, "remove_tree", substitute_git)
    with AnchoredDirectory.open(checkout, create=False) as directory:
        with pytest.raises(anchored_fs.AnchoredFilesystemError, match="changed"):
            lifecycle._remove_git_metadata(directory)

    assert swapped
    assert (displaced / "HEAD").read_text(encoding="utf-8") == "original"
    assert (git_directory / "victim").read_text(encoding="utf-8") == "attacker"


def test_git_temporary_root_cleanup_leaves_replaced_root_untouched(
    tmp_path: Path,
) -> None:
    _require_anchored_platform()
    temporary_root = tmp_path / "temporary-root"
    (temporary_root / "checkout").mkdir(parents=True)
    (temporary_root / "checkout" / "plugin.json").write_text(
        "original", encoding="utf-8"
    )
    displaced = tmp_path / "displaced-temporary-root"
    replacement = tmp_path / "replacement-temporary-root"
    (replacement / "checkout").mkdir(parents=True)
    (replacement / "checkout" / "victim").write_text("attacker", encoding="utf-8")

    with AnchoredDirectory.open(tmp_path, create=False, private=False) as parent:
        with AnchoredDirectory.open(temporary_root, create=False) as held_root:
            temporary_root.rename(displaced)
            replacement.rename(temporary_root)
            with pytest.raises(anchored_fs.AnchoredFilesystemError, match="changed"):
                parent.remove_tree(
                    temporary_root.name,
                    expected_descriptor=held_root.descriptor,
                )

    assert (displaced / "checkout" / "plugin.json").read_text(encoding="utf-8") == "original"
    assert (temporary_root / "checkout" / "victim").read_text(encoding="utf-8") == "attacker"


def test_git_setup_anchors_temp_parent_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_anchored_platform()
    outside = tmp_path / "outside-temp"
    outside.mkdir()
    linked_temp = tmp_path / "linked-temp"
    try:
        linked_temp.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    monkeypatch.setattr(lifecycle.tempfile, "gettempdir", lambda: str(linked_temp))
    monkeypatch.setattr(
        lifecycle,
        "resolve_host_executable",
        lambda *args, **kwargs: "/bin/true",
    )

    with pytest.raises(PluginLifecycleError, match="temporary directory"):
        install_git_plugin(
            "https://plugins.example/demo.git",
            ref="main",
            destination_root=tmp_path / "installed",
        )

    assert list(outside.glob("ash-plugin-git-*")) == []


def test_git_tree_size_scan_closes_completed_directory_descriptors(
    tmp_path: Path,
) -> None:
    _require_anchored_platform()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for index in range(1_400):
        (checkout / f"directory-{index}").mkdir()

    with AnchoredDirectory.open(checkout, create=False, private=False) as directory:
        assert not lifecycle._tree_exceeds_bytes_at(directory, 1)


def test_strict_identity_mutation_capability_is_explicitly_unavailable() -> None:
    _require_anchored_platform()
    assert not anchored_fs.supports_strict_identity_mutation()
    with pytest.raises(
        anchored_fs.AnchoredFilesystemUnavailable,
        match="strict same-principal",
    ):
        anchored_fs.require_strict_identity_mutation()
