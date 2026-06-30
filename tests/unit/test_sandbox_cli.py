from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from cli.sandbox import build_sandbox_image, render_sandbox_status, sandbox_status
from config import AshConfig


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
    assert status["remediation"] == ""


def test_render_sandbox_status_supports_text_and_json() -> None:
    status = {
        "requested_backend": "auto",
        "backend": "scoped",
        "tier": 1,
        "isolated": False,
        "filesystem": "host",
        "network": "host",
        "fail_closed": True,
        "available": {"scoped": True, "docker": False},
        "detail": "Direct execution.",
        "remediation": "Install a sandbox.",
    }

    rendered = render_sandbox_status(status)
    payload = json.loads(render_sandbox_status(status, json_output=True))

    assert "Isolation: disabled" in rendered
    assert "Action: Install a sandbox." in rendered
    assert payload == status


def test_build_sandbox_image_uses_packaged_dockerfile() -> None:
    completed = subprocess.CompletedProcess([], 0)
    with (
        patch("cli.sandbox.shutil.which", return_value="/usr/bin/docker"),
        patch("cli.sandbox.subprocess.run", return_value=completed) as run,
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


def test_build_sandbox_image_requires_docker() -> None:
    with patch("cli.sandbox.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="Docker CLI"):
            build_sandbox_image("ash-sandbox:test")
