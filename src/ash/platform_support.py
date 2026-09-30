"""Declared host-platform support for the Ash CLI and installer."""

from __future__ import annotations

import sys


SUPPORTED_NATIVE_PLATFORMS = frozenset({"linux", "darwin"})


class UnsupportedPlatformError(RuntimeError):
    """Raised when a public Ash runtime is created on an unsupported host."""


def native_platform_supported(platform: str | None = None) -> bool:
    """Return whether Ash supports the native host represented by ``platform``."""

    value = sys.platform if platform is None else platform
    return value in SUPPORTED_NATIVE_PLATFORMS


def platform_support_error(platform: str | None = None) -> str | None:
    """Return an actionable unsupported-platform message, or ``None``."""

    value = sys.platform if platform is None else platform
    if native_platform_supported(value):
        return None
    if value == "win32":
        return (
            "Native Windows is not currently supported by Ash. "
            "Use Ash inside WSL2, where it runs through the supported Linux path."
        )
    return (
        f"Ash does not currently support this native platform ({value}). "
        "Supported native platforms are Linux and macOS."
    )


def require_supported_native_platform(platform: str | None = None) -> None:
    """Raise when a caller attempts to construct Ash on an unsupported host."""

    message = platform_support_error(platform)
    if message is not None:
        raise UnsupportedPlatformError(message)
