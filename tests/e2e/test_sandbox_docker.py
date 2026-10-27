from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ash.sandbox import SandboxManager


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("ASH_RUN_DOCKER_TESTS") != "1",
    reason="set ASH_RUN_DOCKER_TESTS=1 on a host with Docker",
)
def test_docker_sandbox_enforces_aggregate_cgroup_limits(tmp_path: Path) -> None:
    image = os.environ.get("ASH_DOCKER_TEST_IMAGE", "ash-sandbox:ci")
    manager = SandboxManager(
        workspace_root=tmp_path,
        backend_preference="docker",
        network=False,
        workspace_read_only=True,
        docker_image=image,
        docker_memory_mb=128,
        docker_cpus=0.5,
        require_resource_containment=True,
    )

    assert manager.backend_name == "docker"
    assert manager.has_aggregate_resource_limits() is True

    script = r"""
set -eu
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    echo MODE=v2
    echo MEMORY=$(cat /sys/fs/cgroup/memory.max)
    echo CPU=$(cat /sys/fs/cgroup/cpu.max)
    echo PIDS=$(cat /sys/fs/cgroup/pids.max)
else
    echo MODE=v1
    echo MEMORY=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)
    echo CPU_QUOTA=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us)
    echo CPU_PERIOD=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)
    echo PIDS=$(cat /sys/fs/cgroup/pids/pids.max)
fi
"""
    result = asyncio.run(
        manager.run_supervised_process(
            ["/bin/sh", "-c", script],
            cwd=tmp_path,
            timeout=30,
        )
    )

    assert result.exit_code == 0, result.stderr
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip()

    assert values["MEMORY"] == str(128 * 1024 * 1024)
    assert values["PIDS"] == "256"
    if values["MODE"] == "v2":
        quota_text, period_text = values["CPU"].split()
        assert quota_text != "max"
        assert int(quota_text) / int(period_text) == pytest.approx(0.5)
    else:
        assert int(values["CPU_QUOTA"]) / int(values["CPU_PERIOD"]) == pytest.approx(
            0.5
        )


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("ASH_RUN_DOCKER_TESTS") != "1",
    reason="set ASH_RUN_DOCKER_TESTS=1 on a host with Docker",
)
def test_supervised_docker_process_cannot_see_host_resources(
    tmp_path: Path,
) -> None:
    image = os.environ.get("ASH_DOCKER_TEST_IMAGE", "ash-sandbox:ci")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "host-only-sentinel.txt"
    sentinel.write_text("synthetic host-only marker", encoding="utf-8")

    manager = SandboxManager(
        workspace_root=workspace,
        backend_preference="docker",
        network=False,
        workspace_read_only=True,
        docker_image=image,
        docker_memory_mb=128,
        docker_cpus=0.5,
        require_resource_containment=True,
    )
    assert manager.backend_name == "docker"

    marker = f"ash-host-probe-{uuid.uuid4().hex}"
    host_probe = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", marker],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            "HOME": str(tmp_path),
            "TMPDIR": os.environ.get("TMPDIR", str(tmp_path)),
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    script = """
from pathlib import Path
import socket
import sys

sentinel, host_pid, marker = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    Path(sentinel).read_text(encoding="utf-8")
except OSError:
    print("HOST_READ=blocked")
else:
    print("HOST_READ=visible")
interfaces = {name for _, name in socket.if_nameindex()}
print("NETWORK=loopback-only" if interfaces == {"lo"} else "NETWORK=attached")
try:
    host_cmdline = (Path("/proc") / str(host_pid) / "cmdline").read_bytes()
except OSError:
    host_visible = False
else:
    host_visible = marker.encode() in host_cmdline
print(f"HOST_PID_VISIBLE={host_visible}")
"""
    try:
        result = asyncio.run(
            manager.run_supervised_process(
                [
                    "/usr/local/bin/python",
                    "-c",
                    script,
                    str(sentinel),
                    str(host_probe.pid),
                    marker,
                ],
                cwd=workspace,
                timeout=30,
            )
        )
    finally:
        host_probe.terminate()
        try:
            host_probe.wait(timeout=5)
        except subprocess.TimeoutExpired:
            host_probe.kill()
            host_probe.wait(timeout=5)

    assert result.exit_code == 0, result.stderr
    assert result.backend_name == "docker"
    assert result.fallback_used is False
    assert "HOST_READ=blocked" in result.stdout
    assert "NETWORK=loopback-only" in result.stdout
    assert "HOST_PID_VISIBLE=False" in result.stdout


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("ASH_RUN_DOCKER_TESTS") != "1",
    reason="set ASH_RUN_DOCKER_TESTS=1 on a host with Docker",
)
def test_supervised_docker_timeout_removes_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox.docker import run_docker_cli_sync

    image = os.environ.get("ASH_DOCKER_TEST_IMAGE", "ash-sandbox:ci")
    manager = SandboxManager(
        workspace_root=tmp_path,
        backend_preference="docker",
        network=False,
        workspace_read_only=True,
        docker_image=image,
        docker_memory_mb=128,
        docker_cpus=0.5,
        require_resource_containment=True,
    )
    assert manager.backend_name == "docker"
    docker_environment = asyncio.run(manager._capture_supervised_docker_environment())
    monkeypatch.setattr(
        manager,
        "_capture_supervised_docker_environment",
        AsyncMock(return_value=docker_environment),
    )
    token = uuid.uuid4().hex
    monkeypatch.setattr("ash.sandbox.manager.secrets.token_hex", lambda _size: token)
    container_name = "ash-supervised-" + token
    script = """
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
"""

    result = asyncio.run(
        manager.run_supervised_process(
            ["/usr/local/bin/python", "-c", script],
            cwd=tmp_path,
            timeout=2,
        )
    )

    assert result.timed_out is True
    containers = run_docker_cli_sync(
        [
            shutil.which("docker") or "docker",
            "ps",
            "-a",
            "--filter",
            f"name={container_name}",
            "--format",
            "{{.Names}}",
        ],
        workspace_root=tmp_path,
        capture_output=True,
        timeout=10,
        env=docker_environment,
    )
    assert containers.returncode == 0, containers.stderr.decode(
        "utf-8", errors="replace"
    )
    assert container_name not in containers.stdout.decode("utf-8", errors="replace")
