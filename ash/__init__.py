"""Ash coding harness."""

import importlib
import sys
from importlib.metadata import PackageNotFoundError, version

from ash.sdk import AshClient, AshResult

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

for _package_name in _LEGACY_PACKAGES:
    _module = importlib.import_module(_package_name)
    sys.modules.setdefault(f"{__name__}.{_package_name}", _module)
    globals()[_package_name] = _module

__all__ = ["__version__", "AshClient", "AshResult"]
