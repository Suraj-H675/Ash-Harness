"""Top-level sandbox readiness and image setup commands."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ash.sandbox import SandboxManager
from ash.sandbox.docker import run_docker_cli_sync
from ash.sandbox.process_utils import ProcessTreeError
from ash.safety.environment import resolve_host_executable

if TYPE_CHECKING:
    from ash.config import AshConfig


def sandbox_status(config: AshConfig) -> dict[str, Any]:
    manager = SandboxManager(
        workspace_root=config.workspace_root,
        network=config.sandbox_network,
        backend_preference=config.sandbox_backend,
        docker_image=config.sandbox_docker_image,
        docker_memory_mb=config.sandbox_docker_memory_mb,
        docker_cpus=config.sandbox_docker_cpus,
        require_resource_containment=(
            config.safety_tier == "auto_approve"
            and not config.allow_unsafe_auto_approve
        ),
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
        "Aggregate resource limits: "
        + ("enabled" if status["aggregate_resource_limits"] else "disabled"),
        f"Fail closed: {'yes' if status['fail_closed'] else 'no'}",
        f"Available: {available}",
        str(status["detail"]),
    ]
    if status.get("remediation"):
        lines.append(f"Action: {status['remediation']}")
    return "\n".join(lines)


def build_sandbox_image(
    image: str, *, workspace_root: str | Path | None = None
) -> int:
    """Build the packaged baseline image after an explicit user command."""

    workspace = Path(workspace_root or Path.cwd()).resolve()
    docker = resolve_host_executable(
        "docker", workspace_root=workspace, cwd=workspace
    )
    if docker is None:
        raise RuntimeError("Docker CLI is not installed or is not on PATH")
    resource = files("ash.sandbox").joinpath("Dockerfile")
    dockerfile = Path(str(resource))
    if not dockerfile.is_file():
        raise RuntimeError("packaged sandbox Dockerfile is missing")
    try:
        result = run_docker_cli_sync(
            [
                docker,
                "build",
                "--tag",
                image,
                "--file",
                str(dockerfile),
                str(dockerfile.parent),
            ],
            workspace_root=workspace,
        )
    except ProcessTreeError as exc:
        raise RuntimeError(f"Docker build could not be managed safely: {exc}") from exc
    return result.returncode
