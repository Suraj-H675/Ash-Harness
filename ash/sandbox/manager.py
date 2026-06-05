"""Sandbox tier selection and execution orchestration.

The :class:`SandboxManager` is the only entry point the rest of the
codebase needs to know about. It probes for available backends at
construction time, picks the highest tier it can use, and exposes a
single :meth:`run` method that always returns a
:class:`SandboxResult` regardless of which tier ran the command.

Falling back is graceful: if a Tier 2/3 backend raises
:class:`SandboxBackendUnavailable` mid-run, the manager transparently
re-issues the command at Tier 1 so the user workflow is not blocked.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ash.sandbox._base import (
    SANDBOX_TIER_BWRAP,
    SANDBOX_TIER_DOCKER,
    SANDBOX_TIER_SCOPED,
    SandboxBackend,
    SandboxBackendUnavailable,
    SandboxTier,
)
from ash.sandbox.bwrap import BubblewrapSandbox, probe_bwrap
from ash.sandbox.docker import DockerSandbox, probe_docker


# Re-export the tier constants for backwards compatibility with code
# that imports them from ash.sandbox.manager.
__all__ = [
    "SANDBOX_TIER_BWRAP",
    "SANDBOX_TIER_DOCKER",
    "SANDBOX_TIER_SANDBOX_EXEC",
    "SANDBOX_TIER_SCOPED",
    "SandboxBackendUnavailable",
    "SandboxManager",
    "SandboxResult",
    "SandboxTier",
    "has_bwrap",
    "has_docker",
    "has_sandbox_exec",
]


# macOS-only tier constant (semantically distinct from bwrap).
SANDBOX_TIER_SANDBOX_EXEC: int = SANDBOX_TIER_BWRAP


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of a sandboxed subprocess run."""

    exit_code: int
    stdout: str
    stderr: str
    tier: SandboxTier
    backend_name: str
    fallback_used: bool = False
    duration_seconds: float = 0.0


# --- probe helpers ---------------------------------------------------------


def has_bwrap() -> bool:
    """Whether ``bwrap`` is on PATH and the host is Linux."""

    return sys.platform.startswith("linux") and probe_bwrap() is not None


def has_sandbox_exec() -> bool:
    """Whether ``sandbox-exec`` is available (macOS only)."""

    if sys.platform != "darwin":
        return False
    return shutil.which("sandbox-exec") is not None


def has_docker() -> bool:
    """Whether ``docker`` is on PATH."""

    return probe_docker() is not None


# --- the manager -----------------------------------------------------------


@dataclass
class SandboxManager:
    """
    Pick the best available sandbox and execute subprocesses through it.

    Parameters
    ----------
    workspace_root
        Project root the manager will read/write. Required for tier
        2/3; optional for tier 1 (the path-scoped subprocess respects
        the caller's ``cwd``).
    preferred_tier
        The highest tier the caller is willing to use. The manager
        still picks the highest tier ``<= preferred_tier`` that is
        actually available; defaults to the maximum (3).
    network
        Whether sandboxed processes may access the network. Defaults
        to ``False`` per the V4 spec.
    timeout_seconds
        Hard timeout for sandboxed subprocesses.
    extra_read_only_paths
        Additional read-only paths to expose to tier-2/3 backends
        (e.g. system libraries).
    """

    workspace_root: Path | None = None
    preferred_tier: SandboxTier = SANDBOX_TIER_DOCKER
    network: bool = False
    timeout_seconds: int = 300
    extra_read_only_paths: tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self._tier: SandboxTier = self._detect_tier()

    # --- public API ------------------------------------------------------

    @property
    def tier(self) -> SandboxTier:
        """The active tier (1, 2, or 3)."""

        return self._tier

    @property
    def backend_name(self) -> str:
        """Human-readable name of the active backend."""

        return _backend_name(self._tier)

    def capabilities(self) -> dict[str, bool]:
        """Return the availability map for every backend."""

        return {
            "scoped": True,
            "bwrap": has_bwrap(),
            "sandbox_exec": has_sandbox_exec(),
            "docker": has_docker(),
        }

    def is_fully_isolated(self) -> bool:
        """``True`` only when a Tier 2 or Tier 3 backend is active."""

        return self._tier >= SANDBOX_TIER_BWRAP

    async def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
    ) -> SandboxResult:
        """
        Run ``command`` (argv list) under the active sandbox.

        If the active tier is unavailable mid-run (e.g. bwrap installed
        but bwrap binary missing on the executable path), the manager
        transparently falls back to Tier 1. The :class:`SandboxResult`
        reports the actual tier used and a ``fallback_used`` flag so
        the loop can log the degradation.
        """

        if not command:
            raise ValueError("command must be a non-empty sequence")

        deadline = timeout if timeout is not None else self.timeout_seconds
        try:
            backend = self._build_backend(self._tier)
        except SandboxBackendUnavailable:
            return await _run_scoped(
                _ScopedBackend(), command, cwd, deadline, fallback=True
            )

        if isinstance(backend, _ScopedBackend):
            return await _run_scoped(
                backend, command, cwd, deadline, fallback=False
            )

        # Tier 2/3 path: build the wrapped argv, then exec.
        try:
            wrapped = backend.wrap(command, cwd=cwd)
        except SandboxBackendUnavailable:
            return await _run_scoped(
                _ScopedBackend(), command, cwd, deadline, fallback=True
            )

        return await _run_subprocess(
            wrapped,
            cwd=cwd,
            deadline=deadline,
            tier=self._tier,
            backend_name=backend.name,
        )

    # --- tier detection --------------------------------------------------

    def _detect_tier(self) -> SandboxTier:
        """Pick the highest available tier ``<= preferred_tier``."""

        if self.preferred_tier >= SANDBOX_TIER_DOCKER and has_docker():
            return SANDBOX_TIER_DOCKER
        if self.preferred_tier >= SANDBOX_TIER_BWRAP and has_bwrap():
            return SANDBOX_TIER_BWRAP
        if self.preferred_tier >= SANDBOX_TIER_BWRAP and has_sandbox_exec():
            return SANDBOX_TIER_BWRAP
        return SANDBOX_TIER_SCOPED

    def _build_backend(self, tier: SandboxTier) -> SandboxBackend:
        if tier == SANDBOX_TIER_DOCKER:
            backend = DockerSandbox(workspace_root=self.workspace_root, network=self.network)
            if not backend.is_available():
                raise SandboxBackendUnavailable("docker backend unavailable")
            return backend
        if tier == SANDBOX_TIER_BWRAP:
            if sys.platform.startswith("linux"):
                backend = BubblewrapSandbox(
                    workspace_root=self.workspace_root,
                    read_only_paths=self.extra_read_only_paths,
                    network=self.network,
                )
                if not backend.is_available():
                    raise SandboxBackendUnavailable("bwrap backend unavailable")
                return backend
            if has_sandbox_exec():
                return _SandboxExecBackend()
        return _ScopedBackend()


