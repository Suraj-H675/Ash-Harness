"""Subprocess sandboxing primitives for Ash (Sprint 11).

Tiered execution per the V4 roadmap in ASH_MASTER_PLAN_V2.md:

* Tier 1: path-scoped subprocess (always available — the Sprint 4 default).
* Tier 2: bubblewrap on Linux, sandbox-exec on macOS.
* Tier 3: ephemeral Docker container.

Higher tiers are preferred when available; on any failure the manager
falls back to the next-lower tier so user workflows are never blocked
by missing sandboxing infrastructure.
"""

from sandbox._base import (
    SANDBOX_TIER_BWRAP,
    SANDBOX_TIER_DOCKER,
    SANDBOX_TIER_SANDBOX_EXEC,
    SANDBOX_TIER_SCOPED,
    SandboxBackend,
    SandboxBackendUnavailable,
    SandboxTier,
)
from sandbox.bwrap import BubblewrapSandbox
from sandbox.docker import DockerSandbox
from sandbox.manager import (
    SandboxManager,
    SandboxInvocation,
    SandboxResult,
    auto_approve_safety_error,
    has_bwrap,
    has_docker,
    has_sandbox_exec,
)


__all__ = [
    "SANDBOX_TIER_BWRAP",
    "SANDBOX_TIER_DOCKER",
    "SANDBOX_TIER_SANDBOX_EXEC",
    "SANDBOX_TIER_SCOPED",
    "BubblewrapSandbox",
    "DockerSandbox",
    "SandboxBackend",
    "SandboxBackendUnavailable",
    "SandboxManager",
    "SandboxInvocation",
    "SandboxResult",
    "SandboxTier",
    "auto_approve_safety_error",
    "has_bwrap",
    "has_docker",
    "has_sandbox_exec",
]
