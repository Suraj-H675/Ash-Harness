"""Ash coding harness."""

import importlib
from importlib.metadata import PackageNotFoundError, version
from typing import Any

try:
    __version__ = version("ash-ai")
except PackageNotFoundError:
    __version__ = "0.1.0"


_SDK_EXPORTS = frozenset(
    {"AshClient", "AshDelegationResult", "AshEvent", "AshEventRecord", "AshResult"}
)


def __getattr__(name: str) -> Any:
    """Load public SDK exports only when requested."""

    if name in _SDK_EXPORTS:
        module = importlib.import_module("ash.sdk")
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_SDK_EXPORTS})


__all__ = [
    "__version__",
    "AshClient",
    "AshDelegationResult",
    "AshEvent",
    "AshEventRecord",
    "AshResult",
]
