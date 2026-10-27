"""Unit tests for the sandbox manager and backends (Sprint 11)."""

from __future__ import annotations

import asyncio
import io
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ash.sandbox import (
    BubblewrapSandbox,
    DockerSandbox,
    SANDBOX_TIER_BWRAP,
    SANDBOX_TIER_DOCKER,
    SANDBOX_TIER_SCOPED,
    SandboxBackendUnavailable,
    SandboxManager,
    SandboxResult,
    auto_approve_safety_error,
    has_bwrap,
    has_docker,
    has_sandbox_exec,
)
from ash.sandbox.bwrap import probe_bwrap
from ash.sandbox.docker import (
    DEFAULT_IMAGE,
    docker_cli_environment,
    probe_docker,
    run_docker_cli_sync,
)
from ash.safety.environment import resolve_host_executable
from ash.tools.base import ToolExecutionOutcome
from ash.tools.command import RunCommandTool


_SUPERVISED_DOCKER_ENVIRONMENT = {
    "HOME": "/tmp",
    "PATH": os.defpath,
    "DOCKER_HOST": "unix:///var/run/docker.sock",
}


def _stub_supervised_docker_environment(
    monkeypatch: pytest.MonkeyPatch,
    manager: SandboxManager,
    environment: dict[str, str] | None = None,
) -> None:
    monkeypatch.setattr(
        manager,
        "_capture_supervised_docker_environment",
        AsyncMock(return_value=dict(environment or _SUPERVISED_DOCKER_ENVIRONMENT)),
    )


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def test_has_bwrap_matches_probe() -> None:
    assert has_bwrap() is (
        probe_bwrap() is not None and sys.platform.startswith("linux")
    )


def test_has_docker_matches_probe() -> None:
    assert has_docker() is (probe_docker() is not None)


def test_has_sandbox_exec_only_on_macos() -> None:
    if sys.platform != "darwin":
        assert has_sandbox_exec() is False
    else:
        # On macOS, reflects whether a host binary is actually on PATH.
        assert has_sandbox_exec() is (
            resolve_host_executable("sandbox-exec") is not None
        )


@pytest.mark.parametrize(
    ("version_output", "expected_available"),
    [
        (b"bubblewrap 0.11.0\n", False),
        (b"bubblewrap 0.12.0\n", True),
        (b"bubblewrap 0.13.0\n", True),
        (b"unexpected version output\n", False),
    ],
)
def test_probe_bwrap_requires_security_supported_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_output: bytes,
    expected_available: bool,
) -> None:
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-provider-secret")
    calls: list[list[str]] = []
    environments: list[dict[str, str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        environments.append(dict(kwargs["env"]))
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=version_output, stderr=b"")
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        "ash.sandbox.bwrap.resolve_host_executable",
        lambda *args, **kwargs: str(fake),
    )
    monkeypatch.setattr("ash.sandbox.bwrap.subprocess.run", run)
    monkeypatch.setattr("ash.sandbox.bwrap.sys.platform", "linux")

    available = probe_bwrap(workspace_root=tmp_path)

    assert (available is not None) is expected_available
    assert calls[0][1:] == ["--version"]
    assert all("OPENAI_API_KEY" not in environment for environment in environments)
    if expected_available:
        assert len(calls) == 2
    else:
        assert len(calls) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_resolve_host_executable_skips_workspace_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    for directory in (workspace, host_bin):
        executable = directory / "helper"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(workspace), str(host_bin))))

    resolved = resolve_host_executable(
        "helper", workspace_root=workspace, cwd=workspace
    )

    assert resolved == str((host_bin / "helper").resolve())


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cwd")
@pytest.mark.asyncio
async def test_manager_run_honors_explicit_expected_cwd_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = workspace / "work"
    saved = workspace / "work-saved"
    replacement = workspace / "replacement"
    cwd.mkdir()
    replacement.mkdir()
    metadata = cwd.stat()
    expected = (metadata.st_dev, metadata.st_ino)
    manager = SandboxManager(workspace_root=workspace, backend_preference="direct")
    cwd.rename(saved)
    replacement.rename(cwd)

    with pytest.raises(
        SandboxBackendUnavailable,
        match="working directory identity changed",
    ):
        await manager.run(
            ["/bin/sh", "-c", "printf unsafe > marker.txt"],
            cwd=cwd,
            timeout=5,
            expected_cwd_identity=expected,
        )

    assert not (saved / "marker.txt").exists()
    assert not (cwd / "marker.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cwd")
@pytest.mark.asyncio
async def test_manager_run_refuses_replaced_workspace_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    manager = SandboxManager(workspace_root=workspace, backend_preference="direct")
    workspace.rename(saved)
    replacement.rename(workspace)

    with pytest.raises(
        SandboxBackendUnavailable,
        match="working directory identity changed",
    ):
        await manager.run(
            ["/bin/sh", "-c", "printf unsafe > marker.txt"],
            cwd=workspace,
            timeout=5,
        )

    assert not (saved / "marker.txt").exists()
    assert not (workspace / "marker.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cwd")
@pytest.mark.asyncio
async def test_manager_run_refuses_replaced_workspace_root_for_nested_cwd(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    saved = tmp_path / "workspace-saved"
    replacement = tmp_path / "replacement"
    replacement_nested = replacement / "nested"
    replacement_nested.mkdir(parents=True)
    manager = SandboxManager(workspace_root=workspace, backend_preference="direct")
    workspace.rename(saved)
    replacement.rename(workspace)

    with pytest.raises(
        SandboxBackendUnavailable,
        match="working directory identity changed",
    ):
        await manager.run(
            ["/bin/sh", "-c", "printf unsafe > marker.txt"],
            cwd=workspace / "nested",
            timeout=5,
        )

    assert not (saved / "nested" / "marker.txt").exists()
    assert not (workspace / "nested" / "marker.txt").exists()


def test_manager_rejects_unexpected_initial_workspace_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = workspace.stat()
    expected_identity = (metadata.st_dev, metadata.st_ino)
    workspace.rename(tmp_path / "workspace-original")
    workspace.mkdir()

    with pytest.raises(
        SandboxBackendUnavailable,
        match="sandbox workspace identity changed",
    ):
        SandboxManager(
            workspace_root=workspace,
            expected_workspace_identity=expected_identity,
            backend_preference="direct",
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cwd")
@pytest.mark.asyncio
async def test_manager_run_refuses_workspace_root_replaced_by_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    manager = SandboxManager(workspace_root=workspace, backend_preference="direct")
    workspace.rename(saved)
    try:
        workspace.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    with pytest.raises(
        SandboxBackendUnavailable,
        match="working directory identity changed",
    ):
        await manager.run(
            ["/bin/sh", "-c", "printf unsafe > marker.txt"],
            cwd=workspace,
            timeout=5,
        )

    assert not (saved / "marker.txt").exists()
    assert not (outside / "marker.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cwd")
@pytest.mark.asyncio
async def test_manager_run_uses_held_cwd_after_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    real_prepare = process_utils_module._prepare_posix_cwd_launch

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = workspace / "work"
    cwd.mkdir()
    saved = workspace / "work-saved"
    outside = tmp_path / "outside"
    outside.mkdir()
    swapped = False

    def prepare_after_swap(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            cwd.rename(saved)
            try:
                cwd.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"Symlink creation is unavailable: {exc}")
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(
        process_utils_module,
        "_prepare_posix_cwd_launch",
        prepare_after_swap,
    )
    manager = SandboxManager(workspace_root=workspace, backend_preference="direct")

    result = await manager.run(
        ["/bin/sh", "-c", "printf safe > marker.txt"],
        cwd=cwd,
        timeout=5,
    )

    assert swapped is True
    assert result.exit_code == 0
    assert not (outside / "marker.txt").exists()
    assert (saved / "marker.txt").read_text(encoding="utf-8") == "safe"


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cwd")
@pytest.mark.asyncio
async def test_manager_run_fails_closed_if_cwd_changes_before_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = workspace / "work"
    cwd.mkdir()
    saved = workspace / "work-saved"
    outside = tmp_path / "outside"
    outside.mkdir()
    manager = SandboxManager(workspace_root=workspace, backend_preference="direct")
    real_prepare = manager.prepare
    swapped = False

    def prepare_then_swap(*args, **kwargs):
        nonlocal swapped
        invocation = real_prepare(*args, **kwargs)
        if not swapped:
            swapped = True
            cwd.rename(saved)
            try:
                cwd.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"Symlink creation is unavailable: {exc}")
        return invocation

    monkeypatch.setattr(manager, "prepare", prepare_then_swap)

    with pytest.raises(SandboxBackendUnavailable, match="cwd became unavailable"):
        await manager.run(
            ["/bin/sh", "-c", "printf unsafe > marker.txt"],
            cwd=cwd,
            timeout=5,
        )

    assert swapped is True
    assert not (outside / "marker.txt").exists()
    assert not (saved / "marker.txt").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_manager_does_not_trust_workspace_shadowed_bwrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    workspace_bwrap = workspace / "bwrap"
    workspace_bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    workspace_bwrap.chmod(0o755)
    host_bwrap = host_bin / "bwrap"
    host_bwrap.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "bubblewrap 0.13.0"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    host_bwrap.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(workspace), str(host_bin))))

    manager = SandboxManager(workspace_root=workspace, backend_preference="native")
    invocation = manager.prepare(["true"], cwd=workspace)

    assert manager.backend_name == "bubblewrap"
    assert manager.is_fully_isolated() is True
    assert manager.has_aggregate_resource_limits() is False
    assert "aggregate CPU and memory containment" in (
        auto_approve_safety_error(manager, allow_unsafe=False) or ""
    )
    assert Path(invocation.argv[0]).resolve() == (host_bin / "bwrap").resolve()


# ---------------------------------------------------------------------------
# tier detection
# ---------------------------------------------------------------------------


