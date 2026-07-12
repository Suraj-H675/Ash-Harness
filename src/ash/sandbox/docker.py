"""Docker container sandbox wrapper.

Builds a ``docker run`` argv that launches an ephemeral container with
the project root mounted read-write, an output directory, and no
network by default. The container is removed automatically after the
command exits.

If Docker is not installed on the host, :meth:`DockerSandbox.wrap`
raises :class:`SandboxBackendUnavailable`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ash.sandbox._base import (
    SANDBOX_TIER_DOCKER,
    SandboxBackend,
    SandboxBackendUnavailable,
)


DEFAULT_IMAGE = "ash-sandbox:latest"


@dataclass(frozen=True)
class DockerSandbox(SandboxBackend):
    """Build a ``docker run`` argv that sandboxes a child process."""

    name: str = "docker"
    tier: int = SANDBOX_TIER_DOCKER

    image: str = DEFAULT_IMAGE
    workspace_root: Path | None = None
    output_dir: Path | None = None
    network: bool = False
    workspace_read_only: bool = False
    memory_limit: str | None = None
    cpus: float | None = None
    docker_path: str | None = None

    def __post_init__(self) -> None:
        if self.docker_path is None:
            resolved = shutil.which("docker")
            object.__setattr__(self, "docker_path", resolved)

    def is_available(self) -> bool:
        if self.docker_path is None:
            return False
        if sys.platform == "win32" and self.docker_path.endswith(".exe") is False:
            # Still allowed — Docker Desktop ships docker.exe on Windows.
            pass
        return Path(self.docker_path).exists()

    def wrap(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        passthrough_env_names: Sequence[str] = (),
    ) -> list[str]:
        """Build a full ``docker run --rm … image command`` argv list."""

        if not self.is_available():
            raise SandboxBackendUnavailable(
                f"docker not available at {self.docker_path or 'PATH'!r}"
            )
        if not command:
            raise ValueError("command must be a non-empty sequence")

        args: list[str] = [
            self.docker_path or "docker",
            "run",
            "--rm",
            "--interactive",
            "--init",
            "--pids-limit=256",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m",
            "--env",
            "HOME=/tmp",
        ]
        if sys.platform != "win32" and hasattr(os, "getuid"):
            args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        if not self.network:
            args.append("--network=none")
        if self.memory_limit is not None:
            args.extend(["--memory", self.memory_limit])
        if self.cpus is not None:
            args.extend(["--cpus", str(self.cpus)])
        for name in passthrough_env_names:
            args.extend(["--env", name])

        # Bind-mount the workspace and the output directory if given.
        container_cwd: str | None = None
        if self.workspace_root is not None:
            root = Path(self.workspace_root).resolve()
            if not root.is_dir():
                raise SandboxBackendUnavailable(
                    f"workspace root is not a directory: {root}"
                )
            mount = f"type=bind,source={root},target=/workspace"
            if self.workspace_read_only:
                mount += ",readonly"
            args.extend(["--mount", mount])
            container_cwd = "/workspace"
        if self.output_dir is not None:
            out = Path(self.output_dir).resolve()
            out.mkdir(parents=True, exist_ok=True)
            args.extend(["--mount", f"type=bind,source={out},target=/output"])
        if cwd is not None:
            cwd_path = Path(cwd).resolve()
            if self.workspace_root is None:
                raise SandboxBackendUnavailable(
                    "a workspace root is required when cwd is set"
                )
            try:
                relative = cwd_path.relative_to(Path(self.workspace_root).resolve())
            except ValueError as exc:
                raise SandboxBackendUnavailable(
                    f"cwd is outside the sandbox workspace: {cwd_path}"
                ) from exc
            if not cwd_path.is_dir():
                raise SandboxBackendUnavailable(
                    f"sandbox cwd is not a directory: {cwd_path}"
                )
            container_cwd = str(Path("/workspace") / relative).replace("\\", "/")
        if container_cwd is not None:
            args.extend(["--workdir", container_cwd])

        # Tighten the security profile: drop all capabilities, mark
        # the container read-only at the rootfs level, and prevent
        # any new privileges.
        args.extend(
            [
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--read-only",
            ]
        )

        # Image name + command.
        args.append(self.image)
        args.extend(command)
        return args


def probe_docker(*, image: str = DEFAULT_IMAGE) -> str | None:
    """Return Docker's path only when its daemon and sandbox image are ready."""

    path = shutil.which("docker")
    if path is None:
        return None
    try:
        daemon = subprocess.run(
            [path, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        if daemon.returncode != 0 or not daemon.stdout.strip():
            return None
        image_check = subprocess.run(
            [path, "image", "inspect", image],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return path if image_check.returncode == 0 else None
