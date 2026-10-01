from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

from ash.mcp.client import MCPClient
from ash.mcp.server import MCPServerConfig


SERVER_SOURCE = r'''
import sys

from mcp.server import MCPServer

mcp = MCPServer("ash-conformance")

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--http":
        mcp.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=0,
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
        )
    else:
        mcp.run()
'''

_UVICORN_LISTENING = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")


def _official_sdk_python() -> Path:
    interpreter = os.environ.get("ASH_MCP_SDK_PYTHON")
    if not interpreter:
        pytest.skip("set ASH_MCP_SDK_PYTHON to an interpreter with mcp==2.2.0")
    python = Path(interpreter)
    if not python.is_file():
        pytest.fail(f"configured MCP SDK interpreter does not exist: {python}")
    return python


async def _start_http_server(
    python: Path,
    server: Path,
) -> tuple[asyncio.subprocess.Process, int]:
    process = await asyncio.create_subprocess_exec(
        str(python),
        str(server),
        "--http",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stderr is not None
    startup_lines: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    try:
        for _ in range(50):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            line = await asyncio.wait_for(
                process.stderr.readline(),
                timeout=remaining,
            )
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            startup_lines.append(text)
            match = _UVICORN_LISTENING.search(text)
            if match is not None:
                return process, int(match.group(1))
    except TimeoutError:
        pass

    await _stop_process(process)
    pytest.fail(
        "official MCP SDK HTTP server did not become ready:\n"
        + "\n".join(startup_lines[-20:])
    )


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


@pytest.mark.asyncio
async def test_stdio_conforms_with_official_mcp_python_sdk(tmp_path: Path) -> None:
    python = _official_sdk_python()

    server = tmp_path / "official_mcp_server.py"
    server.write_text(SERVER_SOURCE, encoding="utf-8")
    client = MCPClient(
        MCPServerConfig(
            name="official-sdk",
            command=str(python),
            args=[str(server)],
            env={},
        ),
        timeout=10,
    )
    try:
        await client.connect()

        assert client.protocol_version == "2026-07-28"
        assert client.server_info.get("name") == "ash-conformance"

        tools = await client.list_tools()
        assert [tool.get("name") for tool in tools] == ["add"]

        result = await client.call_tool("add", {"a": 2, "b": 5})
        assert result["isError"] is False
        assert result["structuredContent"] == {"result": 7}
        assert result["content"] == [{"text": "7", "type": "text"}]
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_streamable_http_conforms_with_official_mcp_python_sdk(
    tmp_path: Path,
) -> None:
    python = _official_sdk_python()
    server = tmp_path / "official_mcp_server.py"
    server.write_text(SERVER_SOURCE, encoding="utf-8")
    process, port = await _start_http_server(python, server)
    client = MCPClient(
        MCPServerConfig(
            name="official-sdk-http",
            command="",
            args=[],
            env={},
            transport="http",
            url=f"http://127.0.0.1:{port}/mcp",
        ),
        timeout=10,
    )
    try:
        await client.connect()

        assert client.protocol_version == "2026-07-28"
        assert client.server_info.get("name") == "ash-conformance"

        tools = await client.list_tools()
        assert [tool.get("name") for tool in tools] == ["add"]

        result = await client.call_tool("add", {"a": 2, "b": 5})
        assert result["isError"] is False
        assert result["structuredContent"] == {"result": 7}
        assert result["content"] == [{"text": "7", "type": "text"}]
    finally:
        try:
            await client.disconnect()
        finally:
            await _stop_process(process)
