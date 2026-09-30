from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ash.commands.sandbox import (
    build_sandbox_image,
    render_sandbox_status,
    sandbox_status,
)
from ash.config import AshConfig


def test_sandbox_status_uses_user_configuration(tmp_path) -> None:
    config = AshConfig(
        workspace_root=tmp_path,
        sandbox_backend="direct",
        sandbox_network=False,
        sandbox_docker_image="company/sandbox:v1",
    )

    status = sandbox_status(config)

    assert status["requested_backend"] == "direct"
    assert status["backend"] == "scoped"
    assert status["isolated"] is False
    assert status["aggregate_resource_limits"] is False
    assert status["remediation"] == ""


def test_sandbox_status_auto_approve_uses_resource_bounded_backend(
    tmp_path: Path,
) -> None:
    config = AshConfig(
        workspace_root=tmp_path,
        safety_tier="auto_approve",
    )
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        status = sandbox_status(config)

    assert status["backend"] == "docker"
    assert status["isolated"] is True
    assert status["aggregate_resource_limits"] is True


def test_render_sandbox_status_supports_text_and_json() -> None:
    status = {
        "requested_backend": "auto",
        "backend": "scoped",
        "tier": 1,
        "isolated": False,
        "filesystem": "host",
        "network": "host",
        "aggregate_resource_limits": False,
        "fail_closed": True,
        "available": {"scoped": True, "docker": False},
        "detail": "Direct execution.",
        "remediation": "Install a sandbox.",
    }

    rendered = render_sandbox_status(status)
    payload = json.loads(render_sandbox_status(status, json_output=True))

    assert "Isolation: disabled" in rendered
    assert "Aggregate resource limits: disabled" in rendered
    assert "Action: Install a sandbox." in rendered
    assert payload == status


def test_build_sandbox_image_uses_packaged_dockerfile() -> None:
    completed = subprocess.CompletedProcess([], 0)
    with (
        patch(
            "ash.commands.sandbox.resolve_host_executable",
            return_value="/usr/bin/docker",
        ),
        patch(
            "ash.commands.sandbox.run_docker_cli_sync",
            return_value=completed,
        ) as run,
    ):
        assert build_sandbox_image("ash-sandbox:test") == 0

    argv = run.call_args.args[0]
    assert argv[:4] == [
        "/usr/bin/docker",
        "build",
        "--tag",
        "ash-sandbox:test",
    ]
    assert argv[argv.index("--file") + 1].endswith("sandbox/Dockerfile")
    assert run.call_args.kwargs["workspace_root"] == Path.cwd().resolve()


def test_build_sandbox_image_requires_docker() -> None:
    with patch("ash.commands.sandbox.resolve_host_executable", return_value=None):
        with pytest.raises(RuntimeError, match="Docker CLI"):
            build_sandbox_image("ash-sandbox:test")


def test_build_sandbox_image_rejects_workspace_shadowed_docker(
    tmp_path: Path, monkeypatch
) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(RuntimeError, match="Docker CLI"):
        build_sandbox_image("ash-sandbox:test", workspace_root=tmp_path)
