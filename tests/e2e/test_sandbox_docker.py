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


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("ASH_RUN_DOCKER_TESTS") != "1",
    reason="set ASH_RUN_DOCKER_TESTS=1 on a host with Docker",
)
def test_supervised_coworker_syntax_checks_isolate_concurrent_sessions_and_restart(
    tmp_path: Path,
) -> None:
    import json

    from ash.coworkers import CoworkerDirectory

    ash_dir = tmp_path / "ash"
    profiles = ash_dir / "profiles"
    profiles.mkdir(parents=True)
    workspaces = []
    for profile_name, source in (
        ("alpha", "raise RuntimeError('syntax check must not execute source')\n"),
        ("beta", "def broken(:\n"),
    ):
        (profiles / profile_name).mkdir()
        workspace = tmp_path / f"{profile_name}-workspace"
        workspace.mkdir()
        (workspace / "selected.py").write_text(source, encoding="utf-8")
        (workspace / "not-selected.py").write_text(
            "def must_not_be_staged(:\n",
            encoding="utf-8",
        )
        workspaces.append(workspace)

    directory = CoworkerDirectory.open(ash_dir)
    identities = [
        directory.create(
            owner_id="local-owner-label",
            display_name=profile_name,
            profile_name=profile_name,
            workspace=workspace,
        )
        for profile_name, workspace in zip(("alpha", "beta"), workspaces, strict=True)
    ]
    handles = [
        directory.open_coworker(
            owner_id="local-owner-label",
            coworker_id=identity.coworker_id,
        )
        for identity in identities
    ]

    async def run_concurrently() -> list[object]:
        return list(
            await asyncio.gather(
                handles[0].check_python_syntax(
                    run_id="alpha-syntax",
                    permitted_files=("selected.py",),
                ),
                handles[1].check_python_syntax(
                    run_id="beta-syntax",
                    permitted_files=("selected.py",),
                ),
            )
        )

    reports = asyncio.run(run_concurrently())
    alpha_report, beta_report = reports
    assert alpha_report.valid is True
    assert beta_report.valid is False
    assert alpha_report.session_ref.coworker_id != beta_report.session_ref.coworker_id
    assert alpha_report.session_ref.session_id != beta_report.session_ref.session_id
    assert (
        alpha_report.session_ref.session_id
        != alpha_report.session_ref.canonical_session_id
    )
    assert (
        beta_report.session_ref.session_id != beta_report.session_ref.canonical_session_id
    )
    assert (
        alpha_report.session_ref.canonical_session_id
        != beta_report.session_ref.canonical_session_id
    )
    assert alpha_report.files[0].name == "selected.py"
    assert beta_report.files[0].name == "selected.py"
    assert beta_report.files[0].line == 1

    restart_code = """
import json, sys
from pathlib import Path
from ash.coworkers import CoworkerDirectory
directory = CoworkerDirectory.open(Path(sys.argv[1]))
results = []
for offset in (2, 5):
    handle = directory.open_coworker(
        owner_id=sys.argv[offset], coworker_id=sys.argv[offset + 1]
    )
    owner = directory._require_local_coworker_owner(handle)
    canonical = handle.ensure_canonical_conversation()
    run = handle.begin_background_run(sys.argv[offset + 2])
    results.append({
        'coworker_id': handle.identity.coworker_id,
        'owner_uid': owner.owner_uid,
        'canonical': canonical.session_id,
        'session': run.session_id,
        'run_id': run.run_id,
    })
print(json.dumps(results, separators=(',', ':')))
"""
    restarted = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            "-c",
            restart_code,
            str(ash_dir),
            "local-owner-label",
            identities[0].coworker_id,
            "alpha-syntax",
            "local-owner-label",
            identities[1].coworker_id,
            "beta-syntax",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={
            "HOME": str(tmp_path / "restart-home"),
            "XDG_CONFIG_HOME": str(tmp_path / "restart-config"),
            "XDG_DATA_HOME": str(tmp_path / "restart-data"),
            "XDG_CACHE_HOME": str(tmp_path / "restart-cache"),
            "TMPDIR": str(tmp_path / "restart-tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    persistent = json.loads(restarted.stdout)
    assert persistent == [
        {
            "coworker_id": report.session_ref.coworker_id,
            "owner_uid": identity.owner_uid,
            "canonical": report.session_ref.canonical_session_id,
            "session": report.session_ref.session_id,
            "run_id": report.session_ref.run_id,
        }
        for identity, report in zip(identities, (alpha_report, beta_report), strict=True)
    ]