# --- the path-scoped fallback ----------------------------------------------


@dataclass(frozen=True)
class _ScopedBackend(SandboxBackend):
    """Tier 1 — plain subprocess; the Sprint 4 default."""

    name: str = "scoped"
    tier: SandboxTier = SANDBOX_TIER_SCOPED

    def is_available(self) -> bool:
        return True

    def wrap(self, command: Sequence[str], *, cwd: Path | None = None) -> list[str]:
        return list(command)


async def _run_scoped(
    backend: "_ScopedBackend",
    command: Sequence[str],
    cwd: Path | None,
    deadline: int,
    *,
    fallback: bool,
) -> SandboxResult:
    """Execute the command directly via asyncio.create_subprocess_exec."""

    start = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=deadline
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr=f"Command timed out after {deadline} seconds.",
            tier=SANDBOX_TIER_SCOPED,
            backend_name=backend.name,
            fallback_used=fallback,
            duration_seconds=time.monotonic() - start,
        )
    return SandboxResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
        stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
        tier=SANDBOX_TIER_SCOPED,
        backend_name=backend.name,
        fallback_used=fallback,
        duration_seconds=time.monotonic() - start,
    )


async def _run_subprocess(
    argv: list[str],
    *,
    cwd: Path | None,
    deadline: int,
    tier: SandboxTier,
    backend_name: str,
) -> SandboxResult:
    """Execute a pre-wrapped argv (e.g. bwrap or docker run) directly."""

    start = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=deadline
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr=f"Command timed out after {deadline} seconds.",
            tier=tier,
            backend_name=backend_name,
            fallback_used=False,
            duration_seconds=time.monotonic() - start,
        )
    return SandboxResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
        stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
        tier=tier,
        backend_name=backend_name,
        fallback_used=False,
        duration_seconds=time.monotonic() - start,
    )


# --- macOS sandbox-exec ----------------------------------------------------


@dataclass(frozen=True)
class _SandboxExecBackend(SandboxBackend):
    """macOS ``sandbox-exec`` profile-based isolation."""

    name: str = "sandbox-exec"
    tier: SandboxTier = SANDBOX_TIER_BWRAP

    def is_available(self) -> bool:
        return has_sandbox_exec()

    def wrap(self, command: Sequence[str], *, cwd: Path | None = None) -> list[str]:
        if not self.is_available():
            raise SandboxBackendUnavailable("sandbox-exec not available")
        return ["sandbox-exec", "-p", _SANDBOX_EXEC_PROFILE, *command]


_SANDBOX_EXEC_PROFILE = """(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow file-read*)
(allow file-write* (subpath "/tmp"))
(allow file-write* (subpath "/Users"))
(allow sysctl-read)
(deny network-outbound)
(deny network-inbound)
"""


# --- helpers --------------------------------------------------------------


def _backend_name(tier: SandboxTier) -> str:
    if tier == SANDBOX_TIER_DOCKER:
        return "docker"
    if tier == SANDBOX_TIER_BWRAP:
        if sys.platform == "darwin":
            return "sandbox-exec"
        return "bubblewrap"
    return "scoped"
