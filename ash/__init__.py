"""Ash coding harness."""

import importlib
import importlib.util
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any

try:
    __version__ = version("ash-ai")
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


class _LegacyAliasLoader:
    def __init__(self, target: str) -> None:
        self.target = target

    def create_module(self, spec: Any) -> Any:
        return importlib.import_module(self.target)

    def exec_module(self, module: Any) -> None:
        return None


class _LegacyAliasFinder:
    """Resolve historical ``ash.<package>`` names without eager imports."""

    _ash_legacy_alias_finder = True

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: Any = None,
    ) -> Any:
        prefix = f"{__name__}."
        if not fullname.startswith(prefix):
            return None
        canonical = fullname[len(prefix) :]
        root = canonical.partition(".")[0]
        if root not in _LEGACY_PACKAGES:
            return None
        canonical_spec = importlib.util.find_spec(canonical)
        if canonical_spec is None:
            return None
        loader: Any = _LegacyAliasLoader(canonical)
        return importlib.util.spec_from_loader(
            fullname,
            loader,
            is_package=canonical_spec.submodule_search_locations is not None,
        )


if not any(
    getattr(finder, "_ash_legacy_alias_finder", False) for finder in sys.meta_path
):
    sys.meta_path.insert(0, _LegacyAliasFinder())


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
