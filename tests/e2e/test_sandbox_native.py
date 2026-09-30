from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from ash.safety.environment import build_scrubbed_environment
from ash.sandbox import SandboxManager


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
            assert "HOST_READ=allowed" in result.stdout
    finally:
        outside_write.unlink(missing_ok=True)
