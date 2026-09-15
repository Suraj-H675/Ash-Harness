"""Native macOS sandbox enforcement probes."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

from ash.sandbox import SandboxManager, has_sandbox_exec


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="native sandbox-exec enforcement is macOS-only",
)


def test_native_macos_sandbox_enforces_write_and_network_boundaries(
    tmp_path: Path,
) -> None:
    assert has_sandbox_exec(tmp_path), "first-class macOS requires sandbox-exec"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SandboxManager(
        workspace_root=workspace,
        backend_preference="native",
        network=False,
    )
    assert manager.backend_name == "sandbox-exec"

    inside = workspace / "inside.txt"
    inside_result = asyncio.run(
        manager.run(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(inside)!r}).write_text('ok')",
            ],
            cwd=workspace,
        )
    )
    assert inside_result.exit_code == 0, inside_result.stderr
    assert inside.read_text(encoding="utf-8") == "ok"

    outside = Path.home() / f".ash-sandbox-probe-{uuid.uuid4().hex}"
    try:
        outside_result = asyncio.run(
            manager.run(
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(outside)!r}).write_text('blocked')",
                ],
                cwd=workspace,
            )
        )
        assert outside_result.exit_code != 0
        assert not outside.exists()
    finally:
        outside.unlink(missing_ok=True)

    asyncio.run(_assert_loopback_blocked(manager, workspace))


async def _assert_loopback_blocked(manager: SandboxManager, workspace: Path) -> None:
    accepted = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        result = await manager.run(
            [
                sys.executable,
                "-c",
                (
                    "import socket; "
                    f"s=socket.create_connection(('127.0.0.1',{port}),timeout=1); "
                    "s.close()"
                ),
            ],
            cwd=workspace,
        )
        assert result.exit_code != 0
        await asyncio.sleep(0.05)
        assert not accepted.is_set()
    finally:
        server.close()
        await server.wait_closed()
