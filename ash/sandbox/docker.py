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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ash.sandbox._base import SANDBOX_TIER_DOCKER, SandboxBackend, SandboxBackendUnavailable


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

    def wrap(self, command: Sequence[str], *, cwd: Path | None = None) -> list[str]:
        """Build a full ``docker run --rm … image command`` argv list."""

        if not self.is_available():
            raise SandboxBackendUnavailable(
                f"docker not available at {self.docker_path or 'PATH'!r}"
            )
        if not command:
            raise ValueError("command must be a non-empty sequence")

        args: list[str] = [self.docker_path or "docker", "run", "--rm", "--interactive"]
        if not self.network:
            args.append("--network=none")
        if self.memory_limit is not None:
            args.extend(["--memory", self.memory_limit])
        if self.cpus is not None:
            args.extend(["--cpus", str(self.cpus)])

        # Bind-mount the workspace and the output directory if given.
        if self.workspace_root is not None:
            root = Path(self.workspace_root).resolve()
            args.extend(["--volume", f"{root}:/workspace:rw"])
        if self.output_dir is not None:
            out = Path(self.output_dir).resolve()
            out.mkdir(parents=True, exist_ok=True)
            args.extend(["--volume", f"{out}:/output:rw"])
        if cwd is not None:
            args.extend(["--workdir", str(Path(cwd).resolve())])

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


def probe_docker() -> str | None:
    """Return the path to ``docker`` if installed, else ``None``."""

    return shutil.which("docker")
