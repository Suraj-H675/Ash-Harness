from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from ash.sandbox import SandboxManager


@pytest.mark.skipif(
    shutil.which("docker") is None
    or os.environ.get("ASH_RUN_DOCKER_TESTS") != "1",
    reason="set ASH_RUN_DOCKER_TESTS=1 on a host with Docker",
)
def test_docker_sandbox_enforces_aggregate_cgroup_limits(tmp_path: Path) -> None:
    image = os.environ.get("ASH_DOCKER_TEST_IMAGE", "ash-sandbox:ci")
    manager = SandboxManager(
        workspace_root=tmp_path,
        backend_preference="docker",
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
        manager.run(
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
