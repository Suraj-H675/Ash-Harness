from __future__ import annotations

import asyncio
import os
import shlex
import socket
import sys
import time
from pathlib import Path

import pytest

from ash.safety.environment import build_scrubbed_environment
from ash.safety.guard import SafetyGuard
from ash.sandbox import SandboxManager
from ash.tools.command import RunCommandTool


@pytest.mark.skipif(
    os.name == "nt" or os.environ.get("ASH_RUN_NATIVE_SANDBOX_TESTS") != "1",
    reason="set ASH_RUN_NATIVE_SANDBOX_TESTS=1 on a supported native host",
)
def test_native_sandbox_enforces_supported_host_contract(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host_secret = tmp_path / "host-secret.txt"
    host_secret.write_text("host-secret", encoding="utf-8")
    outside_write = Path("/var/tmp") / (
        f"ash-native-sandbox-{os.getpid()}-{time.monotonic_ns()}.txt"
    )
    outside_write.unlink(missing_ok=True)

    manager = SandboxManager(
        workspace_root=workspace,
        backend_preference="native",
        network=False,
    )
    if sys.platform.startswith("linux"):
        assert manager.backend_name == "bubblewrap"
        assert manager.is_fully_isolated() is True
    elif sys.platform == "darwin":
        assert manager.backend_name == "sandbox-exec"
        assert manager.is_fully_isolated() is False
    else:
        pytest.skip(f"unsupported native sandbox host: {sys.platform}")

    script = f"""
set -u
printf 'inside' > inside.txt
if printf 'outside' > {shlex.quote(str(outside_write))} 2>/dev/null; then
  echo OUTSIDE_WRITE=allowed
else
  echo OUTSIDE_WRITE=blocked
fi
if cat {shlex.quote(str(host_secret))} >/dev/null 2>&1; then
  echo HOST_READ=allowed
else
  echo HOST_READ=blocked
fi
"""
    try:
        result = asyncio.run(
            manager.run(
                ["/bin/sh", "-c", script],
                cwd=workspace,
                timeout=30,
                env=build_scrubbed_environment(),
            )
        )
        assert result.exit_code == 0, result.stderr
        assert (workspace / "inside.txt").read_text(encoding="utf-8") == "inside"
        assert not outside_write.exists()
        assert "OUTSIDE_WRITE=blocked" in result.stdout
        if sys.platform.startswith("linux"):
            assert "HOST_READ=blocked" in result.stdout
        else:
            # Ash's sandbox-exec profile does not provide host-read isolation,
            # but macOS may independently deny access to particular paths.
            assert manager.status()["filesystem"] == "host-read;workspace-write"
    finally:
        outside_write.unlink(missing_ok=True)


@pytest.mark.skipif(
    os.name == "nt" or os.environ.get("ASH_RUN_NATIVE_SANDBOX_TESTS") != "1",
    reason="set ASH_RUN_NATIVE_SANDBOX_TESTS=1 on a supported native host",
)
def test_native_sandbox_preserves_real_pty_for_tty_required_cli(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host_secret = tmp_path / "pty-host-secret.txt"
    host_secret.write_text("pty-host-secret", encoding="utf-8")
    outside_write = Path("/var/tmp") / (
        f"ash-native-pty-{os.getpid()}-{time.monotonic_ns()}.txt"
    )
    outside_write.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    manager = SandboxManager(
        workspace_root=workspace,
        backend_preference="native",
        network=False,
    )
    if sys.platform.startswith("linux"):
        assert manager.backend_name == "bubblewrap"
    elif sys.platform == "darwin":
        assert manager.backend_name == "sandbox-exec"
        refused = asyncio.run(
            RunCommandTool(
                SafetyGuard(workspace),
                project_root=workspace,
                sandbox_manager=manager,
            ).run(command_line="printf should-not-run", pty=True)
        )
        assert refused.success is False
        assert "sandbox-exec backend" in (refused.error or "")
        manager = SandboxManager(
            workspace_root=workspace,
            backend_preference="direct",
            network=False,
        )
        assert manager.backend_name == "scoped"
    else:
        pytest.skip(f"unsupported native sandbox host: {sys.platform}")
    script = f"""
import os
import socket
from pathlib import Path

print('tty=' + ','.join(str(os.isatty(fd)) for fd in (0, 1, 2)))
print('foreground=' + str(os.tcgetpgrp(0) == os.getpgrp()))
Path('pty-inside.txt').write_text('inside', encoding='utf-8')
try:
    Path({str(outside_write)!r}).write_text('outside', encoding='utf-8')
except OSError:
    print('OUTSIDE_WRITE=blocked')
else:
    print('OUTSIDE_WRITE=allowed')
try:
    Path({str(host_secret)!r}).read_text(encoding='utf-8')
except OSError:
    print('HOST_READ=blocked')
else:
    print('HOST_READ=allowed')
s = socket.socket()
s.settimeout(0.5)
try:
    s.connect(('127.0.0.1', {port}))
except OSError:
    print('NETWORK=blocked')
else:
    print('NETWORK=allowed')
finally:
    s.close()
"""
    sandbox_python = "/usr/bin/python3" if sys.platform.startswith("linux") else sys.executable
    command = f"{shlex.quote(sandbox_python)} -c {shlex.quote(script)}"
    try:
        result = asyncio.run(
            RunCommandTool(
                SafetyGuard(workspace),
                project_root=workspace,
                sandbox_manager=manager,
            ).run(command_line=command, pty=True)
        )

        assert result.success is True, result.error
        if sys.platform.startswith("linux"):
            assert f"backend={manager.backend_name}" in result.output
        else:
            assert "[sandbox tier=" not in result.output
        assert "tty=True,True,True" in result.output
        assert "foreground=True" in result.output
        if sys.platform.startswith("linux"):
            assert "OUTSIDE_WRITE=blocked" in result.output
            assert "NETWORK=blocked" in result.output
        else:
            assert "OUTSIDE_WRITE=allowed" in result.output
            assert "NETWORK=allowed" in result.output
        assert (workspace / "pty-inside.txt").read_text(encoding="utf-8") == "inside"
        if sys.platform.startswith("linux"):
            assert not outside_write.exists()
            assert "HOST_READ=blocked" in result.output
        else:
            assert outside_write.exists()
    finally:
        listener.close()
        outside_write.unlink(missing_ok=True)
