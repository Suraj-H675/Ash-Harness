from __future__ import annotations

import os
from pathlib import Path

import pytest

from ash.mcp.client import MCPClient
from ash.mcp.server import MCPServerConfig


SERVER_SOURCE = r'''
from mcp.server import MCPServer

mcp = MCPServer("ash-conformance")

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

if __name__ == "__main__":
    mcp.run()
'''


@pytest.mark.asyncio
async def test_stdio_conforms_with_official_mcp_python_sdk(tmp_path: Path) -> None:
    interpreter = os.environ.get("ASH_MCP_SDK_PYTHON")
    if not interpreter:
        pytest.skip("set ASH_MCP_SDK_PYTHON to an interpreter with mcp==2.2.0")
    python = Path(interpreter)
    if not python.is_file():
        pytest.fail(f"configured MCP SDK interpreter does not exist: {python}")

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