def test_manager_picks_highest_available_tier(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.tier == SANDBOX_TIER_BWRAP
        assert mgr.backend_name == "bubblewrap"


def test_manager_prefers_native_backend_when_docker_is_also_available(
    tmp_path: Path,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_docker", return_value=True),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.tier == SANDBOX_TIER_BWRAP
        assert mgr.backend_name == "bubblewrap"


def test_manager_resource_containment_prefers_docker_over_native(
    tmp_path: Path,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_docker", return_value=True),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
    ):
        mgr = SandboxManager(
            workspace_root=tmp_path,
            require_resource_containment=True,
        )

    assert mgr.tier == SANDBOX_TIER_DOCKER
    assert mgr.backend_name == "docker"
    assert mgr.is_fully_isolated() is True
    assert mgr.has_aggregate_resource_limits() is True
    assert auto_approve_safety_error(mgr, allow_unsafe=False) is None


def test_manager_resource_containment_fails_closed_without_docker(
    tmp_path: Path,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
    ):
        mgr = SandboxManager(
            workspace_root=tmp_path,
            require_resource_containment=True,
        )

    assert mgr.status()["backend"] == "unavailable"
    assert "aggregate CPU and memory containment" in (
        auto_approve_safety_error(mgr, allow_unsafe=False) or ""
    )
    with pytest.raises(SandboxBackendUnavailable, match="Aggregate CPU/memory"):
        mgr.prepare(["true"], cwd=tmp_path)


def test_manager_can_upgrade_native_selection_to_resource_bounded_docker(
    tmp_path: Path,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.backend_name == "bubblewrap"

        mgr.require_aggregate_resource_containment()

    assert mgr.backend_name == "docker"
    assert mgr.require_resource_containment is True
    assert mgr.has_aggregate_resource_limits() is True


def test_manager_failed_resource_upgrade_restores_native_selection(
    tmp_path: Path,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.backend_name == "bubblewrap"

        with pytest.raises(SandboxBackendUnavailable, match="Aggregate CPU/memory"):
            mgr.require_aggregate_resource_containment()

    assert mgr.backend_name == "bubblewrap"
    assert mgr.require_resource_containment is False
    assert mgr.is_fully_isolated() is True
    assert mgr.has_aggregate_resource_limits() is False


def test_manager_uses_sandbox_exec_on_macos(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "darwin"),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
    assert mgr.tier == SANDBOX_TIER_BWRAP
    assert mgr.backend_name == "sandbox-exec"
    assert mgr.is_fully_isolated() is False
    assert auto_approve_safety_error(mgr, allow_unsafe=False)


def test_manager_refuses_macos_pty_inside_sandbox_exec(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "darwin"),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch(
            "ash.sandbox.manager.resolve_host_executable",
            return_value="/usr/bin/sandbox-exec",
        ),
    ):
        manager = SandboxManager(workspace_root=tmp_path)
        with pytest.raises(
            SandboxBackendUnavailable,
            match="sandbox-exec backend",
        ):
            manager.prepare(["echo", "hi"], cwd=tmp_path, pty=True)


def test_manager_reports_sandbox_exec_as_partial_isolation(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "darwin"),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)

    status = mgr.status()
    assert status["backend"] == "sandbox-exec"
    assert status["isolated"] is False
    assert status["filesystem"] == "host-read;workspace-write"
    assert status["network"] == "blocked"
    assert "does not restrict host file reads" in status["detail"]
    assert "macOS may still deny particular paths independently" in status["detail"]
    assert "full filesystem isolation" in status["remediation"]


def test_sandbox_exec_disappearance_fails_closed_without_fallback(
    tmp_path: Path,
) -> None:
    availability = iter((True, False))
    with (
        patch("ash.sandbox.manager.sys.platform", "darwin"),
        patch(
            "ash.sandbox.manager.has_sandbox_exec",
            side_effect=lambda *args, **kwargs: next(availability),
        ),
        patch("ash.sandbox.manager.has_docker", return_value=False),
    ):
        manager = SandboxManager(workspace_root=tmp_path)
        with pytest.raises(SandboxBackendUnavailable, match="sandbox-exec"):
            manager.prepare(["echo", "must-not-run"], cwd=tmp_path)


def test_sandbox_exec_disappearance_uses_only_explicit_scoped_fallback(
    tmp_path: Path,
) -> None:
    availability = iter((True, False))
    with (
        patch("ash.sandbox.manager.sys.platform", "darwin"),
        patch(
            "ash.sandbox.manager.has_sandbox_exec",
            side_effect=lambda *args, **kwargs: next(availability),
        ),
        patch("ash.sandbox.manager.has_docker", return_value=False),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            allow_scoped_fallback=True,
        )
        invocation = manager.prepare(["echo", "explicit-fallback"], cwd=tmp_path)

    assert invocation.argv == ("echo", "explicit-fallback")
    assert invocation.backend_name == "scoped"
    assert invocation.tier == SANDBOX_TIER_SCOPED
    assert invocation.fallback_used is True


def test_manager_falls_back_to_scoped_when_nothing_available(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.tier == SANDBOX_TIER_SCOPED
        assert mgr.backend_name == "scoped"


def test_supervised_process_refuses_scoped_auto_selection_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="auto",
            workspace_read_only=True,
        )

    assert manager.backend_name == "scoped"

    async def reject_spawn(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("unisolated process must not start")

    monkeypatch.setattr(
        "ash.sandbox.manager.asyncio.create_subprocess_exec", reject_spawn
    )
    with pytest.raises(SandboxBackendUnavailable):
        asyncio.run(
            manager.run_supervised_process(
                [sys.executable, "-c", "print('must-not-run')"],
                cwd=tmp_path,
                timeout=5,
            )
        )


def test_public_run_does_not_accept_supervisor_container_controls(
    tmp_path: Path,
) -> None:
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")

    with pytest.raises(TypeError, match="_docker_container_name"):
        manager.run(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            _docker_container_name="ash-supervised-abc123",
            _docker_supervised_token="a" * 32,
        )
    with pytest.raises(TypeError, match="_docker_container_name"):
        manager.prepare(
            ["echo", "must-not-be-supervised"],
            cwd=tmp_path,
            _docker_container_name="ash-supervised-abc123",
            _docker_supervised_token="a" * 32,
        )


@pytest.mark.parametrize("proxy_source", ["docker_config", "client_environment"])
def test_supervised_process_refuses_docker_proxy_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proxy_source: str,
) -> None:
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    (docker_config / "config.json").write_text(
        '{"proxies":{"default":{"httpProxy":"http://proxy.invalid:3128"}}}'
        if proxy_source == "docker_config"
        else '{"proxies":{}}',
        encoding="utf-8",
    )
    for name in (
        "ALL_PROXY",
        "FTP_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "ftp_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    if proxy_source == "client_environment":
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")

    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            network=False,
            workspace_read_only=True,
            docker_memory_mb=128,
            docker_cpus=0.5,
        )
    _stub_supervised_docker_environment(
        monkeypatch, manager, docker_cli_environment()
    )
    run = AsyncMock()
    monkeypatch.setattr(manager, "_run", run)

    with pytest.raises(SandboxBackendUnavailable, match="proxy injection"):
        asyncio.run(
            manager.run_supervised_process(
                [sys.executable, "-c", "raise SystemExit(70)"],
                cwd=tmp_path,
                timeout=5,
            )
        )

    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_supervised_docker_environment_pins_local_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "ALL_PROXY",
        "FTP_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "ftp_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSH_AUTH_SOCK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    docker_control = AsyncMock(
        side_effect=[b"default\n", b"unix:///var/run/docker.sock\n"]
    )

    with (
        patch(
            "ash.sandbox.manager.resolve_host_executable", return_value="/usr/bin/docker"
        ),
        patch("ash.sandbox.manager._run_docker_control", docker_control),
    ):
        environment = await manager._capture_supervised_docker_environment()

    assert environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert "DOCKER_CONTEXT" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert docker_control.await_args_list[1].args[0] == [
        "/usr/bin/docker",
        "context",
        "inspect",
        "--format",
        "{{.Endpoints.docker.Host}}",
        "default",
    ]


@pytest.mark.parametrize(
    "endpoint",
    ["ssh://docker@example.invalid", "unix://relative/docker.sock"],
)
@pytest.mark.asyncio
async def test_supervised_docker_environment_refuses_remote_or_overridden_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    docker_control = AsyncMock(side_effect=[b"work-remote\n", f"{endpoint}\n".encode()])

    with (
        patch(
            "ash.sandbox.manager.resolve_host_executable", return_value="/usr/bin/docker"
        ),
        patch("ash.sandbox.manager._run_docker_control", docker_control),
        pytest.raises(SandboxBackendUnavailable, match="local Docker socket"),
    ):
        await manager._capture_supervised_docker_environment()

    docker_control.assert_awaited()

    monkeypatch.setenv("DOCKER_HOST", "tcp://docker.example.invalid:2376")
    docker_control.reset_mock()
    with pytest.raises(SandboxBackendUnavailable, match="DOCKER_HOST"):
        await manager._capture_supervised_docker_environment()
    docker_control.assert_not_awaited()


@pytest.mark.parametrize(
    ("endpoint", "is_local"),
    [
        ("npipe:////./pipe/docker_engine", True),
        ("npipe:////remote/pipe/docker_engine", False),
    ],
)
@pytest.mark.asyncio
async def test_supervised_docker_environment_checks_windows_named_pipe_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    is_local: bool,
) -> None:
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    docker_control = AsyncMock(
        side_effect=[b"desktop-windows\n", f"{endpoint}\n".encode()]
    )

    with (
        patch("ash.sandbox.manager.os.name", "nt"),
        patch(
            "ash.sandbox.manager.resolve_host_executable", return_value="docker.exe"
        ),
        patch("ash.sandbox.manager._run_docker_control", docker_control),
    ):
        if is_local:
            environment = await manager._capture_supervised_docker_environment()
            assert environment["DOCKER_HOST"] == endpoint
        else:
            with pytest.raises(SandboxBackendUnavailable, match="local Docker socket"):
                await manager._capture_supervised_docker_environment()


def test_supervised_process_returns_the_managed_child_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            network=False,
            workspace_read_only=True,
            docker_memory_mb=128,
            docker_cpus=0.5,
        )
    _stub_supervised_docker_environment(monkeypatch, manager)
    monkeypatch.setattr(
        "ash.sandbox.manager.secrets.token_hex", lambda _length: "a" * 32
    )
    expected = SandboxResult(
        exit_code=0,
        stdout="ok",
        stderr="",
        tier=SANDBOX_TIER_DOCKER,
        backend_name="docker",
    )
    run = AsyncMock(return_value=expected)
    monkeypatch.setattr(manager, "_run", run)

    result = asyncio.run(
        manager.run_supervised_process(
            [sys.executable, "-c", "print('ok')"],
            cwd=tmp_path,
            timeout=5,
        )
    )

    assert result == expected
    run.assert_awaited_once_with(
        [sys.executable, "-c", "print('ok')"],
        cwd=tmp_path,
        timeout=5,
        stdin=asyncio.subprocess.DEVNULL,
        env=_SUPERVISED_DOCKER_ENVIRONMENT,
        _docker_container_name="ash-supervised-" + "a" * 32,
        _docker_supervised_token="a" * 32,
    )


