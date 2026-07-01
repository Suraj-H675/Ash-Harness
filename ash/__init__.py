"""Ash coding harness."""

import importlib
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any

try:
    __version__ = version("ash")
except PackageNotFoundError:
    __version__ = "0.1.0"


_LEGACY_PACKAGES = (
    "agents",
    "context",
    "core",
    "hooks",
    "lsp",
    "mcp",
    "memory",
    "plugins",
    "providers",
    "repo",
    "safety",
    "sandbox",
    "server",
    "tools",
    "ui",
)

_SDK_EXPORTS = frozenset({"AshClient", "AshEvent", "AshResult"})


def __getattr__(name: str) -> Any:
    """Load SDK exports and legacy namespace aliases only when requested."""

    if name in _SDK_EXPORTS:
        module = importlib.import_module("ash.sdk")
        value = getattr(module, name)
        globals()[name] = value
        return value
    if name in _LEGACY_PACKAGES:
        module = importlib.import_module(name)
        sys.modules.setdefault(f"{__name__}.{name}", module)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_SDK_EXPORTS, *_LEGACY_PACKAGES})


__all__ = ["__version__", "AshClient", "AshEvent", "AshResult"]
