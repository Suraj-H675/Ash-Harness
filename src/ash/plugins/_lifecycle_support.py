"""Private safety helpers shared by plugin lifecycle persistence modules."""

from __future__ import annotations

import errno
import os

from ash.plugins.errors import PluginLifecycleError
from ash.plugins.manifest import PLUGIN_NAME
from ash.safety.anchored_fs import (
    AnchoredFilesystemError,
    AnchoredFilesystemUnavailable,
    require_anchored_mutation,
    supports_anchored_mutation,
)


def validate_plugin_name(name: str) -> None:
    if not PLUGIN_NAME.fullmatch(name):
        raise PluginLifecycleError("plugin name must be a path-safe identifier")


def require_anchored_plugin_mutation() -> None:
    if not supports_anchored_mutation():
        raise PluginLifecycleError(
            "descriptor-anchored plugin lifecycle mutation is unavailable "
            "on this platform/build"
        )
    try:
        require_anchored_mutation()
    except (AnchoredFilesystemUnavailable, AnchoredFilesystemError) as exc:
        raise PluginLifecycleError(str(exc)) from exc


def lifecycle_error(label: str, exc: BaseException) -> PluginLifecycleError:
    if getattr(exc, "errno", None) == errno.ELOOP:
        return PluginLifecycleError(f"{label} cannot traverse a link")
    detail = str(exc)
    if "link" in detail.lower():
        return PluginLifecycleError(f"{label} cannot use a linked entry: {detail}")
    return PluginLifecycleError(f"cannot securely mutate {label}: {detail}")


def write_all(descriptor: int, payload: bytes, *, label: str) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise PluginLifecycleError(f"short write to {label}")
        view = view[written:]


def sync_directory_strict(descriptor: int, *, label: str) -> None:
    """Require one lifecycle directory update to reach the filesystem boundary."""

    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise PluginLifecycleError(f"cannot durably update {label}: {exc}") from exc