@pytest.mark.parametrize(
    "result",
    [
        SandboxResult(
            exit_code=125,
            stdout="",
            stderr="Docker attach failed",
            tier=SANDBOX_TIER_DOCKER,
            backend_name="docker",
        ),
        SandboxResult(
            exit_code=-1,
            stdout="",
            stderr="timed out",
            tier=SANDBOX_TIER_DOCKER,
            backend_name="docker",
            timed_out=True,
        ),
        SandboxResult(
            exit_code=-1,
            stdout="",
            stderr="output limit",
            tier=SANDBOX_TIER_DOCKER,
            backend_name="docker",
            output_truncated=True,
        ),
        SandboxResult(
            exit_code=-1,
            stdout="",
            stderr="outcome unknown",
            tier=SANDBOX_TIER_DOCKER,
            backend_name="docker",
            outcome_unknown=True,
        ),
    ],
)
def test_supervised_process_removes_container_after_ambiguous_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: SandboxResult,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            network=False,
            workspace_read_only=True,
            docker_memory_mb=128,
            docker_cpus=0.5,
        )
    _stub_supervised_docker_environment(monkeypatch, manager)
    monkeypatch.setattr(
        "ash.sandbox.manager.secrets.token_hex", lambda _length: "b" * 32
    )
    run = AsyncMock(return_value=result)
    remove = AsyncMock()
    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(manager, "_remove_supervised_container", remove)

    observed = asyncio.run(
        manager.run_supervised_process(
            [sys.executable, "-c", "pass"], cwd=tmp_path, timeout=5
        )
    )

    assert observed == result
    remove.assert_awaited_once_with(
        "ash-supervised-" + "b" * 32, "b" * 32, _SUPERVISED_DOCKER_ENVIRONMENT
    )


def test_supervised_process_removes_container_after_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            network=False,
            workspace_read_only=True,
            docker_memory_mb=128,
            docker_cpus=0.5,
        )
    _stub_supervised_docker_environment(monkeypatch, manager)
    monkeypatch.setattr(
        "ash.sandbox.manager.secrets.token_hex", lambda _length: "c" * 32
    )
    run = AsyncMock(side_effect=asyncio.CancelledError)
    remove = AsyncMock()
    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(manager, "_remove_supervised_container", remove)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            manager.run_supervised_process(
                [sys.executable, "-c", "pass"], cwd=tmp_path, timeout=5
            )
        )

    remove.assert_awaited_once_with(
        "ash-supervised-" + "c" * 32, "c" * 32, _SUPERVISED_DOCKER_ENVIRONMENT
    )


def test_supervised_process_requires_confirmed_container_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            network=False,
            workspace_read_only=True,
            docker_memory_mb=128,
            docker_cpus=0.5,
        )
    _stub_supervised_docker_environment(monkeypatch, manager)
    result = SandboxResult(
        exit_code=-1,
        stdout="",
        stderr="timed out",
        tier=SANDBOX_TIER_DOCKER,
        backend_name="docker",
        timed_out=True,
    )
    monkeypatch.setattr("ash.sandbox.manager.secrets.token_hex", lambda _size: "e" * 32)
    monkeypatch.setattr(manager, "_run", AsyncMock(return_value=result))
    monkeypatch.setattr(
        manager,
        "_remove_supervised_container",
        AsyncMock(side_effect=SandboxBackendUnavailable("Docker daemon unavailable")),
    )

    with pytest.raises(
        SandboxBackendUnavailable, match="could not be confirmed stopped"
    ):
        asyncio.run(
            manager.run_supervised_process(
                [sys.executable, "-c", "pass"], cwd=tmp_path, timeout=5
            )
        )


def test_supervised_process_waits_for_cleanup_before_propagating_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            network=False,
            workspace_read_only=True,
            docker_memory_mb=128,
            docker_cpus=0.5,
        )
    _stub_supervised_docker_environment(monkeypatch, manager)
    monkeypatch.setattr("ash.sandbox.manager.secrets.token_hex", lambda _size: "f" * 32)
    result = SandboxResult(
        exit_code=0,
        stdout="",
        stderr="",
        tier=SANDBOX_TIER_DOCKER,
        backend_name="docker",
        timed_out=True,
    )
    cleanup_started = asyncio.Event()
    allow_cleanup_to_finish = asyncio.Event()

    async def delayed_cleanup(
        _name: str, _token: str, _environment: dict[str, str]
    ) -> None:
        cleanup_started.set()
        await allow_cleanup_to_finish.wait()

    monkeypatch.setattr(manager, "_run", AsyncMock(return_value=result))
    remove = AsyncMock(side_effect=delayed_cleanup)
    monkeypatch.setattr(manager, "_remove_supervised_container", remove)

    async def exercise() -> None:
        task = asyncio.create_task(
            manager.run_supervised_process(
                [sys.executable, "-c", "pass"], cwd=tmp_path, timeout=5
            )
        )
        await cleanup_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        allow_cleanup_to_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    remove.assert_awaited_once_with(
        "ash-supervised-" + "f" * 32, "f" * 32, _SUPERVISED_DOCKER_ENVIRONMENT
    )


def test_supervised_process_propagates_cancellation_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            network=False,
            workspace_read_only=True,
            docker_memory_mb=128,
            docker_cpus=0.5,
        )
    _stub_supervised_docker_environment(monkeypatch, manager)
    monkeypatch.setattr("ash.sandbox.manager.secrets.token_hex", lambda _size: "g" * 32)
    result = SandboxResult(
        exit_code=0,
        stdout="",
        stderr="",
        tier=SANDBOX_TIER_DOCKER,
        backend_name="docker",
        timed_out=True,
    )
    cleanup_started = asyncio.Event()
    allow_cleanup_to_fail = asyncio.Event()
    cleanup_error = SandboxBackendUnavailable("Docker daemon unavailable")

    async def failing_cleanup(
        _name: str, _token: str, _environment: dict[str, str]
    ) -> None:
        cleanup_started.set()
        await allow_cleanup_to_fail.wait()
        raise cleanup_error

    monkeypatch.setattr(manager, "_run", AsyncMock(return_value=result))
    remove = AsyncMock(side_effect=failing_cleanup)
    monkeypatch.setattr(manager, "_remove_supervised_container", remove)

    async def exercise() -> asyncio.CancelledError:
        task = asyncio.create_task(
            manager.run_supervised_process(
                [sys.executable, "-c", "pass"], cwd=tmp_path, timeout=5
            )
        )
        await cleanup_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        allow_cleanup_to_fail.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        return raised.value

    cancellation = asyncio.run(exercise())
    assert cancellation.__cause__ is cleanup_error
    assert any("cleanup failed" in note for note in cancellation.__notes__)
    remove.assert_awaited_once_with(
        "ash-supervised-" + "g" * 32, "g" * 32, _SUPERVISED_DOCKER_ENVIRONMENT
    )


def test_supervised_process_reports_cleanup_task_cancellation_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            network=False,
            workspace_read_only=True,
            docker_memory_mb=128,
            docker_cpus=0.5,
        )
    _stub_supervised_docker_environment(monkeypatch, manager)
    monkeypatch.setattr("ash.sandbox.manager.secrets.token_hex", lambda _size: "h" * 32)
    result = SandboxResult(
        exit_code=0,
        stdout="",
        stderr="",
        tier=SANDBOX_TIER_DOCKER,
        backend_name="docker",
        timed_out=True,
    )
    monkeypatch.setattr(manager, "_run", AsyncMock(return_value=result))
    remove = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(manager, "_remove_supervised_container", remove)

    with pytest.raises(
        SandboxBackendUnavailable, match="could not be confirmed stopped"
    ) as raised:
        asyncio.run(
            manager.run_supervised_process(
                [sys.executable, "-c", "pass"], cwd=tmp_path, timeout=5
            )
        )

    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    remove.assert_awaited_once_with(
        "ash-supervised-" + "h" * 32, "h" * 32, _SUPERVISED_DOCKER_ENVIRONMENT
    )


@pytest.mark.asyncio
async def test_supervised_cleanup_refuses_unowned_container_name(
    tmp_path: Path,
) -> None:
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")
    container_id = "a" * 64
    inspect = AsyncMock(return_value=f"{container_id} another-run-token\n".encode())

    with (
        patch(
            "ash.sandbox.manager.resolve_host_executable", return_value="/usr/bin/docker"
        ),
        patch("ash.sandbox.manager._run_docker_control", inspect),
        pytest.raises(SandboxBackendUnavailable, match="matching supervisor token"),
    ):
        await manager._remove_supervised_container(
            "ash-supervised-collision", "b" * 32, _SUPERVISED_DOCKER_ENVIRONMENT
        )

    inspect.assert_awaited_once_with(
        [
            "/usr/bin/docker",
            "inspect",
            "--format",
            '{{.Id}} {{index .Config.Labels "ash.supervised.token"}}',
            "ash-supervised-collision",
        ],
        timeout_seconds=15,
        env=_SUPERVISED_DOCKER_ENVIRONMENT,
    )


