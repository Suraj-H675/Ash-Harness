"""Async Model Context Protocol client with stdio and HTTP transports."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from mcp.server import MCPServerConfig
from sandbox.process_utils import process_group_options, terminate_process_tree


class MCPProtocolError(RuntimeError):
    """Raised for JSON-RPC or MCP negotiation failures."""


class MCPClient:
    """One initialized connection to an MCP server."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = http_client
        self._owns_http = http_client is None
        self._http_session_id = ""
        self._initialized = False

    async def connect(self) -> None:
        if self._initialized:
            return
        if self.config.transport == "stdio":
            await self._connect_stdio()
        elif self.config.transport in {"http", "sse"}:
            if not self.config.resolved_url:
                raise MCPProtocolError(f"MCP server {self.config.name!r} has no URL")
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=self.timeout)
        else:
            raise MCPProtocolError(
                f"Unsupported MCP transport: {self.config.transport}"
            )
        await self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ash", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized", {})
        self._initialized = True

    async def _connect_stdio(self) -> None:
        env = {**os.environ, **self.config.resolved_env}
        self._process = await asyncio.create_subprocess_exec(
            self.config.resolved_command,
            *self.config.resolved_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **process_group_options(),
        )
        self._reader_task = asyncio.create_task(self._read_stdio())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _read_stdio(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        while line := await self._process.stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            request_id = message.get("id")
            if isinstance(request_id, int) and request_id in self._pending:
                future = self._pending.pop(request_id)
                if not future.done():
                    future.set_result(message)
        error = MCPProtocolError(f"MCP server {self.config.name!r} closed its stdout")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while await self._process.stderr.readline():
            pass

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if self.config.transport == "stdio":
            if self._process is None or self._process.stdin is None:
                raise MCPProtocolError("MCP stdio client is not connected")
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            async with self._write_lock:
                self._process.stdin.write(
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode()
                )
                await self._process.stdin.drain()
            try:
                response = await asyncio.wait_for(future, timeout=self.timeout)
            finally:
                self._pending.pop(request_id, None)
        else:
            if self._http is None:
                raise MCPProtocolError("MCP HTTP client is not connected")
            headers = {
                **self.config.resolved_headers,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            if self._http_session_id:
                headers["Mcp-Session-Id"] = self._http_session_id
            response_http = await self._http.post(
                self.config.resolved_url,
                json=payload,
                headers=headers,
            )
            response_http.raise_for_status()
            session_id = response_http.headers.get("Mcp-Session-Id")
            if session_id:
                self._http_session_id = session_id
            response = _parse_http_response(response_http, request_id)
        if "error" in response:
            error = response["error"]
            raise MCPProtocolError(
                f"{method} failed ({error.get('code')}): {error.get('message')}"
            )
        result = response.get("result", {})
        return result if isinstance(result, dict) else {"value": result}

    async def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if self.config.transport == "stdio":
            if self._process is None or self._process.stdin is None:
                raise MCPProtocolError("MCP stdio client is not connected")
            async with self._write_lock:
                self._process.stdin.write(
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode()
                )
                await self._process.stdin.drain()
        elif self._http is not None:
            headers = self.config.resolved_headers
            if self._http_session_id:
                headers["Mcp-Session-Id"] = self._http_session_id
            response = await self._http.post(
                self.config.resolved_url, json=payload, headers=headers
            )
            response.raise_for_status()

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.request("tools/list")
        tools = result.get("tools", [])
        return [tool for tool in tools if isinstance(tool, dict)]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )

    async def list_resources(self) -> list[dict[str, Any]]:
        result = await self.request("resources/list")
        return [item for item in result.get("resources", []) if isinstance(item, dict)]

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return await self.request("resources/read", {"uri": uri})

    async def list_prompts(self) -> list[dict[str, Any]]:
        result = await self.request("prompts/list")
        return [item for item in result.get("prompts", []) if isinstance(item, dict)]

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return await self.request(
            "prompts/get", {"name": name, "arguments": arguments or {}}
        )

    async def disconnect(self) -> None:
        self._initialized = False
        if self._http is not None and self._http_session_id:
            try:
                await self._http.delete(
                    self.config.resolved_url,
                    headers={
                        **self.config.resolved_headers,
                        "Mcp-Session-Id": self._http_session_id,
                    },
                )
            except httpx.HTTPError:
                pass
            self._http_session_id = ""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None
        if self._process is not None:
            await terminate_process_tree(self._process)
            self._process = None
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task),
            return_exceptions=True,
        )
        self._reader_task = None
        self._stderr_task = None


def _parse_http_response(response: httpx.Response, request_id: int) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").casefold()
    if "text/event-stream" not in content_type:
        payload = response.json()
        if not isinstance(payload, dict):
            raise MCPProtocolError("MCP HTTP response must be a JSON object")
        return payload
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("id") == request_id:
            return payload
    raise MCPProtocolError(f"MCP SSE response omitted request id {request_id}")
