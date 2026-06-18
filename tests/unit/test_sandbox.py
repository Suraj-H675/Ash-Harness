"""Unit tests for the sandbox manager and backends (Sprint 11)."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from sandbox import (
    BubblewrapSandbox,
    DockerSandbox,
    SANDBOX_TIER_BWRAP,
    SANDBOX_TIER_DOCKER,
    SANDBOX_TIER_SCOPED,
    SandboxBackendUnavailable,
    SandboxManager,
    has_bwrap,
    has_docker,
    has_sandbox_exec,
)
from sandbox.bwrap import probe_bwrap
from sandbox.docker import probe_docker
from tools.command import RunCommandTool


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
        # On macOS, reflects whether the binary is actually on PATH.
        assert has_sandbox_exec() is (shutil.which("sandbox-exec") is not None)


# ---------------------------------------------------------------------------
# tier detection
# ---------------------------------------------------------------------------


def test_manager_picks_highest_available_tier(tmp_path: Path) -> None:
    with (
        patch("sandbox.manager.has_docker", return_value=False),
        patch("sandbox.manager.has_bwrap", return_value=True),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.tier == SANDBOX_TIER_BWRAP
        assert mgr.backend_name == "bubblewrap"


def test_manager_prefers_docker_when_available(tmp_path: Path) -> None:
    with (
        patch("sandbox.manager.has_docker", return_value=True),
        patch("sandbox.manager.has_bwrap", return_value=True),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.tier == SANDBOX_TIER_DOCKER
        assert mgr.backend_name == "docker"


def test_manager_falls_back_to_scoped_when_nothing_available(tmp_path: Path) -> None:
    with (
        patch("sandbox.manager.has_docker", return_value=False),
        patch("sandbox.manager.has_bwrap", return_value=False),
        patch("sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.tier == SANDBOX_TIER_SCOPED
        assert mgr.backend_name == "scoped"


def test_manager_respects_preferred_tier(tmp_path: Path) -> None:
    with (
        patch("sandbox.manager.has_docker", return_value=True),
        patch("sandbox.manager.has_bwrap", return_value=True),
    ):
        mgr = SandboxManager(workspace_root=tmp_path, preferred_tier=2)
        assert mgr.tier == SANDBOX_TIER_BWRAP
        assert mgr.backend_name == "bubblewrap"


def test_manager_capabilities_reports_each_backend(tmp_path: Path) -> None:
    with (
        patch("sandbox.manager.has_docker", return_value=False),
        patch("sandbox.manager.has_bwrap", return_value=True),
        patch("sandbox.manager.has_sandbox_exec", return_value=False),
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
        patch("sandbox.manager.has_docker", return_value=False),
        patch("sandbox.manager.has_bwrap", return_value=True),
        patch("sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.is_fully_isolated() is True

    with (
        patch("sandbox.manager.has_docker", return_value=False),
        patch("sandbox.manager.has_bwrap", return_value=False),
        patch("sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
        assert mgr.is_fully_isolated() is False


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
    assert "--unshare-pid" in argv
    assert "--unshare-uts" in argv
    assert "--unshare-ipc" in argv
    assert "--die-with-parent" in argv
    # Network is off by default.
    assert "--unshare-net" in argv
    # Workspace is bound.
    assert "--bind" in argv
    assert str(tmp_path) in argv
    # Command separator and the actual command are at the tail.
    assert "--" in argv
    assert argv[-2:] == ["echo", "hi"]


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
    # Volume mount for the workspace.
    assert "--volume" in argv
    assert "/workspace:rw" in " ".join(argv)
    # Image + command tail.
    assert argv[-2:] == ["echo", "hi"]


def test_docker_with_network_omits_none_flag(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    backend = DockerSandbox(
        workspace_root=tmp_path, docker_path=str(fake), network=True
    )
    argv = backend.wrap(["echo", "hi"])
    assert "--network=none" not in argv


# ---------------------------------------------------------------------------
# Manager run() with mocked tier-1 fallback
# ---------------------------------------------------------------------------


def test_run_falls_back_to_scoped_when_docker_unavailable_mid_flight(
    tmp_path: Path,
) -> None:
    # Force tier 3 detection, then make the Docker backend raise at
    # wrap-time to exercise the fallback path.
    with (
        patch("sandbox.manager.has_docker", return_value=True),
        patch("sandbox.manager.has_bwrap", return_value=False),
        patch("sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)

    # The Docker backend should fail when actually wrapping because no
    # docker binary exists; the manager must transparently fall back.
    async def runner() -> object:
        return await mgr.run(["echo", "fallback"], cwd=tmp_path)

    result = asyncio.run(runner())
    assert result.fallback_used is True
    assert result.tier == SANDBOX_TIER_SCOPED
    assert "fallback" in result.stdout


def test_run_with_scoped_tier_executes_directly(tmp_path: Path) -> None:
    with (
        patch("sandbox.manager.has_docker", return_value=False),
        patch("sandbox.manager.has_bwrap", return_value=False),
        patch("sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)

    async def runner() -> object:
        return await mgr.run(["echo", "scoped"], cwd=tmp_path)

    result = asyncio.run(runner())
    assert result.tier == SANDBOX_TIER_SCOPED
    assert result.fallback_used is False
    assert "scoped" in result.stdout


def test_run_rejects_empty_command(tmp_path: Path) -> None:
    mgr = SandboxManager(workspace_root=tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(mgr.run([]))


def test_run_with_real_bwrap_actually_isolates(tmp_path: Path) -> None:
    """End-to-end: if bwrap is available, the sandboxed child must not
    be able to read files outside the workspace mount."""

    if not has_bwrap():
        pytest.skip("bwrap not installed on this host")

    # Create a file outside the workspace.
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    # Sandbox the workspace.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mgr = SandboxManager(workspace_root=workspace, preferred_tier=2)
    assert mgr.tier == SANDBOX_TIER_BWRAP

    # Try to read the outside file from inside the sandbox.
    async def runner() -> object:
        return await mgr.run(["cat", str(outside)], cwd=workspace, timeout=15)

    result = asyncio.run(runner())
    # The sandbox should make the file unreadable (No such file or
    # Permission denied). Either is a containment success.
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# run_command integration with the manager
# ---------------------------------------------------------------------------


def test_run_command_uses_tier1_when_no_sandbox(tmp_path: Path) -> None:
    from safety.guard import SafetyGuard

    guard = SafetyGuard(project_root=tmp_path)
    tool = RunCommandTool(guard)  # no sandbox_manager
    result = asyncio.run(tool.run(command_line="echo plain", cwd=str(tmp_path)))
    assert result.success is True
    assert "plain" in result.output


def test_run_command_with_sandbox_annotates_output(tmp_path: Path) -> None:
    if not has_bwrap():
        pytest.skip("bwrap not installed on this host")
    from safety.guard import SafetyGuard

    guard = SafetyGuard(project_root=tmp_path)
    mgr = SandboxManager(workspace_root=tmp_path, preferred_tier=2)
    tool = RunCommandTool(guard, sandbox_manager=mgr)
    result = asyncio.run(tool.run(command_line="echo annotated", cwd=str(tmp_path)))
    assert result.success is True
    # Output includes the sandbox tier annotation.
    assert "sandbox tier=2" in result.output
    assert "annotated" in result.output


def test_run_command_falls_back_when_sandbox_unavailable(tmp_path: Path) -> None:
    from safety.guard import SafetyGuard

    guard = SafetyGuard(project_root=tmp_path)
    # Manager that detects Docker (which isn't installed) so wrap fails.
    with (
        patch("sandbox.manager.has_docker", return_value=True),
        patch("sandbox.manager.has_bwrap", return_value=False),
        patch("sandbox.manager.has_sandbox_exec", return_value=False),
    ):
        mgr = SandboxManager(workspace_root=tmp_path)
    assert mgr.tier == SANDBOX_TIER_DOCKER  # by detection
    tool = RunCommandTool(guard, sandbox_manager=mgr)
    # Actual run should still succeed via the scoped fallback.
    result = asyncio.run(tool.run(command_line="echo ok", cwd=str(tmp_path)))
    assert result.success is True
    assert "ok" in result.output
