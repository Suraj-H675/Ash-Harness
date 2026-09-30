"""Persistent plugin activation state with descriptor-anchored atomic updates."""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ash.plugins._lifecycle_support import (
    lifecycle_error,
    require_anchored_plugin_mutation,
    sync_directory_strict,
    validate_plugin_name,
    write_all,
)
from ash.plugins.errors import PluginLifecycleError
from ash.safe_io import read_bounded_open_file, strict_json_loads
from ash.safety.anchored_fs import (
    AnchoredDirectory,
    AnchoredFilesystemError,
    supports_anchored_mutation,
)

MAX_EXTENSION_STATE_BYTES = 256 * 1024
STATE_VERSION = 1


@dataclass(frozen=True)
class ExtensionState:
    disabled_plugins: frozenset[str] = frozenset()


def extension_state_path() -> Path:
    return Path.home() / ".ash" / "extensions.json"


def load_extension_state(path: Path | None = None) -> ExtensionState:
    state_path = path or extension_state_path()
    if not supports_anchored_mutation():
        try:
            raw = read_bounded_open_file(
                state_path,
                MAX_EXTENSION_STATE_BYTES,
                label="extension state",
            )
        except FileNotFoundError:
            return ExtensionState()
        except (OSError, ValueError) as exc:
            raise lifecycle_error("extension state", exc) from exc
        return _parse_extension_state(raw, state_path)

    require_anchored_plugin_mutation()
    try:
        directory = AnchoredDirectory.open(
            state_path.parent,
            create=False,
            private=False,
        )
    except FileNotFoundError:
        return ExtensionState()
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("extension state", exc) from exc
    try:
        return _read_extension_state_at(directory, state_path)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("extension state", exc) from exc
    finally:
        directory.close()


def _parse_extension_state(raw: bytes, state_path: Path) -> ExtensionState:
    try:
        payload = strict_json_loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PluginLifecycleError(
            f"cannot load extension state {state_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise PluginLifecycleError(f"invalid extension state: {state_path}")
    disabled = payload.get("disabled_plugins", [])
    if not isinstance(disabled, list) or not all(
        isinstance(name, str) and name for name in disabled
    ):
        raise PluginLifecycleError(
            f"disabled_plugins must be a list of names: {state_path}"
        )
    return ExtensionState(disabled_plugins=frozenset(disabled))


def set_plugin_enabled(
    name: str,
    *,
    enabled: bool,
    path: Path | None = None,
) -> ExtensionState:
    _, updated = transition_extension_state(name, enabled=enabled, path=path)
    return updated


@contextmanager
def locked_extension_state(
    *,
    path: Path | None = None,
) -> Iterator[ExtensionState]:
    """Hold the extension-state lock while a caller validates a short commit window."""

    state_path = path or extension_state_path()
    require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(state_path.parent, create=True) as directory,
            directory.lock(f".{state_path.name}.lock"),
        ):
            yield _read_extension_state_at(directory, state_path)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("extension state", exc) from exc


def transition_extension_state(
    name: str,
    *,
    enabled: bool,
    path: Path | None = None,
) -> tuple[ExtensionState, ExtensionState]:
    return transition_extension_state_checked(
        name,
        enabled=enabled,
        path=path,
    )


def transition_extension_state_checked(
    name: str,
    *,
    enabled: bool,
    path: Path | None = None,
    validator: Callable[[ExtensionState], None] | None = None,
) -> tuple[ExtensionState, ExtensionState]:
    """Validate and mutate one plugin bit while the state lock is held."""

    validate_plugin_name(name)
    require_anchored_plugin_mutation()
    state_path = path or extension_state_path()
    try:
        with (
            AnchoredDirectory.open(state_path.parent, create=True) as directory,
            directory.lock(f".{state_path.name}.lock"),
        ):
            directory.prepare_durable_mutation()
            state = _read_extension_state_at(directory, state_path)
            if validator is not None:
                validator(state)
            disabled = set(state.disabled_plugins)
            if enabled:
                disabled.discard(name)
            else:
                disabled.add(name)
            updated = ExtensionState(disabled_plugins=frozenset(disabled))
            try:
                _save_extension_state_at(directory, updated, state_path)
            except BaseException as primary:
                try:
                    current = _read_extension_state_at(directory, state_path)
                    if current == updated and current != state:
                        _save_extension_state_at(directory, state, state_path)
                except BaseException as cleanup:
                    primary.add_note(f"extension-state rollback failed: {cleanup}")
                raise
            return state, updated
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("extension state", exc) from exc


def restore_extension_state(
    expected: ExtensionState,
    previous: ExtensionState,
    *,
    path: Path | None = None,
) -> None:
    if expected == previous:
        return
    changed_plugins = expected.disabled_plugins ^ previous.disabled_plugins
    state_path = path or extension_state_path()
    try:
        with (
            AnchoredDirectory.open(state_path.parent, create=True) as directory,
            directory.lock(f".{state_path.name}.lock"),
        ):
            directory.prepare_durable_mutation()
            current = _read_extension_state_at(directory, state_path)
            disabled = set(current.disabled_plugins)
            for name in changed_plugins:
                expected_disabled = name in expected.disabled_plugins
                if (name in disabled) != expected_disabled:
                    raise PluginLifecycleError(
                        f"extension state for plugin {name!r} changed while plugin "
                        "installation was in progress"
                    )
                if name in previous.disabled_plugins:
                    disabled.add(name)
                else:
                    disabled.discard(name)
            restored = ExtensionState(disabled_plugins=frozenset(disabled))
            if restored != current:
                _save_extension_state_at(directory, restored, state_path)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("extension state", exc) from exc


def _read_extension_state_at(
    directory: AnchoredDirectory,
    state_path: Path,
) -> ExtensionState:
    raw = directory.read_file(
        state_path.name,
        max_bytes=MAX_EXTENSION_STATE_BYTES,
    )
    if raw is None:
        return ExtensionState()
    return _parse_extension_state(raw, state_path)


def _save_extension_state(state: ExtensionState, path: Path) -> None:
    require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(path.parent, create=True) as directory,
            directory.lock(f".{path.name}.lock"),
        ):
            directory.prepare_durable_mutation()
            _save_extension_state_at(directory, state, path)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("extension state", exc) from exc


def _save_extension_state_at(
    directory: AnchoredDirectory,
    state: ExtensionState,
    path: Path,
) -> None:
    payload: dict[str, Any] = {
        "version": STATE_VERSION,
        "disabled_plugins": sorted(state.disabled_plugins),
    }
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    existing = directory.stat(path.name)
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise PluginLifecycleError(f"extension state is not a regular file: {path}")
    temporary = directory.unique_name(f".{path.name}.", ".tmp")
    descriptor = -1
    temporary_identity: os.stat_result | None = None
    try:
        descriptor = directory.create_file(temporary, mode=0o600)
        temporary_identity = os.fstat(descriptor)
        write_all(descriptor, data, label="extension state")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        directory.rename(temporary, path.name, expected_source=temporary_identity)
        temporary = ""
        directory.sync()
        sync_directory_strict(
            directory.descriptor,
            label="plugin activation-state directory",
        )
    except BaseException as primary:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except BaseException as cleanup:
                primary.add_note(f"extension-state descriptor close failed: {cleanup}")
            descriptor = -1
        if temporary:
            try:
                directory.unlink(
                    temporary,
                    expected=temporary_identity,
                    missing_ok=True,
                )
            except BaseException as cleanup:
                primary.add_note(f"extension-state temporary cleanup failed: {cleanup}")
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
