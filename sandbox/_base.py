"""Shared sandbox types — kept tiny to avoid circular imports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


# Tier constants are stable identifiers, not behaviour switches.
SANDBOX_TIER_SCOPED: int = 1
SANDBOX_TIER_BWRAP: int = 2
SANDBOX_TIER_SANDBOX_EXEC: int = 2
SANDBOX_TIER_DOCKER: int = 3

SandboxTier = int


class SandboxBackendUnavailable(RuntimeError):
    """Raised when a sandbox backend cannot fulfill a request."""


@dataclass(frozen=True)
class SandboxBackend:
    """Minimal contract every backend satisfies.

    Concrete backends (BubblewrapSandbox, DockerSandbox, the internal
    scoped backend) extend this contract; the manager only depends on
    the abstract :class:`SandboxBackend` interface.
    """

    name: str
    tier: SandboxTier

    def is_available(self) -> bool:  # pragma: no cover - default
        return False

    def wrap(self, command: Sequence[str], *, cwd: Path | None = None) -> list[str]:
        raise NotImplementedError
