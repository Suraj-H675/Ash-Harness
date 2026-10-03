"""Bubblewrap (bwrap) sandbox wrapper.

Bubblewrap is a Linux user-space tool that builds sandboxed environments
using mount namespaces, network namespaces, and seccomp filters. This
module is responsible for the *argv construction* only — the actual
``bwrap`` binary is invoked by the host's subprocess machinery.

If ``bwrap`` is not installed on the host, :meth:`BubblewrapSandbox.wrap`
raises :class:`SandboxBackendUnavailable` so the manager can fall back
to Tier 1.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ash.sandbox._base import (
    SANDBOX_TIER_BWRAP,
    SandboxBackend,
    SandboxBackendUnavailable,
)
from ash.safety.environment import build_scrubbed_environment, resolve_host_executable


MINIMUM_BWRAP_VERSION = (0, 12, 0)
_BWRAP_VERSION = re.compile(r"\bbubblewrap\s+(\d+)\.(\d+)\.(\d+)\b")

_SYSTEM_READ_ONLY_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/lib",
    "/lib64",
)

_ETC_COMPAT_READ_ONLY_PATHS: tuple[str, ...] = (
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/hosts",
    "/etc/resolv.conf",
    "/etc/gai.conf",
    "/etc/services",
    "/etc/protocols",
    "/etc/ssl",
    "/etc/pki",
    "/etc/ca-certificates",
    "/etc/crypto-policies",
    "/etc/localtime",
    "/etc/alternatives",
)


# Bwrap flags we always pass for hardened defaults.
_BWRAP_BASE_FLAGS: tuple[str, ...] = (
    "--unshare-user",  # a private user namespace is required for isolation
    "--unshare-pid",  # new PID namespace
    "--unshare-uts",  # new UTS namespace (hostname isolation)
    "--unshare-ipc",  # new IPC namespace
    "--disable-userns",  # child cannot create nested user namespaces
    "--assert-userns-disabled",  # fail closed if the lock-down was ineffective
    "--die-with-parent",  # kill the sandbox if the parent dies
)


@dataclass(frozen=True)
class BubblewrapSandbox(SandboxBackend):
    """Build a bwrap argv that isolates the child process.

    Parameters
    ----------
    workspace_root
        Filesystem path the sandbox can read and write.
    scratch_dir
        Optional scratch directory mounted read-write; defaults to a
        temp dir under the workspace.
    read_only_paths
        Extra paths to expose read-only (e.g. system libraries).
    network
        When ``False`` (default), the sandbox cannot reach the network.
    workspace_read_only
        Mount plugin or source code read-only while retaining writable tmpfs.
    bwrap_path
        Override the path to the ``bwrap`` binary. When ``None`` the
        manager probes ``shutil.which``.
    """

    name: str = "bubblewrap"
    tier: int = SANDBOX_TIER_BWRAP

    workspace_root: Path | None = None
    scratch_dir: Path | None = None
    read_only_paths: tuple[Path, ...] = ()
    network: bool = False
    workspace_read_only: bool = False
    bwrap_path: str | None = None

    def __post_init__(self) -> None:
        # Resolve the bwrap binary lazily; the manager decides whether
        # to use this backend based on the probe result.
        if self.bwrap_path is None:
            resolved = resolve_host_executable(
                "bwrap", workspace_root=self.workspace_root
            )
            if resolved is not None:
                object.__setattr__(self, "bwrap_path", resolved)

    def is_available(self) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        if self.bwrap_path is None:
            return False
        return Path(self.bwrap_path).exists()

    def wrap(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        passthrough_env_names: Sequence[str] = (),
        workspace_fd: int | None = None,
        read_only_fds: Sequence[tuple[int, Path]] | None = None,
        pty: bool = False,
    ) -> list[str]:
        """Build a full ``bwrap … -- command`` argv list."""

        if not self.is_available():
            raise SandboxBackendUnavailable(
                f"bwrap not available at {self.bwrap_path or 'PATH'!r}"
            )
        if not command:
            raise ValueError("command must be a non-empty sequence")

        args: list[str] = [self.bwrap_path or "bwrap"]
        args.extend(_BWRAP_BASE_FLAGS)
        if not pty:
            # Ordinary sandboxed commands get a fresh session so they cannot
            # interact with the caller's controlling terminal. PTY commands
            # already run in an Ash-owned new session with a dedicated PTY;
            # creating another session here would discard that controlling TTY.
            args.append("--new-session")

        # Mount a fresh /tmp so the sandbox cannot tamper with host temp.
        args.extend(["--tmpfs", "/tmp"])

        # Read-only system mounts so basic commands can resolve their loaders.
        # Do not expose the whole host /etc: it can contain machine identity,
        # organization configuration, and other host-readable state unrelated
        # to command execution.
        for ro in _SYSTEM_READ_ONLY_PATHS:
            if Path(ro).exists():
                ro_str = str(Path(ro))
                args.extend(["--ro-bind", ro_str, ro_str])
        args.extend(["--dir", "/etc"])
        for ro in _ETC_COMPAT_READ_ONLY_PATHS:
            if Path(ro).exists():
                args.extend(["--ro-bind", ro, ro])
        args.extend(["--proc", "/proc", "--dev", "/dev"])

        # Workspace access is caller-selected; commands default to read-write,
        # while executable extensions use a read-only code mount.
        if self.workspace_root is None:
            raise SandboxBackendUnavailable("bubblewrap requires a workspace root")
        if workspace_fd is not None and workspace_fd < 0:
            raise ValueError("workspace_fd must be non-negative")
        root = (
            Path(self.workspace_root).absolute()
            if workspace_fd is not None
            else Path(self.workspace_root).resolve()
        )
        if workspace_fd is not None:
            try:
                workspace_metadata = os.fstat(workspace_fd)
            except OSError as exc:
                raise SandboxBackendUnavailable(
                    "bubblewrap workspace descriptor is unavailable"
                ) from exc
            if not stat.S_ISDIR(workspace_metadata.st_mode):
                raise SandboxBackendUnavailable(
                    "bubblewrap workspace descriptor is not a directory"
                )
        elif not root.is_dir():
            raise SandboxBackendUnavailable(
                f"workspace root is not a directory: {root}"
            )
        if workspace_fd is not None:
            workspace_bind = (
                "--ro-bind-fd" if self.workspace_read_only else "--bind-fd"
            )
            args.extend([workspace_bind, str(workspace_fd), str(root)])
        else:
            workspace_bind = "--ro-bind" if self.workspace_read_only else "--bind"
            args.extend([workspace_bind, str(root), str(root)])

        # Scratch directory for ephemeral writes.
        scratch = Path(self.scratch_dir).resolve() if self.scratch_dir else Path("/tmp")
        if self.scratch_dir is not None:
            try:
                scratch.relative_to(root)
            except ValueError as exc:
                raise SandboxBackendUnavailable(
                    f"scratch directory is outside the sandbox workspace: {scratch}"
                ) from exc
            scratch.mkdir(parents=True, exist_ok=True)
            args.extend(["--bind", str(scratch), str(scratch)])

        # Additional read-only paths the caller wants to expose. Launching
        # callers can bind held descriptors so the host pathname cannot be
        # swapped after validation; plain argv previews retain legacy paths.
        if read_only_fds is not None:
            for descriptor, destination in read_only_fds:
                if descriptor < 0:
                    raise ValueError("read-only descriptor must be non-negative")
                args.extend(["--ro-bind-fd", str(descriptor), str(destination)])
        else:
            for ro_entry in self.read_only_paths:
                ro_resolved: Path = Path(str(ro_entry)).resolve()
                if ro_resolved.exists():
                    ro_str = str(ro_resolved)
                    args.extend(["--ro-bind", ro_str, ro_str])

        if not self.network:
            # Block all network namespaces by clearing net namespace.
            args.append("--unshare-net")

        # The working directory must already be part of the workspace mount.
        if cwd is not None:
            cwd_resolved = (
                Path(cwd).absolute() if workspace_fd is not None else Path(cwd).resolve()
            )
            try:
                cwd_resolved.relative_to(root)
            except ValueError as exc:
                raise SandboxBackendUnavailable(
                    f"cwd is outside the sandbox workspace: {cwd_resolved}"
                ) from exc
            if not cwd_resolved.is_dir():
                raise SandboxBackendUnavailable(
                    f"sandbox cwd is not a directory: {cwd_resolved}"
                )
            args.extend(["--chdir", str(cwd_resolved)])

        # The manager starts bwrap with an already scrubbed environment. Keep
        # it so explicitly allowlisted variables reach the child, while
        # replacing host-specific PATH and HOME with sandbox values.
        args.extend(["--setenv", "PATH", "/usr/bin:/bin"])
        args.extend(["--setenv", "HOME", str(scratch)])

        # The bwrap command separator.
        args.append("--")
        args.extend(command)
        return args


def probe_bwrap(*, workspace_root: Path | None = None) -> str | None:
    """Return a usable ``bwrap`` path, not merely an installed binary.

    Container hosts frequently expose the executable while denying the user or
    network namespaces Ash needs. A short capability probe prevents selecting a
    backend that will fail every command at runtime.
    """

    path = resolve_host_executable("bwrap", workspace_root=workspace_root)
    if path is None or not sys.platform.startswith("linux"):
        return None
    descriptor = -1
    try:
        version = subprocess.run(
            [path, "--version"],
            capture_output=True,
            check=False,
            timeout=5,
            env=build_scrubbed_environment(),
        )
        if version.returncode != 0:
            return None
        output = version.stdout.decode("utf-8", errors="replace").strip()
        match = _BWRAP_VERSION.search(output)
        if match is None:
            return None
        parsed_version = tuple(int(part) for part in match.groups())
        if parsed_version < MINIMUM_BWRAP_VERSION:
            return None
        descriptor = os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        result = subprocess.run(
            [
                path,
                "--unshare-user",
                "--unshare-pid",
                "--unshare-net",
                "--disable-userns",
                "--assert-userns-disabled",
                "--ro-bind-fd",
                str(descriptor),
                "/",
                "--",
                "true",
            ],
            pass_fds=(descriptor,),
            capture_output=True,
            check=False,
            timeout=5,
            env=build_scrubbed_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path if result.returncode == 0 else None