@pytest.mark.asyncio
async def test_supervised_cleanup_removes_only_verified_container_id(
    tmp_path: Path,
) -> None:
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")
    container_id = "c" * 64
    token = "d" * 32
    control = AsyncMock(side_effect=[f"{container_id} {token}\n".encode(), b""])

    with (
        patch(
            "ash.sandbox.manager.resolve_host_executable", return_value="/usr/bin/docker"
        ),
        patch("ash.sandbox.manager._run_docker_control", control),
    ):
        await manager._remove_supervised_container(
            "ash-supervised-run", token, _SUPERVISED_DOCKER_ENVIRONMENT
        )

    assert control.await_args_list[1].args[0] == [
        "/usr/bin/docker",
        "rm",
        "--force",
        container_id,
    ]
    assert control.await_args_list[1].kwargs["env"] == _SUPERVISED_DOCKER_ENVIRONMENT


def test_supervised_process_container_name_reaches_docker_invocation(
    tmp_path: Path,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            network=False,
            workspace_read_only=True,
            docker_memory_mb=128,
            docker_cpus=0.5,
        )

    with patch("ash.sandbox.manager.has_docker", return_value=True):
        with manager._prepare_launch(
            ["echo", "bounded"],
            cwd=tmp_path,
            passthrough_env_names=(),
            pty=False,
            docker_container_name="ash-supervised-abc123",
            docker_supervised_token="a" * 32,
        ) as invocation_context:
            invocation = invocation_context
            assert invocation.backend_name == "docker"

    assert "--name" in invocation.argv
    assert invocation.argv[invocation.argv.index("--name") + 1] == (
        "ash-supervised-abc123"
    )
    assert "--label" in invocation.argv
    assert invocation.argv[invocation.argv.index("--label") + 1] == (
        "ash.supervised.token=" + "a" * 32
    )


@pytest.mark.asyncio
async def test_sandbox_manager_can_close_child_stdin(tmp_path: Path) -> None:
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")

    result = await manager.run(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
        cwd=tmp_path,
        timeout=5,
        stdin=asyncio.subprocess.DEVNULL,
    )

    assert result.exit_code == 0
    assert result.stdout == "''\n"


def test_supervised_process_requires_docker_and_all_boundary_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=False),
    ):
        bubblewrap = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="native",
            network=False,
            workspace_read_only=True,
        )
    assert bubblewrap.backend_name == "bubblewrap"

    async def reject_spawn(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("Bubblewrap must not start a supervised process")

    monkeypatch.setattr(
        "ash.sandbox.manager.asyncio.create_subprocess_exec", reject_spawn
    )
    with pytest.raises(SandboxBackendUnavailable):
        asyncio.run(
            bubblewrap.run_supervised_process(
                [sys.executable, "-c", "print('must-not-run')"],
                cwd=tmp_path,
                timeout=5,
            )
        )

    with (
        patch("ash.sandbox.manager.sys.platform", "darwin"),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=False),
    ):
        sandbox_exec = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="native",
            network=False,
            workspace_read_only=True,
        )
    assert sandbox_exec.backend_name == "sandbox-exec"
    with pytest.raises(SandboxBackendUnavailable):
        asyncio.run(
            sandbox_exec.run_supervised_process(
                [sys.executable, "-c", "print('must-not-run')"],
                cwd=tmp_path,
                timeout=5,
            )
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"network": True},
        {"workspace_read_only": False},
        {"docker_memory_mb": 0},
        {"docker_cpus": 0},
        {"allow_scoped_fallback": True},
    ],
)
def test_supervised_process_rejects_unsafe_docker_configuration(
    tmp_path: Path,
    overrides: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: dict[str, object] = {
        "network": False,
        "workspace_read_only": True,
        "docker_memory_mb": 128,
        "docker_cpus": 0.5,
        "allow_scoped_fallback": False,
    }
    options.update(overrides)
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            **options,
        )
    assert manager.backend_name == "docker"

    async def reject_spawn(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("unsafe Docker configuration must not start a process")

    monkeypatch.setattr(
        "ash.sandbox.manager.asyncio.create_subprocess_exec", reject_spawn
    )
    with pytest.raises(SandboxBackendUnavailable):
        asyncio.run(
            manager.run_supervised_process(
                [sys.executable, "-c", "print('must-not-run')"],
                cwd=tmp_path,
                timeout=5,
            )
        )


def test_manager_respects_preferred_tier(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_docker", return_value=True),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
    ):
        mgr = SandboxManager(workspace_root=tmp_path, preferred_tier=2)
        assert mgr.tier == SANDBOX_TIER_BWRAP
        assert mgr.backend_name == "bubblewrap"


def test_manager_rejects_invalid_preferred_tier(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="1, 2, or 3"):
        SandboxManager(workspace_root=tmp_path, preferred_tier=0)


def test_manager_explicit_direct_does_not_probe_backends(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.has_bwrap") as bwrap,
        patch("ash.sandbox.manager.has_sandbox_exec") as sandbox_exec,
        patch("ash.sandbox.manager.has_docker") as docker,
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="direct",
        )

    assert manager.tier == SANDBOX_TIER_SCOPED
    bwrap.assert_not_called()
    sandbox_exec.assert_not_called()
    docker.assert_not_called()


def test_manager_explicit_unavailable_backend_fails_closed(
    tmp_path: Path,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=False) as bwrap,
        patch("ash.sandbox.manager.has_docker", return_value=False) as docker,
    ):
        native = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="native",
        )
        requested_docker = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
        )
        docker_fallback = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            allow_scoped_fallback=True,
        )

    assert native.tier == SANDBOX_TIER_SCOPED
    assert requested_docker.tier == SANDBOX_TIER_SCOPED
    assert native.status()["backend"] == "unavailable"
    assert requested_docker.status()["backend"] == "unavailable"
    assert native.status()["fail_closed"] is True
    assert requested_docker.status()["fail_closed"] is True
    with pytest.raises(SandboxBackendUnavailable, match="native sandbox backend"):
        native.prepare(["true"])
    with pytest.raises(SandboxBackendUnavailable, match="docker sandbox backend"):
        requested_docker.prepare(["true"])
    fallback = docker_fallback.prepare(["true"])
    assert fallback.backend_name == "scoped"
    assert fallback.fallback_used is True
    assert docker_fallback.status()["backend"] == "scoped"
    with pytest.raises(
        SandboxBackendUnavailable,
        match="PTY execution is unavailable with the Docker sandbox",
    ):
        docker_fallback.prepare(["echo", "hi"], pty=True)
    bwrap.assert_called_once_with(tmp_path)
    assert docker.call_count == 2
    docker.assert_called_with(DEFAULT_IMAGE, workspace_root=tmp_path)


def test_manager_explicit_docker_uses_configured_image(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap") as bwrap,
        patch("ash.sandbox.manager.has_docker", return_value=True) as docker,
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            docker_image="company/ash-sandbox:v2",
            docker_memory_mb=2048,
            docker_cpus=1.5,
        )
        backend = manager._build_backend(manager.tier)
        bwrap.assert_not_called()
        status = manager.status()

    assert manager.tier == SANDBOX_TIER_DOCKER
    assert manager.is_fully_isolated() is True
    assert isinstance(backend, DockerSandbox)
    assert backend.memory_limit == "2048m"
    assert backend.cpus == 1.5
    assert "memory=2048 MiB" in status["detail"]
    assert "cpus=1.5" in status["detail"]
    docker.assert_called_with(
        "company/ash-sandbox:v2", workspace_root=tmp_path
    )
    with (
        patch.object(manager, "_build_backend", return_value=backend),
        pytest.raises(
            SandboxBackendUnavailable,
            match="PTY execution is unavailable with the Docker sandbox",
        ),
    ):
        manager.prepare(["echo", "hi"], pty=True)
    manager.allow_scoped_fallback = True
    with (
        patch.object(manager, "_build_backend", return_value=backend),
        pytest.raises(
            SandboxBackendUnavailable,
            match="PTY execution is unavailable with the Docker sandbox",
        ),
    ):
        manager.prepare(["echo", "hi"], pty=True)


def test_manager_can_disable_docker_cpu_and_memory_limits(tmp_path: Path) -> None:
    with patch("ash.sandbox.manager.has_docker", return_value=True):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            docker_memory_mb=0,
            docker_cpus=0,
        )
        backend = manager._build_backend(manager.tier)
        status = manager.status()

    assert isinstance(backend, DockerSandbox)
    assert backend.memory_limit is None
    assert backend.cpus is None
    assert "memory=unlimited" in status["detail"]
    assert "cpus=unlimited" in status["detail"]
    assert manager.has_aggregate_resource_limits() is False
    assert "aggregate CPU and memory containment" in (
        auto_approve_safety_error(manager, allow_unsafe=False) or ""
    )


def test_resource_required_docker_reports_disabled_limit_remediation(
    tmp_path: Path,
) -> None:
    with patch("ash.sandbox.manager.has_docker", return_value=True):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            docker_memory_mb=0,
            docker_cpus=0,
            require_resource_containment=True,
        )

    status = manager.status()
    assert status["backend"] == "docker"
    assert status["aggregate_resource_limits"] is False
    assert "sandbox_docker_memory_mb" in status["remediation"]
    assert "sandbox_docker_cpus" in status["remediation"]


def test_manager_rejects_docker_memory_below_engine_minimum(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="0 or at least 6 MiB"):
        SandboxManager(
            workspace_root=tmp_path,
            backend_preference="direct",
            docker_memory_mb=5,
        )

    with pytest.raises(ValueError, match="docker_memory_mb"):
        SandboxManager(
            workspace_root=tmp_path,
            backend_preference="direct",
            docker_memory_mb=1_048_577,
        )
    with pytest.raises(ValueError, match="docker_cpus"):
        SandboxManager(
            workspace_root=tmp_path,
            backend_preference="direct",
            docker_cpus=1025,
        )


def test_manager_capabilities_reports_each_backend(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        caps = mgr.capabilities()
        assert caps == {
            "scoped": True,
            "bwrap": True,
            "sandbox_exec": False,
            "docker": False,
        }


def test_manager_is_fully_isolated_only_at_tier_2_plus(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.is_fully_isolated() is True

    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.is_fully_isolated() is False


def test_manager_status_describes_enforcement(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path, network=False)
        status = mgr.status()

    assert status["backend"] == "bubblewrap"
    assert status["isolated"] is True
    assert status["filesystem"] == "workspace-write"
    assert status["network"] == "blocked"
    assert status["fail_closed"] is True
    assert status["remediation"] == ""


def test_manager_reports_read_only_workspace(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "linux"),
        patch("ash.sandbox.manager.has_bwrap", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=False),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            workspace_read_only=True,
        )

    assert manager.status()["filesystem"] == "workspace-read"


def test_read_isolation_requirement_skips_macos_sandbox_exec(
    tmp_path: Path,
) -> None:
    with (
        patch("ash.sandbox.manager.sys.platform", "darwin"),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=True),
        patch("ash.sandbox.manager.has_docker", return_value=False),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            require_read_isolation=True,
        )

    assert manager.tier == SANDBOX_TIER_SCOPED


