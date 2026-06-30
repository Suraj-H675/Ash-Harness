"""Sandbox tier selection and execution orchestration.

The :class:`SandboxManager` is the only entry point the rest of the
codebase needs to know about. It probes for available backends at
construction time, picks the highest tier it can use, and exposes a
single :meth:`run` method that always returns a
:class:`SandboxResult` regardless of which tier ran the command.

An isolated command fails closed if its backend becomes unavailable. An
explicit compatibility switch permits scoped fallback for callers that have
already obtained informed consent for unisolated execution.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from sandbox._base import (
    SANDBOX_TIER_BWRAP,
    SANDBOX_TIER_DOCKER,
    SANDBOX_TIER_SCOPED,
    SandboxBackend,
    SandboxBackendUnavailable,
    SandboxTier,
)
from sandbox.bwrap import BubblewrapSandbox, probe_bwrap
from sandbox.docker import DockerSandbox, probe_docker
from sandbox.process_utils import process_group_options, terminate_process_tree


# Re-export the tier constants for backwards compatibility with code
# that imports them from sandbox.manager.
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
    allow_scoped_fallback: bool = False

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
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        """
        Run ``command`` (argv list) under the active sandbox.

        Isolated backends fail closed when they become unavailable. Callers
        may explicitly opt into scoped fallback for compatibility; the result
        then reports the actual tier and ``fallback_used=True``.
        """

        if not command:
            raise ValueError("command must be a non-empty sequence")

        deadline = timeout if timeout is not None else self.timeout_seconds
        try:
            backend = self._build_backend(self._tier)
        except SandboxBackendUnavailable:
            if not self.allow_scoped_fallback:
                raise
            return await _run_scoped(
                _ScopedBackend(), command, cwd, deadline, fallback=True, env=env
            )

        if isinstance(backend, _ScopedBackend):
            return await _run_scoped(
                backend, command, cwd, deadline, fallback=False, env=env
            )

        # Tier 2/3 path: build the wrapped argv, then exec.
        try:
            wrapped = backend.wrap(command, cwd=cwd)
        except SandboxBackendUnavailable:
            if not self.allow_scoped_fallback:
                raise
            return await _run_scoped(
                _ScopedBackend(), command, cwd, deadline, fallback=True, env=env
            )

        return await _run_subprocess(
            wrapped,
            cwd=cwd,
            deadline=deadline,
            tier=self._tier,
            backend_name=backend.name,
            env=env,
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
            docker_backend = DockerSandbox(
                workspace_root=self.workspace_root, network=self.network
            )
            if not docker_backend.is_available():
                raise SandboxBackendUnavailable("docker backend unavailable")
            return docker_backend
        if tier == SANDBOX_TIER_BWRAP:
            if sys.platform.startswith("linux"):
                bwrap_backend = BubblewrapSandbox(
                    workspace_root=self.workspace_root,
                    read_only_paths=self.extra_read_only_paths,
                    network=self.network,
                )
                if not bwrap_backend.is_available():
                    raise SandboxBackendUnavailable("bwrap backend unavailable")
                return bwrap_backend
            if has_sandbox_exec():
                return _SandboxExecBackend(
                    workspace_root=self.workspace_root,
                    network=self.network,
                )
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
    env: dict[str, str] | None = None,
) -> SandboxResult:
    """Execute the command directly via asyncio.create_subprocess_exec."""

    start = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_group_options(),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=deadline
        )
    except asyncio.TimeoutError:
        await terminate_process_tree(process)
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr=f"Command timed out after {deadline} seconds.",
            tier=SANDBOX_TIER_SCOPED,
            backend_name=backend.name,
            fallback_used=fallback,
            duration_seconds=time.monotonic() - start,
        )
    except asyncio.CancelledError:
        await terminate_process_tree(process)
        raise
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
    env: dict[str, str] | None = None,
) -> SandboxResult:
    """Execute a pre-wrapped argv (e.g. bwrap or docker run) directly."""

    start = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_group_options(),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=deadline
        )
    except asyncio.TimeoutError:
        await terminate_process_tree(process)
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr=f"Command timed out after {deadline} seconds.",
            tier=tier,
            backend_name=backend_name,
            fallback_used=False,
            duration_seconds=time.monotonic() - start,
        )
    except asyncio.CancelledError:
        await terminate_process_tree(process)
        raise
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
    workspace_root: Path | None = None
    network: bool = False

    def is_available(self) -> bool:
        return has_sandbox_exec()

    def wrap(self, command: Sequence[str], *, cwd: Path | None = None) -> list[str]:
        if not self.is_available():
            raise SandboxBackendUnavailable("sandbox-exec not available")
        if self.workspace_root is None:
            raise SandboxBackendUnavailable("sandbox-exec requires a workspace root")
        root = Path(self.workspace_root).resolve()
        if not root.is_dir():
            raise SandboxBackendUnavailable(
                f"workspace root is not a directory: {root}"
            )
        if cwd is not None:
            cwd_path = Path(cwd).resolve()
            try:
                cwd_path.relative_to(root)
            except ValueError as exc:
                raise SandboxBackendUnavailable(
                    f"cwd is outside the sandbox workspace: {cwd_path}"
                ) from exc
            if not cwd_path.is_dir():
                raise SandboxBackendUnavailable(
                    f"sandbox cwd is not a directory: {cwd_path}"
                )
        profile = _sandbox_exec_profile(root, network=self.network)
        return ["sandbox-exec", "-p", profile, *command]


def _sandbox_exec_profile(workspace_root: Path, *, network: bool) -> str:
    root = _escape_sandbox_literal(str(workspace_root))
    network_rules = (
        "(allow network*)"
        if network
        else "(deny network-outbound)\n(deny network-inbound)"
    )
    return f"""(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow file-read*)
(allow file-write* (subpath \"{root}\"))
(allow file-write* (subpath \"/tmp\"))
(allow file-write* (subpath \"/private/tmp\"))
(allow sysctl-read)
{network_rules}
"""


def _escape_sandbox_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


# --- helpers --------------------------------------------------------------


def _backend_name(tier: SandboxTier) -> str:
    if tier == SANDBOX_TIER_DOCKER:
        return "docker"
    if tier == SANDBOX_TIER_BWRAP:
        if sys.platform == "darwin":
            return "sandbox-exec"
        return "bubblewrap"
    return "scoped"
