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
from typing import Sequence, TypedDict

from sandbox._base import (
    SANDBOX_TIER_BWRAP,
    SANDBOX_TIER_DOCKER,
    SANDBOX_TIER_SCOPED,
    SandboxBackend,
    SandboxBackendUnavailable,
    SandboxTier,
)
from sandbox.bwrap import BubblewrapSandbox, probe_bwrap
from sandbox.docker import DEFAULT_IMAGE, DockerSandbox, probe_docker
from sandbox.process_utils import (
    ProcessStreamCallback,
    communicate_process,
    process_group_options,
    terminate_process_tree,
)


# Re-export the tier constants for backwards compatibility with code
# that imports them from sandbox.manager.
__all__ = [
    "SANDBOX_TIER_BWRAP",
    "SANDBOX_TIER_DOCKER",
    "SANDBOX_TIER_SANDBOX_EXEC",
    "SANDBOX_TIER_SCOPED",
    "SandboxBackendUnavailable",
    "SandboxManager",
    "SandboxInvocation",
    "SandboxResult",
    "SandboxTier",
    "has_bwrap",
    "has_docker",
    "has_sandbox_exec",
    "auto_approve_safety_error",
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


@dataclass(frozen=True)
class SandboxInvocation:
    """A prepared subprocess invocation with its effective isolation metadata."""

    argv: tuple[str, ...]
    cwd: Path | None
    tier: SandboxTier
    backend_name: str
    fallback_used: bool = False


class SandboxStatus(TypedDict):
    requested_backend: str
    backend: str
    tier: SandboxTier
    isolated: bool
    filesystem: str
    network: str
    fail_closed: bool
    available: dict[str, bool]
    detail: str
    remediation: str


# --- probe helpers ---------------------------------------------------------


def has_bwrap() -> bool:
    """Whether ``bwrap`` is on PATH and the host is Linux."""

    return sys.platform.startswith("linux") and probe_bwrap() is not None


def has_sandbox_exec() -> bool:
    """Whether ``sandbox-exec`` is available (macOS only)."""

    if sys.platform != "darwin":
        return False
    return shutil.which("sandbox-exec") is not None


def has_docker(image: str = DEFAULT_IMAGE) -> bool:
    """Whether Docker's daemon and selected local image are ready."""

    return probe_docker(image=image) is not None


def auto_approve_safety_error(
    manager: "SandboxManager",
    *,
    allow_unsafe: bool,
) -> str | None:
    """Explain why full-auto execution is unsafe, or return ``None``."""

    if manager.is_fully_isolated() or allow_unsafe:
        return None
    return (
        "auto_approve requires an available OS sandbox; "
        f"the active backend is {manager.backend_name}, which does not isolate "
        "filesystem or network access. Use interactive/auto_edit mode, install "
        "a supported sandbox, or explicitly set ASH_ALLOW_UNSAFE_AUTO_APPROVE=true."
    )


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
        The strongest tier the caller is willing to use. Native isolation is
        preferred for host-tool compatibility, with a verified container as
        the portable fallback. Defaults to the maximum (3).
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
    backend_preference: str = "auto"
    docker_image: str = DEFAULT_IMAGE

    def __post_init__(self) -> None:
        if self.preferred_tier not in {
            SANDBOX_TIER_SCOPED,
            SANDBOX_TIER_BWRAP,
            SANDBOX_TIER_DOCKER,
        }:
            raise ValueError("preferred_tier must be 1, 2, or 3")
        self.backend_preference = self.backend_preference.strip().casefold()
        if self.backend_preference not in {"auto", "native", "docker", "direct"}:
            raise ValueError(
                "backend_preference must be auto, native, docker, or direct"
            )
        if not self.docker_image.strip():
            raise ValueError("docker_image must not be empty")
        self._available: dict[str, bool] = {"scoped": True}
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
            "bwrap": self._backend_available("bwrap"),
            "sandbox_exec": self._backend_available("sandbox_exec"),
            "docker": self._backend_available("docker"),
        }

    def status(self) -> SandboxStatus:
        """Return stable, user-facing enforcement and backend diagnostics."""

        isolated = self.is_fully_isolated()
        if isolated:
            filesystem = "workspace-write"
            network = "enabled" if self.network else "blocked"
            detail = (
                "Commands are isolated to the workspace and temporary storage; "
                f"network access is {network}."
            )
        else:
            filesystem = "host"
            network = "host"
            detail = (
                "Commands run as the current user without OS filesystem or network "
                "isolation and require permission-policy control."
            )
        return {
            "requested_backend": self.backend_preference,
            "backend": self.backend_name,
            "tier": self.tier,
            "isolated": isolated,
            "filesystem": filesystem,
            "network": network,
            "fail_closed": not self.allow_scoped_fallback,
            "available": self.capabilities(),
            "detail": detail,
            "remediation": (
                ""
                if isolated or self.backend_preference == "direct"
                else _sandbox_remediation(
                    preference=self.backend_preference,
                    docker_image=self.docker_image,
                )
            ),
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
        stream_callback: ProcessStreamCallback | None = None,
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
        invocation = self.prepare(command, cwd=cwd)
        if invocation.tier == SANDBOX_TIER_SCOPED:
            return await _run_scoped(
                _ScopedBackend(),
                invocation.argv,
                invocation.cwd,
                deadline,
                fallback=invocation.fallback_used,
                env=env,
                stream_callback=stream_callback,
            )

        return await _run_subprocess(
            list(invocation.argv),
            cwd=invocation.cwd,
            deadline=deadline,
            tier=invocation.tier,
            backend_name=invocation.backend_name,
            env=env,
            stream_callback=stream_callback,
        )

    def prepare(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> SandboxInvocation:
        """Prepare a command for foreground or managed background execution."""

        if not command:
            raise ValueError("command must be a non-empty sequence")
        try:
            backend = self._build_backend(self._tier)
            wrapped = backend.wrap(command, cwd=cwd)
        except SandboxBackendUnavailable:
            if not self.allow_scoped_fallback:
                raise
            return SandboxInvocation(
                tuple(command),
                cwd,
                SANDBOX_TIER_SCOPED,
                "scoped",
                fallback_used=True,
            )
        return SandboxInvocation(
            tuple(wrapped),
            cwd,
            backend.tier,
            backend_name=backend.name,
        )

    # --- tier detection --------------------------------------------------

    def _detect_tier(self) -> SandboxTier:
        """Prefer a compatible native sandbox, then a verified container."""

        if self.backend_preference == "direct":
            return SANDBOX_TIER_SCOPED
        use_native = self.backend_preference in {"auto", "native"}
        use_docker = self.backend_preference in {"auto", "docker"}
        if use_native and self.preferred_tier >= SANDBOX_TIER_BWRAP:
            if sys.platform.startswith("linux") and self._backend_available("bwrap"):
                return SANDBOX_TIER_BWRAP
            if sys.platform == "darwin" and self._backend_available("sandbox_exec"):
                return SANDBOX_TIER_BWRAP
        if (
            use_docker
            and self.preferred_tier >= SANDBOX_TIER_DOCKER
            and self._backend_available("docker")
        ):
            return SANDBOX_TIER_DOCKER
        return SANDBOX_TIER_SCOPED

    def _backend_available(self, name: str) -> bool:
        cached = self._available.get(name)
        if cached is not None:
            return cached
        probes = {
            "bwrap": has_bwrap,
            "sandbox_exec": has_sandbox_exec,
            "docker": lambda: has_docker(self.docker_image),
        }
        available = probes[name]()
        self._available[name] = available
        return available

    def _build_backend(self, tier: SandboxTier) -> SandboxBackend:
        if tier == SANDBOX_TIER_DOCKER:
            docker_backend = DockerSandbox(
                workspace_root=self.workspace_root,
                network=self.network,
                image=self.docker_image,
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
    stream_callback: ProcessStreamCallback | None = None,
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
            communicate_process(process, stream_callback=stream_callback),
            timeout=deadline,
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
    stream_callback: ProcessStreamCallback | None = None,
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
            communicate_process(process, stream_callback=stream_callback),
            timeout=deadline,
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


def _sandbox_remediation(*, preference: str, docker_image: str) -> str:
    if preference == "docker":
        return (
            "Start Docker and build or provide the configured local image "
            f"{docker_image}."
        )
    if preference == "native":
        if sys.platform.startswith("linux"):
            return "Install and enable bubblewrap."
        if sys.platform == "darwin":
            return "Ensure /usr/bin/sandbox-exec is available."
        return "No native sandbox backend is supported on this platform."
    if sys.platform.startswith("linux"):
        return (
            "Install and enable bubblewrap, or start Docker and provide the "
            f"{docker_image} image."
        )
    if sys.platform == "darwin":
        return (
            "Ensure /usr/bin/sandbox-exec is available, or start Docker and provide "
            f"the {docker_image} image."
        )
    if sys.platform == "win32":
        return (
            "Start Docker Desktop and provide the "
            f"{docker_image} image; native Windows isolation is not "
            "currently available."
        )
    return f"Start Docker and provide the {docker_image} image."
