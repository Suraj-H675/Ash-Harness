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

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ash.sandbox._base import (
    SANDBOX_TIER_BWRAP,
    SandboxBackend,
    SandboxBackendUnavailable,
)


# Bwrap flags we always pass for hardened defaults.
_BWRAP_BASE_FLAGS: tuple[str, ...] = (
    "--unshare-user-try",  # create a new user namespace if allowed
    "--unshare-pid",  # new PID namespace
    "--unshare-uts",  # new UTS namespace (hostname isolation)
    "--unshare-ipc",  # new IPC namespace
    "--die-with-parent",  # kill the sandbox if the parent dies
    "--new-session",  # new session
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
    bwrap_path: str | None = None

    def __post_init__(self) -> None:
        # Resolve the bwrap binary lazily; the manager decides whether
        # to use this backend based on the probe result.
        if self.bwrap_path is None:
            resolved = shutil.which("bwrap")
            if resolved is not None:
                object.__setattr__(self, "bwrap_path", resolved)

    def is_available(self) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        if self.bwrap_path is None:
            return False
        return Path(self.bwrap_path).exists()

    def wrap(self, command: Sequence[str], *, cwd: Path | None = None) -> list[str]:
        """Build a full ``bwrap … -- command`` argv list."""

        if not self.is_available():
            raise SandboxBackendUnavailable(
                f"bwrap not available at {self.bwrap_path or 'PATH'!r}"
            )
        if not command:
            raise ValueError("command must be a non-empty sequence")

        args: list[str] = [self.bwrap_path or "bwrap"]
        args.extend(_BWRAP_BASE_FLAGS)

        # Mount a fresh /tmp so the sandbox cannot tamper with host temp.
        args.extend(["--tmpfs", "/tmp"])

        # Read-only system mounts so basic commands can resolve their
        # loaders; the list is intentionally conservative.
        for ro in ("/usr", "/bin", "/lib", "/lib64", "/etc", "/dev"):
            if Path(ro).exists():
                ro_str = str(Path(ro))
                args.extend(["--ro-bind", ro_str, ro_str])

        # Workspace (read-write) — required for any meaningful work.
        if self.workspace_root is not None:
            root = Path(self.workspace_root).resolve()
            args.extend(["--bind", str(root), str(root)])

        # Scratch directory for ephemeral writes.
        scratch = self.scratch_dir
        if scratch is None:
            scratch = Path(tempfile_workspace_scratch(self.workspace_root))
        scratch = Path(scratch).resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        args.extend(["--bind", str(scratch), str(scratch)])

        # Additional read-only paths the caller wants to expose.
        for ro_entry in self.read_only_paths:
            ro_resolved: Path = Path(str(ro_entry)).resolve()
            if ro_resolved.exists():
                ro_str = str(ro_resolved)
                args.extend(["--ro-bind", ro_str, ro_str])

        if not self.network:
            # Block all network namespaces by clearing net namespace.
            args.append("--unshare-net")

        # If the caller supplied a cwd, bind-mount it into the sandbox
        # at the same path so child-relative paths still resolve.
        if cwd is not None:
            cwd_resolved = Path(cwd).resolve()
            if cwd_resolved.exists():
                args.extend(["--bind", str(cwd_resolved), str(cwd_resolved)])

        # Environment scrubbing: keep PATH and HOME minimal so the
        # child cannot inherit host secrets.
        args.append("--clearenv")
        args.extend(["--setenv", "PATH", "/usr/bin:/bin"])
        args.extend(["--setenv", "HOME", str(scratch)])

        # The bwrap command separator.
        args.append("--")
        args.extend(command)
        return args


def tempfile_workspace_scratch(workspace_root: Path | None) -> str:
    """Pick a scratch dir name under the workspace (or /tmp as a fallback)."""

    import tempfile

    base = Path(workspace_root) if workspace_root is not None else Path("/tmp")
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="ash-sandbox-", dir=str(base))


def probe_bwrap() -> str | None:
    """Return the path to ``bwrap`` if installed, else ``None``."""

    return shutil.which("bwrap")
