"""Top-level sandbox readiness and image setup commands."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ash.sandbox import SandboxManager

if TYPE_CHECKING:
    from ash.config import AshConfig


def sandbox_status(config: AshConfig) -> dict[str, Any]:
    manager = SandboxManager(
        workspace_root=config.workspace_root,
        network=config.sandbox_network,
        backend_preference=config.sandbox_backend,
        docker_image=config.sandbox_docker_image,
    )
    return dict(manager.status())


def render_sandbox_status(status: dict[str, Any], *, json_output: bool = False) -> str:
    if json_output:
        return json.dumps(status, sort_keys=True)
    available = ", ".join(
        f"{name}={'yes' if ready else 'no'}"
        for name, ready in status["available"].items()
    )
    lines = [
        f"Backend: {status['backend']} (requested={status['requested_backend']}, tier={status['tier']})",
        f"Isolation: {'enabled' if status['isolated'] else 'disabled'}",
        f"Filesystem: {status['filesystem']}",
        f"Network: {status['network']}",
        f"Fail closed: {'yes' if status['fail_closed'] else 'no'}",
        f"Available: {available}",
        str(status["detail"]),
    ]
    if status.get("remediation"):
        lines.append(f"Action: {status['remediation']}")
    return "\n".join(lines)


def build_sandbox_image(image: str) -> int:
    """Build the packaged baseline image after an explicit user command."""

    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker CLI is not installed or is not on PATH")
    resource = files("ash.sandbox").joinpath("Dockerfile")
    dockerfile = Path(str(resource))
    if not dockerfile.is_file():
        raise RuntimeError("packaged sandbox Dockerfile is missing")
    result = subprocess.run(
        [
            docker,
            "build",
            "--tag",
            image,
            "--file",
            str(dockerfile),
            str(dockerfile.parent),
        ],
        check=False,
    )
    return result.returncode