# ---------------------------------------------------------------------------
# Bubblewrap argv construction
# ---------------------------------------------------------------------------


def test_bubblewrap_unavailable_on_non_linux(tmp_path: Path) -> None:
    if sys.platform.startswith("linux"):
        pytest.skip("test is non-Linux-specific")
    backend = BubblewrapSandbox(workspace_root=tmp_path, bwrap_path="/bin/true")
    assert backend.is_available() is False


def test_bubblewrap_wrap_includes_namespace_flags(tmp_path: Path) -> None:
    if not has_bwrap():
        pytest.skip("bwrap not installed on this host")
    backend = BubblewrapSandbox(workspace_root=tmp_path, network=False)
    argv = backend.wrap(["echo", "hi"])
    # First element is the bwrap binary path.
    assert Path(argv[0]).name == "bwrap"
    # Required namespace isolation flags are present.
    assert "--unshare-user" in argv
    assert "--unshare-user-try" not in argv
    assert "--unshare-pid" in argv
    assert "--unshare-uts" in argv
    assert "--unshare-ipc" in argv
    assert "--disable-userns" in argv
    assert "--assert-userns-disabled" in argv
    assert "--die-with-parent" in argv
    assert "--new-session" in argv
    assert "--proc" in argv
    assert argv[argv.index("--proc") + 1] == "/proc"
    assert "--dev" in argv
    assert argv[argv.index("--dev") + 1] == "/dev"
    # Network is off by default.
    assert "--unshare-net" in argv
    # Workspace is bound.
    assert "--bind" in argv
    assert str(tmp_path) in argv
    # Command separator and the actual command are at the tail.
    assert "--" in argv
    assert argv[-2:] == ["echo", "hi"]

    pty_argv = backend.wrap(["echo", "hi"], pty=True)
    assert "--new-session" not in pty_argv


def test_bubblewrap_does_not_mount_entire_host_etc(tmp_path: Path) -> None:
    if not has_bwrap():
        pytest.skip("bwrap not installed on this host")
    backend = BubblewrapSandbox(workspace_root=tmp_path, network=False)

    argv = backend.wrap(["echo", "hi"])

    mount_pairs = {
        (argv[index + 1], argv[index + 2])
        for index, value in enumerate(argv[:-2])
        if value in {"--ro-bind", "--bind"}
    }
    assert ("/etc", "/etc") not in mount_pairs
    assert "/etc/machine-id" not in argv


def test_bubblewrap_with_network_omits_unshare_net(tmp_path: Path) -> None:
    if not has_bwrap():
        pytest.skip("bwrap not installed on this host")
    backend = BubblewrapSandbox(workspace_root=tmp_path, network=True)
    argv = backend.wrap(["echo", "hi"])
    assert "--unshare-net" not in argv


def test_bubblewrap_wrap_validates_command() -> None:
    if not has_bwrap():
        pytest.skip("bwrap not installed on this host")
    backend = BubblewrapSandbox(workspace_root=Path("/tmp"))
    with pytest.raises(ValueError):
        backend.wrap([])


def test_bubblewrap_raises_when_binary_missing(tmp_path: Path) -> None:
    backend = BubblewrapSandbox(
        workspace_root=tmp_path, bwrap_path=str(tmp_path / "nonexistent-bwrap")
    )
    with pytest.raises(SandboxBackendUnavailable):
        backend.wrap(["echo", "hi"])


