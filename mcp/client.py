"""Transport-aware MCP client connections."""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any, AsyncGenerator

from mcp.server import MCPServerConfig


class MCPClient:
    """Manages MCP server connections using transport-specific protocols."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._http_queue: asyncio.Queue[dict[str, Any]] | None = None

    async def connect_stdio(self) -> None:
        """Connect via stdio transport (default)."""
        env = {**subprocess.os.environ, **self.config.env}
        self._process = subprocess.Popen(
            [self.config.command, *self.config.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    async def connect_sse(self, url: str | None = None) -> None:
        """Connect via Server-Sent Events transport."""
        import httpx

        target = url or self.config.url
        self._http_queue = asyncio.Queue()
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("GET", target) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data:
                            self._http_queue.put_nowait(json.loads(data))

    async def connect_http(self, url: str | None = None) -> None:
        """Connect via HTTP/WebSocket transport."""
        import httpx

        target = url or self.config.url
        self._http_queue = asyncio.Queue()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(target, json={"jsonrpc": "2.0", "id": 1})
            resp.raise_for_status()

    async def connect_websocket(self, url: str | None = None) -> None:
        """Connect via WebSocket transport."""
        import websockets  # type: ignore[import-not-found]

        target = url or self.config.url
        self._http_queue = asyncio.Queue()
        async with websockets.connect(target) as ws:
            while True:
                msg = await ws.recv()
                self._http_queue.put_nowait(json.loads(msg))

    async def connect(self) -> None:
        """Connect using the transport specified in the config."""
        transport = self.config.transport
        if transport == "stdio":
            await self.connect_stdio()
        elif transport == "sse":
            await self.connect_sse()
        elif transport == "http":
            await self.connect_http()
        elif transport == "websocket":
            await self.connect_websocket()
        else:
            msg = f"Unknown MCP transport: {transport}"
            raise ValueError(msg)

    async def disconnect(self) -> None:
        """Disconnect and clean up resources."""
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        self._http_queue = None

    async def send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request to the MCP server."""
        if self.config.transport == "stdio" and self._process is not None:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params or {},
            }
            self._process.stdin.write((json.dumps(payload) + "\n").encode())
            self._process.stdin.flush()
            line = self._process.stdout.readline()
            return json.loads(line.decode())
        elif self._http_queue is not None:
            import httpx

            target = self.config.url
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    target,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": params or {},
                    },
                )
                resp.raise_for_status()
                return resp.json()
        else:
            msg = "Not connected. Call connect() first."
            raise RuntimeError(msg)

    async def iterate_notifications(self) -> AsyncGenerator[dict[str, Any], None]:
        """Yield notifications from the MCP server (for SSE/WebSocket)."""
        if self._http_queue is not None:
            while True:
                try:
                    notification = await asyncio.wait_for(
                        self._http_queue.get(), timeout=1.0
                    )
                    yield notification
                except asyncio.TimeoutError:
                    continue