def test_bubblewrap_can_mount_workspace_read_only(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("bubblewrap backend is Linux-only")
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = BubblewrapSandbox(
        workspace_root=tmp_path,
        workspace_read_only=True,
        bwrap_path=str(fake),
    )

    argv = backend.wrap(["echo", "hi"], cwd=tmp_path)

    root_index = argv.index(str(tmp_path))
    assert argv[root_index - 1] == "--ro-bind"


def test_bubblewrap_can_mount_workspace_from_held_descriptor(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("bubblewrap backend is Linux-only")
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = BubblewrapSandbox(
        workspace_root=tmp_path,
        workspace_read_only=True,
        bwrap_path=str(fake),
    )

    workspace_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        argv = backend.wrap(
            ["echo", "hi"],
            cwd=tmp_path,
            workspace_fd=workspace_fd,
        )
    finally:
        os.close(workspace_fd)

    fd_index = argv.index(str(workspace_fd))
    assert argv[fd_index - 1] == "--ro-bind-fd"
    assert argv[fd_index + 1] == str(tmp_path)


def test_bubblewrap_can_mount_extra_read_only_path_from_held_descriptor(
    tmp_path: Path,
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("bubblewrap backend is Linux-only")
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    backend = BubblewrapSandbox(
        workspace_root=workspace,
        read_only_paths=(extra,),
        bwrap_path=str(fake),
    )

    extra_fd = os.open(extra, os.O_RDONLY | os.O_DIRECTORY)
    try:
        argv = backend.wrap(
            ["echo", "hi"],
            cwd=workspace,
            read_only_fds=((extra_fd, extra),),
        )
    finally:
        os.close(extra_fd)

    fd_index = argv.index(str(extra_fd))
    assert argv[fd_index - 1] == "--ro-bind-fd"
    assert argv[fd_index + 1] == str(extra)
    assert "--ro-bind" not in argv[fd_index - 1 : fd_index + 2]


# ---------------------------------------------------------------------------
# Docker argv construction
# ---------------------------------------------------------------------------


def test_docker_unavailable_when_binary_missing(tmp_path: Path) -> None:
    backend = DockerSandbox(
        workspace_root=tmp_path, docker_path=str(tmp_path / "nonexistent-docker")
    )
    assert backend.is_available() is False
    with pytest.raises(SandboxBackendUnavailable):
        backend.wrap(["echo", "hi"])


def test_docker_wrap_includes_security_flags(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(
        workspace_root=tmp_path, docker_path=str(fake), network=False
    )
    argv = backend.wrap(["echo", "hi"])
    assert argv[0] == str(fake)
    assert "run" in argv
    assert "--rm" in argv
    assert "--network=none" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--read-only" in argv
    assert "--init" in argv
    assert "--pids-limit=256" in argv
    assert "/tmp:rw,nosuid,nodev,size=512m" in argv
    assert "HOME=/tmp" in argv
    # Bind mount and container-native working directory for the workspace.
    assert "--mount" in argv
    assert f"source={tmp_path},target=/workspace" in " ".join(argv)
    assert argv[argv.index("--workdir") + 1] == "/workspace"
    # Image + command tail.
    assert argv[-2:] == ["echo", "hi"]


def test_docker_wrap_emits_cpu_and_memory_limits(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(
        workspace_root=tmp_path,
        docker_path=str(fake),
        memory_limit="2048m",
        cpus=1.5,
    )

    argv = backend.wrap(["echo", "bounded"])

    assert argv[argv.index("--memory") + 1] == "2048m"
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert "--pids-limit=256" in argv


def test_docker_wrap_can_name_supervised_container_for_cleanup(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(workspace_root=tmp_path, docker_path=str(fake))

    argv = backend.wrap(["echo", "bounded"], container_name="ash-supervised-abc123")

    assert argv[argv.index("--name") + 1] == "ash-supervised-abc123"
    with pytest.raises(SandboxBackendUnavailable, match="container name"):
        backend.wrap(["true"], container_name="../escape")


def test_docker_wrap_clears_proxy_environment_for_supervised_container(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(
        workspace_root=tmp_path,
        docker_path=str(fake),
        network=False,
        workspace_read_only=True,
        memory_limit="128m",
        cpus=0.5,
    )

    argv = backend.wrap(
        ["echo", "bounded"],
        container_name="ash-supervised-abc123",
        supervised_token="a" * 32,
        clear_proxy_environment=True,
    )

    env_values = {
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "--env"
    }
    assert {
        "ALL_PROXY=",
        "FTP_PROXY=",
        "HTTP_PROXY=",
        "HTTPS_PROXY=",
        "NO_PROXY=",
        "all_proxy=",
        "ftp_proxy=",
        "http_proxy=",
        "https_proxy=",
        "no_proxy=",
    } <= env_values
    mount = argv[argv.index("--mount") + 1]
    assert mount.endswith(",readonly,bind-recursive=disabled")


def test_docker_can_mount_workspace_read_only(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(
        workspace_root=tmp_path,
        workspace_read_only=True,
        docker_path=str(fake),
    )

    argv = backend.wrap(["echo", "hi"])

    mount = argv[argv.index("--mount") + 1]
    assert mount.endswith("target=/workspace,readonly")


def test_docker_can_mount_daemon_workspace_volume_read_only(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(
        workspace_root=tmp_path,
        workspace_read_only=True,
        docker_path=str(fake),
    )

    argv = backend.wrap(
        ["echo", "hi"],
        workspace_volume="ash-plugin-0123456789abcdef",
    )

    mount = argv[argv.index("--mount") + 1]
    assert mount == (
        "type=volume,source=ash-plugin-0123456789abcdef,"
        "target=/workspace,volume-nocopy,readonly"
    )
    assert f"source={tmp_path}" not in " ".join(argv)
    assert argv[argv.index("--workdir") + 1] == "/workspace"


def test_docker_staged_volume_rejects_host_cwd_and_invalid_name(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(workspace_root=tmp_path, docker_path=str(fake))

    with pytest.raises(SandboxBackendUnavailable, match="volume name"):
        backend.wrap(["true"], workspace_volume="../escape")
    with pytest.raises(SandboxBackendUnavailable, match="host cwd"):
        backend.wrap(
            ["true"],
            cwd=tmp_path,
            workspace_volume="ash-plugin-0123456789abcdef",
        )


@pytest.mark.asyncio
async def test_manager_stages_docker_workspace_without_host_bind(
    tmp_path: Path,
) -> None:
    archive = io.BytesIO(b"tar-bytes")
    docker_control = AsyncMock(return_value=b"")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    with (
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
        patch(
            "ash.sandbox.docker.resolve_host_executable",
            return_value=str(fake_docker),
        ),
        patch("ash.sandbox.manager._run_docker_control", docker_control),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
            workspace_read_only=True,
            docker_memory_mb=1024,
            docker_cpus=0.75,
        )
        volume = await manager.stage_docker_workspace(archive)

    assert volume.startswith("ash-plugin-")
    assert docker_control.await_count == 2
    create_argv = docker_control.await_args_list[0].args[0]
    assert create_argv == [str(fake_docker), "volume", "create", volume]
    stage_argv = docker_control.await_args_list[1].args[0]
    assert "run" in stage_argv
    assert stage_argv[stage_argv.index("--memory") + 1] == "1024m"
    assert stage_argv[stage_argv.index("--cpus") + 1] == "0.75"
    assert "--pids-limit=256" in stage_argv
    assert f"source={tmp_path}" not in " ".join(stage_argv)
    mount = stage_argv[stage_argv.index("--mount") + 1]
    assert mount == (
        f"type=volume,source={volume},target=/workspace,volume-nocopy"
    )
    assert stage_argv[-5:] == ["/bin/tar", "-xf", "-", "-C", "/"]
    assert docker_control.await_args_list[1].kwargs["stdin"] is archive
    assert archive.tell() == 0


@pytest.mark.asyncio
async def test_manager_removes_docker_volume_when_staging_fails(
    tmp_path: Path,
) -> None:
    fake_docker = tmp_path / "docker"
    fake_docker.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    docker_control = AsyncMock(
        side_effect=[
            b"",
            SandboxBackendUnavailable("stage failed"),
            b"",
        ]
    )
    with (
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
        patch(
            "ash.sandbox.docker.resolve_host_executable",
            return_value=str(fake_docker),
        ),
        patch(
            "ash.sandbox.manager.resolve_host_executable",
            return_value=str(fake_docker),
        ),
        patch("ash.sandbox.manager._run_docker_control", docker_control),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
        )
        with pytest.raises(SandboxBackendUnavailable, match="stage failed"):
            await manager.stage_docker_workspace(io.BytesIO(b"tar"))

    assert docker_control.await_count == 3
    cleanup_argv = docker_control.await_args_list[2].args[0]
    assert cleanup_argv[:4] == [
        str(fake_docker),
        "volume",
        "rm",
        "--force",
    ]
    assert cleanup_argv[4].startswith("ash-plugin-")


@pytest.mark.asyncio
async def test_manager_removes_docker_volume_after_ambiguous_create_failure(
    tmp_path: Path,
) -> None:
    fake_docker = tmp_path / "docker"
    fake_docker.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    calls: list[list[str]] = []

    async def docker_control(
        argv: list[str],
        *,
        on_spawn=None,
        **_kwargs: object,
    ) -> bytes:
        if argv[1:3] == ["volume", "create"]:
            assert on_spawn is not None
            on_spawn()
            raise SandboxBackendUnavailable("volume create timed out")
        calls.append(argv)
        return b""

    with (
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_docker", return_value=True),
        patch(
            "ash.sandbox.docker.resolve_host_executable",
            return_value=str(fake_docker),
        ),
        patch(
            "ash.sandbox.manager.resolve_host_executable",
            return_value=str(fake_docker),
        ),
        patch("ash.sandbox.manager._run_docker_control", docker_control),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="docker",
        )
        with pytest.raises(SandboxBackendUnavailable, match="create timed out"):
            await manager.stage_docker_workspace(io.BytesIO(b"tar"))

    assert len(calls) == 1
    assert calls[0][:4] == [str(fake_docker), "volume", "rm", "--force"]
    assert calls[0][4].startswith("ash-plugin-")


@pytest.mark.asyncio
async def test_manager_refuses_to_remove_foreign_docker_volume(tmp_path: Path) -> None:
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")

    for name in ("user-data", "ash-plugin-user-data", "ash-plugin-deadbeef"):
        with pytest.raises(SandboxBackendUnavailable, match="foreign"):
            await manager.remove_docker_workspace(name)


def test_docker_forwards_environment_by_name_without_exposing_value(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(workspace_root=tmp_path, docker_path=str(fake))

    argv = backend.wrap(
        ["echo", "hi"], passthrough_env_names=["PACKAGE_REGISTRY_TOKEN"]
    )

    token_index = argv.index("PACKAGE_REGISTRY_TOKEN")
    assert argv[token_index - 1] == "--env"
    assert all("secret-value" not in argument for argument in argv)


def test_docker_with_network_omits_none_flag(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(
        workspace_root=tmp_path, docker_path=str(fake), network=True
    )
    argv = backend.wrap(["echo", "hi"])
    assert "--network=none" not in argv


def test_docker_maps_nested_host_cwd_to_container(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    nested = tmp_path / "packages" / "api"
    nested.mkdir(parents=True)
    backend = DockerSandbox(workspace_root=tmp_path, docker_path=str(fake))

    argv = backend.wrap(["pwd"], cwd=nested)

    assert argv[argv.index("--workdir") + 1] == "/workspace/packages/api"


def test_docker_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = DockerSandbox(workspace_root=workspace, docker_path=str(fake))

    with pytest.raises(SandboxBackendUnavailable, match="outside"):
        backend.wrap(["pwd"], cwd=tmp_path)


def test_probe_docker_requires_daemon_and_image() -> None:
    ready = subprocess.CompletedProcess([], 0, stdout=b"27.0\n", stderr=b"")
    image = subprocess.CompletedProcess([], 0, stdout=b"[]", stderr=b"")
    with (
        patch(
            "ash.sandbox.docker.resolve_host_executable",
            return_value="/usr/bin/docker",
        ),
        patch(
            "ash.sandbox.docker.run_docker_cli_sync",
            side_effect=[ready, image],
        ) as run,
    ):
        assert probe_docker() == "/usr/bin/docker"
    assert run.call_args_list[1].args[0][-1] == DEFAULT_IMAGE


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_sync_docker_cli_timeout_terminates_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived"
    child = (
        "import time; "
        "time.sleep(0.35); "
        f"open({str(marker)!r}, 'w').write('survived'); "
        "time.sleep(5)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(30)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_docker_cli_sync(
            [sys.executable, "-c", parent],
            workspace_root=tmp_path,
            timeout=0.1,
        )
    time.sleep(0.5)

    assert not marker.exists()


def test_docker_cli_environment_preserves_context_without_provider_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-builder")
    monkeypatch.setenv("DOCKER_HOST", "ssh://docker.example")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-provider-secret")

    environment = docker_cli_environment()

    assert environment["DOCKER_CONTEXT"] == "remote-builder"
    assert environment["DOCKER_HOST"] == "ssh://docker.example"
    assert environment["SSH_AUTH_SOCK"] == "/tmp/ssh-agent.sock"
    assert environment["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert "OPENAI_API_KEY" not in environment


@pytest.mark.asyncio
async def test_docker_control_uses_scrubbed_docker_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import manager as manager_module

    monkeypatch.setenv("DOCKER_CONTEXT", "remote-builder")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-provider-secret")
    process = SimpleNamespace(returncode=0)
    create = AsyncMock(return_value=process)
    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        manager_module,
        "prepare_process_tree",
        lambda: SimpleNamespace(spawn_options={}),
    )
    monkeypatch.setattr(
        manager_module,
        "communicate_process",
        AsyncMock(return_value=(b"ok", b"")),
    )
    process_started = False

    def mark_process_started() -> None:
        nonlocal process_started
        process_started = True

    result = await manager_module._run_docker_control(
        ["/usr/bin/docker", "version"],
        on_spawn=mark_process_started,
    )

    assert result == b"ok"
    assert process_started is True
    call = create.await_args
    assert call is not None
    environment = call.kwargs["env"]
    assert environment["DOCKER_CONTEXT"] == "remote-builder"
    assert "OPENAI_API_KEY" not in environment


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", ["scoped", "wrapped", "docker_control"])
async def test_sandbox_runners_clean_process_tree_after_unexpected_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: str,
) -> None:
    from contextlib import nullcontext

    from ash.sandbox import manager as manager_module

    process = SimpleNamespace(returncode=None)
    cleanup = AsyncMock(return_value=(None, False))
    monkeypatch.setattr(
        manager_module,
        "prepare_process_tree",
        lambda *args, **kwargs: SimpleNamespace(spawn_options={}),
    )
    monkeypatch.setattr(
        manager_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        manager_module,
        "communicate_process",
        AsyncMock(side_effect=RuntimeError("sandbox stream failed")),
    )
    monkeypatch.setattr(
        manager_module,
        "settle_process_tree_after_cancellation",
        cleanup,
    )
    monkeypatch.setattr(
        manager_module,
        "prepare_scoped_process_launch",
        lambda *args, **kwargs: nullcontext(
            SimpleNamespace(argv=("sandbox-command",), pass_fds=(), cwd=str(tmp_path))
        ),
    )

    with pytest.raises(RuntimeError, match="sandbox stream failed"):
        if runner == "scoped":
            await manager_module._run_scoped(
                manager_module._ScopedBackend(),
                ["sandbox-command"],
                tmp_path,
                5,
                workspace_root=tmp_path,
                fallback=False,
            )
        elif runner == "wrapped":
            await manager_module._run_subprocess(
                ["sandbox-command"],
                cwd=tmp_path,
                deadline=5,
                tier=SANDBOX_TIER_BWRAP,
                backend_name="test",
                workspace_root=tmp_path,
            )
        else:
            await manager_module._run_docker_control(["docker", "version"])

    cleanup.assert_awaited_once()
    assert cleanup.await_args is not None
    assert cleanup.await_args.args[0] is process


@pytest.mark.asyncio
async def test_wrapped_sandbox_rejects_replaced_workspace_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import manager as manager_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = workspace.stat()
    expected_identity = (metadata.st_dev, metadata.st_ino)
    workspace.rename(tmp_path / "workspace-original")
    workspace.mkdir()
    create = AsyncMock()
    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", create)

    with pytest.raises(
        SandboxBackendUnavailable,
        match="working directory identity changed",
    ):
        await manager_module._run_subprocess(
            ["wrapped-sandbox-command"],
            cwd=None,
            deadline=5,
            tier=SANDBOX_TIER_DOCKER,
            backend_name="docker",
            workspace_root=workspace,
            expected_workspace_identity=expected_identity,
        )

    create.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_probe_docker_skips_workspace_shadowed_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    script = """#!/bin/sh
if [ "$1" = "version" ]; then
  printf '27.0\n'
fi
exit 0
"""
    for directory in (workspace, host_bin):
        executable = directory / "docker"
        executable.write_text(script, encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(workspace), str(host_bin))))

    resolved = probe_docker(workspace_root=workspace)
    backend = DockerSandbox(workspace_root=workspace)

    assert resolved == str((host_bin / "docker").resolve())
    assert backend.docker_path == str((host_bin / "docker").resolve())


def test_probe_docker_rejects_unreachable_daemon_or_missing_image() -> None:
    failed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"failed")
    ready = subprocess.CompletedProcess([], 0, stdout=b"27.0\n", stderr=b"")
    with (
        patch(
            "ash.sandbox.docker.resolve_host_executable",
            return_value="/usr/bin/docker",
        ),
        patch("ash.sandbox.docker.run_docker_cli_sync", return_value=failed),
    ):
        assert probe_docker() is None
    with (
        patch(
            "ash.sandbox.docker.resolve_host_executable",
            return_value="/usr/bin/docker",
        ),
        patch(
            "ash.sandbox.docker.run_docker_cli_sync",
            side_effect=[ready, failed],
        ),
    ):
        assert probe_docker() is None


# ---------------------------------------------------------------------------
# Manager run() with mocked tier-1 fallback
# ---------------------------------------------------------------------------


def test_run_fails_closed_when_docker_unavailable_mid_flight(
    tmp_path: Path,
) -> None:
    # Force tier 3 detection, then make the Docker backend raise at
    # wrap-time to exercise the fallback path.
    with (
        patch("ash.sandbox.manager.has_docker", return_value=True),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)

    async def runner() -> object:
        return await mgr.run(["echo", "fallback"], cwd=tmp_path)

    with pytest.raises(SandboxBackendUnavailable):
        asyncio.run(runner())


def test_run_only_falls_back_when_explicitly_enabled(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.has_docker", return_value=True),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(
            workspace_root=tmp_path,
            allow_scoped_fallback=True,
        )

    result = asyncio.run(mgr.run(["echo", "fallback"], cwd=tmp_path))
    assert result.fallback_used is True
    assert result.tier == SANDBOX_TIER_SCOPED
    assert "fallback" in result.stdout


def test_run_with_scoped_tier_executes_directly(tmp_path: Path) -> None:
    with (
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)

    async def runner() -> object:
        return await mgr.run(["echo", "scoped"], cwd=tmp_path)

    result = asyncio.run(runner())
    assert result.tier == SANDBOX_TIER_SCOPED
    assert result.fallback_used is False
    assert "scoped" in result.stdout


def test_run_does_not_spawn_without_tree_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    with (
        patch("ash.sandbox.manager.has_docker", return_value=False),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        manager = SandboxManager(
            workspace_root=tmp_path,
            backend_preference="direct",
        )

    def unavailable(*args: object, **kwargs: object) -> object:
        raise ProcessTreeUnavailable("taskkill unavailable")

    monkeypatch.setattr("ash.sandbox.manager.prepare_process_tree", unavailable)
    monkeypatch.setattr(
        "ash.sandbox.manager.asyncio.create_subprocess_exec",
        lambda *args, **kwargs: pytest.fail("sandbox command must not launch"),
    )

    with pytest.raises(SandboxBackendUnavailable, match="taskkill unavailable"):
        asyncio.run(manager.run(["echo", "unsafe"], cwd=tmp_path))


def test_run_rejects_empty_command(tmp_path: Path) -> None:
    mgr = SandboxManager(workspace_root=tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(mgr.run([]))


def test_run_with_real_bwrap_hides_outside_file_contents(tmp_path: Path) -> None:
    """A real Tier-2 invocation cannot read a host file outside its mounts."""

    if not has_bwrap():
        pytest.skip("bwrap not installed on this host")

    # Use the host home directory rather than /tmp, which bwrap intentionally
    # replaces with a fresh tmpfs.  The parent directory's existence is not
    # sufficient evidence: bwrap may create empty mount-point parents for the
    # workspace's absolute bind path.
    outside_dir = Path(tempfile.mkdtemp(prefix=".ash-bwrap-outside-", dir=Path.home()))
    outside = outside_dir / "outside-sentinel.txt"
    outside.write_text("ASH_OUTSIDE_SENTINEL_CONTENT\n", encoding="utf-8")
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        mgr = SandboxManager(workspace_root=workspace, preferred_tier=2)
        assert mgr.backend_name == "bubblewrap"
        assert mgr.tier == SANDBOX_TIER_BWRAP

        invocation = mgr.prepare(["cat", str(outside)], cwd=workspace)
        assert invocation.backend_name == "bubblewrap"
        assert invocation.tier == SANDBOX_TIER_BWRAP

        async def runner() -> object:
            return await mgr.run(["cat", str(outside)], cwd=workspace, timeout=15)

        result = asyncio.run(runner())
        assert result.backend_name == "bubblewrap"
        assert result.tier == SANDBOX_TIER_BWRAP
        assert result.exit_code != 0
        assert "ASH_OUTSIDE_SENTINEL_CONTENT" not in result.stdout
    finally:
        outside.unlink(missing_ok=True)
        outside_dir.rmdir()


def test_run_with_real_bwrap_hides_host_machine_identity(tmp_path: Path) -> None:
    """A full-isolation backend must not expose host identity files by default."""

    machine_id = Path("/etc/machine-id")
    if not has_bwrap():
        pytest.skip("bwrap not installed on this host")
    if not machine_id.is_file() or not os.access(machine_id, os.R_OK):
        pytest.skip("host machine-id is unavailable")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SandboxManager(
        workspace_root=workspace,
        preferred_tier=SANDBOX_TIER_BWRAP,
        backend_preference="native",
    )
    assert manager.backend_name == "bubblewrap"

    result = asyncio.run(
        manager.run(["/bin/cat", "/etc/machine-id"], cwd=workspace, timeout=15)
    )

    assert result.exit_code != 0
    assert machine_id.read_text(encoding="utf-8").strip() not in result.stdout


def test_run_with_real_bwrap_pins_workspace_across_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host pathname may change, but bwrap must mount the held workspace."""

    if not has_bwrap():
        pytest.skip("bwrap not installed or usable on this host")

    workspace = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    replacement.mkdir()
    (workspace / "marker.txt").write_text("ORIGINAL", encoding="utf-8")
    (replacement / "marker.txt").write_text("REPLACEMENT", encoding="utf-8")
    manager = SandboxManager(
        workspace_root=workspace,
        preferred_tier=SANDBOX_TIER_BWRAP,
        backend_preference="native",
    )
    assert manager.backend_name == "bubblewrap"

    original_wrap = BubblewrapSandbox.wrap
    swapped = False

    def swap_after_wrap(
        backend: BubblewrapSandbox,
        command: list[str] | tuple[str, ...],
        **kwargs: object,
    ) -> list[str]:
        nonlocal swapped
        argv = original_wrap(backend, command, **kwargs)
        if not swapped:
            workspace.rename(saved)
            workspace.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return argv

    monkeypatch.setattr(BubblewrapSandbox, "wrap", swap_after_wrap)

    result = asyncio.run(
        manager.run(["/bin/cat", "marker.txt"], cwd=workspace, timeout=15)
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "ORIGINAL"


def test_run_with_real_bwrap_pins_extra_read_only_path_across_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extra host reads must remain bound to the validated directory inode."""

    if not has_bwrap():
        pytest.skip("bwrap not installed or usable on this host")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_root = Path(
        tempfile.mkdtemp(prefix=".ash-bwrap-readonly-", dir=Path.home())
    )
    extra = external_root / "extra"
    replacement = external_root / "replacement"
    saved = external_root / "extra-saved"
    extra.mkdir()
    replacement.mkdir()
    (extra / "marker.txt").write_text("ORIGINAL", encoding="utf-8")
    (replacement / "marker.txt").write_text("REPLACEMENT", encoding="utf-8")
    manager = SandboxManager(
        workspace_root=workspace,
        preferred_tier=SANDBOX_TIER_BWRAP,
        backend_preference="native",
        extra_read_only_paths=(extra,),
    )
    assert manager.backend_name == "bubblewrap"

    original_wrap = BubblewrapSandbox.wrap
    swapped = False

    def swap_after_wrap(
        backend: BubblewrapSandbox,
        command: list[str] | tuple[str, ...],
        **kwargs: object,
    ) -> list[str]:
        nonlocal swapped
        argv = original_wrap(backend, command, **kwargs)
        if not swapped:
            extra.rename(saved)
            extra.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return argv

    monkeypatch.setattr(BubblewrapSandbox, "wrap", swap_after_wrap)
    try:
        result = asyncio.run(
            manager.run(
                ["/bin/cat", str(extra / "marker.txt")],
                cwd=workspace,
                timeout=15,
            )
        )

        assert result.exit_code == 0, result.stderr
        assert result.stdout == "ORIGINAL"
    finally:
        if extra.is_symlink():
            extra.unlink()
        for path in (saved, replacement):
            marker = path / "marker.txt"
            marker.unlink(missing_ok=True)
            path.rmdir()
        external_root.rmdir()



def test_run_with_real_bwrap_enforces_write_and_network_boundaries(tmp_path: Path) -> None:
    """Real Linux isolation permits workspace writes but blocks host writes/network."""

    if not has_bwrap():
        pytest.skip("bwrap not installed or usable on this host")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SandboxManager(
        workspace_root=workspace,
        preferred_tier=SANDBOX_TIER_BWRAP,
        network=False,
    )
    assert manager.backend_name == "bubblewrap"

    inside = workspace / "inside.txt"
    inside_result = asyncio.run(
        manager.run(
            ["/bin/sh", "-c", f"printf ok > {shlex.quote(str(inside))}"],
            cwd=workspace,
            timeout=15,
        )
    )
    assert inside_result.exit_code == 0, inside_result.stderr
    assert inside.read_text(encoding="utf-8") == "ok"

    outside_dir = Path(tempfile.mkdtemp(prefix=".ash-bwrap-write-", dir=Path.home()))
    outside = outside_dir / "blocked.txt"
    try:
        outside_result = asyncio.run(
            manager.run(
                ["/bin/sh", "-c", f"printf blocked > {shlex.quote(str(outside))}"],
                cwd=workspace,
                timeout=15,
            )
        )
        assert outside_result.exit_code != 0
        assert not outside.exists()
        asyncio.run(_assert_bwrap_loopback_blocked(manager, workspace))
    finally:
        outside.unlink(missing_ok=True)
        outside_dir.rmdir()


async def _assert_bwrap_loopback_blocked(
    manager: SandboxManager, workspace: Path
) -> None:
    accepted = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    python = next(
        (candidate for candidate in (Path("/usr/bin/python3"), Path("/usr/bin/python")) if candidate.exists()),
        None,
    )
    if python is None:
        server.close()
        await server.wait_closed()
        pytest.skip("no system Python inside Bubblewrap system mounts")
    script = (
        "import socket; "
        f"socket.create_connection(('127.0.0.1',{port}), timeout=1).close()"
    )
    try:
        result = await manager.run(
            [str(python), "-c", script],
            cwd=workspace,
            timeout=10,
        )
        assert result.exit_code != 0
        await asyncio.sleep(0.05)
        assert not accepted.is_set()
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# run_command integration with the manager
# ---------------------------------------------------------------------------


def test_run_command_uses_tier1_when_no_sandbox(tmp_path: Path) -> None:
    from ash.safety.guard import SafetyGuard

    guard = SafetyGuard(project_root=tmp_path)
    tool = RunCommandTool(guard)  # no sandbox_manager
    result = asyncio.run(tool.run(command_line="echo plain", cwd=str(tmp_path)))
    assert result.success is True
    assert "plain" in result.output


def test_sandbox_manager_forwards_streaming_output(tmp_path: Path) -> None:
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")
    observed: list[tuple[str, str]] = []

    result = asyncio.run(
        manager.run(
            [sys.executable, "-c", "print('sandbox-stream', flush=True)"],
            cwd=tmp_path,
            stream_callback=lambda stream, delta: observed.append((stream, delta)),
        )
    )

    assert result.exit_code == 0
    assert observed == [("stdout", "sandbox-stream\n")]


@pytest.mark.asyncio
async def test_sandbox_manager_marks_scoped_timeout_after_effect(tmp_path: Path) -> None:
    marker = tmp_path / "scoped-timeout-effect.txt"
    script = (
        "from pathlib import Path; import time; "
        f"Path({str(marker)!r}).write_text('effect'); time.sleep(30)"
    )
    manager = SandboxManager(workspace_root=tmp_path, backend_preference="direct")

    result = await manager.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        timeout=1,
    )

    assert marker.read_text(encoding="utf-8") == "effect"
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_sandbox_subprocess_marks_timeout_after_effect(tmp_path: Path) -> None:
    from ash.sandbox.manager import _run_subprocess

    marker = tmp_path / "subprocess-timeout-effect.txt"
    script = (
        "from pathlib import Path; import time; "
        f"Path({str(marker)!r}).write_text('effect'); time.sleep(30)"
    )

    result = await _run_subprocess(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        deadline=1,
        tier=SANDBOX_TIER_BWRAP,
        backend_name="test",
        workspace_root=tmp_path,
    )

    assert marker.read_text(encoding="utf-8") == "effect"
    assert result.timed_out is True


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_sandbox_timeout_cleanup_survives_second_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapped: bool,
) -> None:
    import ash.sandbox.manager as sandbox_module
    import ash.sandbox.process_utils as process_utils

    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_complete = asyncio.Event()
    cleanup_calls = 0

    async def fake_spawn(*_args, **_kwargs):
        return SimpleNamespace(pid=12345, returncode=None)

    async def blocked_output(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def fake_terminate(_process, *, plan, grace_seconds=1.0):
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_complete.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(sandbox_module, "communicate_process", blocked_output)
    monkeypatch.setattr(process_utils, "terminate_process_tree", fake_terminate)

    kwargs = dict(
        cwd=tmp_path,
        deadline=0.02,
        workspace_root=tmp_path,
        env={"PATH": os.environ.get("PATH", os.defpath)},
    )
    if wrapped:
        runner = sandbox_module._run_subprocess(
            ["/bin/sh", "-c", "true"],
            tier=SANDBOX_TIER_BWRAP,
            backend_name="test",
            **kwargs,
        )
    else:
        runner = sandbox_module._run_scoped(
            sandbox_module._ScopedBackend(),
            ["/bin/sh", "-c", "true"],
            fallback=False,
            **kwargs,
        )
    task = asyncio.create_task(runner)
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.05)
        assert not done, "sandbox returned before subprocess cleanup finished"
    finally:
        cleanup_release.set()
        if not task.done():
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_complete.is_set()
    assert cleanup_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "timed_out", "outcome_unknown", "expected_outcome"),
    [
        (-1, True, False, ToolExecutionOutcome.UNKNOWN),
        (-15, False, False, ToolExecutionOutcome.COMPLETED),
        (-15, False, True, ToolExecutionOutcome.UNKNOWN),
    ],
)
async def test_run_command_maps_sandbox_outcome(
    tmp_path: Path,
    exit_code: int,
    timed_out: bool,
    outcome_unknown: bool,
    expected_outcome: ToolExecutionOutcome,
) -> None:
    class FakeSandboxManager:
        tier = SANDBOX_TIER_BWRAP

        async def run(self, *args: object, **kwargs: object) -> SandboxResult:
            return SandboxResult(
                exit_code=exit_code,
                stdout="",
                stderr=(
                    "Command timed out after 1 seconds."
                    if timed_out
                    else "process exited by signal"
                ),
                tier=SANDBOX_TIER_BWRAP,
                backend_name="test",
                timed_out=timed_out,
                outcome_unknown=outcome_unknown,
            )

    from ash.safety.guard import SafetyGuard

    tool = RunCommandTool(
        SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        sandbox_manager=FakeSandboxManager(),  # type: ignore[arg-type]
    )

    result = await tool.run(command_line="true", timeout_seconds=1)

    assert result.outcome is expected_outcome


def test_run_command_with_sandbox_annotates_output(tmp_path: Path) -> None:
    if not has_bwrap():
        pytest.skip("bwrap not installed on this host")
    from ash.safety.guard import SafetyGuard

    guard = SafetyGuard(project_root=tmp_path)
    mgr = SandboxManager(workspace_root=tmp_path, preferred_tier=2)
    tool = RunCommandTool(guard, sandbox_manager=mgr)
    result = asyncio.run(tool.run(command_line="echo annotated", cwd=str(tmp_path)))
    assert result.success is True
    # Output includes the sandbox tier annotation.
    assert "sandbox tier=2" in result.output
    assert "annotated" in result.output


def test_run_command_fails_closed_when_sandbox_unavailable(tmp_path: Path) -> None:
    from ash.safety.guard import SafetyGuard

    guard = SafetyGuard(project_root=tmp_path)
    # Manager that detects Docker (which isn't installed) so wrap fails.
    with (
        patch("ash.sandbox.manager.has_docker", return_value=True),
        patch("ash.sandbox.manager.has_bwrap", return_value=False),
        patch("ash.sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
    assert mgr.tier == SANDBOX_TIER_DOCKER  # by detection
    tool = RunCommandTool(guard, sandbox_manager=mgr)
    result = asyncio.run(tool.run(command_line="echo ok", cwd=str(tmp_path)))
    assert result.success is False
    assert "Sandbox unavailable; command was not run" in (result.error or "")


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Bubblewrap is a Linux sandbox backend",
)
def test_bubblewrap_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = BubblewrapSandbox(workspace_root=workspace, bwrap_path=str(fake))

    with pytest.raises(SandboxBackendUnavailable, match="outside"):
        backend.wrap(["pwd"], cwd=tmp_path)


def test_macos_profile_only_writes_workspace_and_temp(tmp_path: Path) -> None:
    from ash.sandbox.manager import _SandboxExecBackend

    workspace = tmp_path / 'workspace "quoted"'
    workspace.mkdir()
    with patch("ash.sandbox.manager.sys.platform", "darwin"):
        argv = _SandboxExecBackend(
            workspace_root=workspace,
            sandbox_exec_path="/usr/bin/sandbox-exec",
        ).wrap(
            ["echo", "ok"], cwd=workspace
        )

    profile = argv[argv.index("-p") + 1]
    escaped_workspace = str(workspace).replace('"', '\\"')
    assert f'(subpath "{escaped_workspace}")' in profile
    assert '(subpath "/Users")' not in profile
    assert "(deny network-outbound)" in profile


def test_macos_profile_can_explicitly_allow_network(tmp_path: Path) -> None:
    from ash.sandbox.manager import _SandboxExecBackend

    with patch("ash.sandbox.manager.sys.platform", "darwin"):
        profile = _SandboxExecBackend(
            workspace_root=tmp_path,
            network=True,
            sandbox_exec_path="/usr/bin/sandbox-exec",
        ).wrap(["echo", "ok"], cwd=tmp_path)[2]
    assert "(allow network*)" in profile
    assert "(deny network-outbound)" not in profile


def test_macos_profile_can_deny_workspace_writes(tmp_path: Path) -> None:
    from ash.sandbox.manager import _SandboxExecBackend

    with patch("ash.sandbox.manager.sys.platform", "darwin"):
        profile = _SandboxExecBackend(
            workspace_root=tmp_path,
            workspace_read_only=True,
            sandbox_exec_path="/usr/bin/sandbox-exec",
        ).wrap(["echo", "ok"], cwd=tmp_path)[2]

    assert f'(allow file-write* (subpath "{tmp_path}"))' not in profile
    assert '(allow file-write* (subpath "/tmp"))' in profile
