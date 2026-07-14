"""Async Model Context Protocol client with stdio and HTTP transports."""

from __future__ import annotations

import asyncio
import inspect
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from ash.mcp.server import MCPServerConfig
from ash.mcp.oauth import (
    MCPAuthorizationRequired,
    MCPOAuthError,
    MCPOAuthSession,
    bearer_challenge_parameters,
    normalize_oauth_scope,
)
from ash.safety.environment import build_scrubbed_environment
from ash.sandbox.process_utils import process_group_options, terminate_process_tree


LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {LATEST_PROTOCOL_VERSION, "2025-06-18", "2025-03-26", "2024-11-05"}
)
MAX_PAGINATION_PAGES = 100
MAX_STDIO_MESSAGE_BYTES = 8 * 1024 * 1024

RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
_MISSING = object()


class MCPProtocolError(RuntimeError):
    """Raised for JSON-RPC or MCP negotiation failures."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        data: Any = _MISSING,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.has_data = data is not _MISSING
        self.data = None if data is _MISSING else data


class MCPClient:
    """One initialized MCP connection with negotiated client capabilities."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        roots: tuple[Path, ...] = (),
        sampling_handler: RequestHandler | None = None,
        sampling_supports_tools: bool = False,
        elicitation_handler: RequestHandler | None = None,
        elicitation_modes: tuple[str, ...] = ("form",),
        notification_handler: NotificationHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        oauth_session: MCPOAuthSession | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("MCP timeout must be positive")
        invalid_modes = set(elicitation_modes) - {"form", "url"}
        if invalid_modes:
            raise ValueError("MCP elicitation modes must be form or url")
        self.config = config
        self.timeout = timeout
        self.roots = tuple(root.expanduser().resolve() for root in roots)
        self.sampling_handler = sampling_handler
        self.sampling_supports_tools = sampling_supports_tools
        self.elicitation_handler = elicitation_handler
        self.elicitation_modes = tuple(dict.fromkeys(elicitation_modes))
        self.notification_handler = notification_handler
        self.server_request_handler = server_request_handler
        self.protocol_version = ""
        self.server_capabilities: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}
        self.server_instructions = ""
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._server_tasks: set[asyncio.Task[None]] = set()
        self._incoming_requests: dict[str | int, asyncio.Task[None]] = {}
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = http_client
        self._owns_http = http_client is None
        self._http_session_id = ""
        self._oauth = oauth_session
        if self._oauth is None and config.auth == "oauth":
            self._oauth = MCPOAuthSession(
                config.name,
                config.resolved_url,
                oauth_config=config.resolved_oauth,
            )
        self._initialized = False

    @property
    def client_capabilities(self) -> dict[str, Any]:
        capabilities: dict[str, Any] = {}
        if self.roots:
            capabilities["roots"] = {"listChanged": False}
        if self.sampling_handler is not None:
            sampling: dict[str, Any] = {}
            if self.sampling_supports_tools:
                sampling["tools"] = {}
            capabilities["sampling"] = sampling
        if self.elicitation_handler is not None and self.elicitation_modes:
            capabilities["elicitation"] = {mode: {} for mode in self.elicitation_modes}
        return capabilities

    def supports_server_capability(self, name: str) -> bool:
        return name in self.server_capabilities

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
            if self._oauth is not None and self._oauth.http_client is None:
                self._oauth.http_client = self._http
        else:
            raise MCPProtocolError(
                f"Unsupported MCP transport: {self.config.transport}"
            )
        result = await self.request(
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": self.client_capabilities,
                "clientInfo": {
                    "name": "ash",
                    "title": "Ash",
                    "version": _client_version(),
                    "description": "Extensible local AI agent harness",
                },
            },
        )
        selected = str(result.get("protocolVersion", ""))
        if selected not in SUPPORTED_PROTOCOL_VERSIONS:
            await self.disconnect()
            raise MCPProtocolError(
                f"MCP server selected unsupported protocol version {selected!r}"
            )
        capabilities = result.get("capabilities", {})
        if not isinstance(capabilities, dict):
            await self.disconnect()
            raise MCPProtocolError("MCP server capabilities must be an object")
        self.protocol_version = selected
        self.server_capabilities = capabilities
        self.server_info = (
            dict(result["serverInfo"])
            if isinstance(result.get("serverInfo"), dict)
            else {}
        )
        instructions = result.get("instructions", "")
        self.server_instructions = instructions if isinstance(instructions, str) else ""
        await self.notify("notifications/initialized", {})
        self._initialized = True

    async def _connect_stdio(self) -> None:
        env = build_scrubbed_environment(overrides=self.config.resolved_env)
        self._process = await asyncio.create_subprocess_exec(
            self.config.resolved_command,
            *self.config.resolved_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.config.resolved_cwd,
            limit=MAX_STDIO_MESSAGE_BYTES + 1,
            **process_group_options(),
        )
        self._reader_task = asyncio.create_task(self._read_stdio(self._process))
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process))

    async def _read_stdio(
        self,
        process: asyncio.subprocess.Process | None = None,
    ) -> None:
        process = process or self._process
        assert process is not None and process.stdout is not None
        error: MCPProtocolError | None = None
        while True:
            try:
                line = await process.stdout.readline()
            except (ValueError, OSError) as exc:
                error = MCPProtocolError(
                    f"MCP server {self.config.name!r} sent invalid stdio framing: {exc}"
                )
                break
            if not line:
                error = MCPProtocolError(
                    f"MCP server {self.config.name!r} closed its stdout"
                )
                break
            if len(line) > MAX_STDIO_MESSAGE_BYTES:
                error = MCPProtocolError(
                    f"MCP server {self.config.name!r} message exceeded "
                    f"{MAX_STDIO_MESSAGE_BYTES} bytes"
                )
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self._dispatch_incoming(message)
        assert error is not None
        self._fail_pending(error)
        self._initialized = False
        if self._process is process:
            self._process = None
        await terminate_process_tree(process, grace_seconds=0.1)

    def _fail_pending(self, error: MCPProtocolError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _dispatch_incoming(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if "method" not in message and isinstance(request_id, int):
            future = self._pending.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        task = asyncio.create_task(self._handle_incoming(message))
        self._server_tasks.add(task)
        incoming_id = message.get("id") if "method" in message else None
        if isinstance(incoming_id, (str, int)) and not isinstance(incoming_id, bool):
            self._incoming_requests[incoming_id] = task

        def finish_server_task(finished: asyncio.Task[None]) -> None:
            self._finish_server_task(finished, incoming_id)

        task.add_done_callback(finish_server_task)

    def _finish_server_task(
        self,
        task: asyncio.Task[None],
        request_id: Any = None,
    ) -> None:
        self._server_tasks.discard(task)
        if self._incoming_requests.get(request_id) is task:
            self._incoming_requests.pop(request_id, None)
        if not task.cancelled():
            task.exception()

    async def _handle_incoming(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params", {})
        if not isinstance(params, dict):
            params = {}
        if "id" not in message:
            if method == "notifications/cancelled":
                cancelled_id = params.get("requestId")
                if isinstance(cancelled_id, (str, int)) and not isinstance(
                    cancelled_id, bool
                ):
                    pending = self._incoming_requests.get(cancelled_id)
                    if pending is not None:
                        pending.cancel()
            if self.notification_handler is not None:
                try:
                    notification_result = self.notification_handler(method, params)
                    if inspect.isawaitable(notification_result):
                        await notification_result
                except Exception:
                    return
            return

        request_id = message["id"]
        try:
            result = await self._handle_server_request(method, params)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except asyncio.CancelledError:
            return
        except MCPProtocolError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": str(exc)},
            }
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "MCP client request failed"},
            }
        await self._send_message(response)

    async def _handle_server_request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "ping":
            return {}
        if method == "roots/list" and self.roots:
            return {
                "roots": [
                    {"uri": root.as_uri(), "name": root.name or str(root)}
                    for root in self.roots
                ]
            }
        if method == "sampling/createMessage" and self.sampling_handler is not None:
            return await self.sampling_handler(params)
        if method == "elicitation/create" and self.elicitation_handler is not None:
            return await self.elicitation_handler(params)
        if self.server_request_handler is not None:
            return await self.server_request_handler(method, params)
        raise MCPProtocolError(f"Unsupported MCP client method: {method}")

    async def _drain_stderr(
        self,
        process: asyncio.subprocess.Process | None = None,
    ) -> None:
        process = process or self._process
        assert process is not None and process.stderr is not None
        while await process.stderr.read(4096):
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
        try:
            if self.config.transport == "stdio":
                response = await self._request_stdio(request_id, payload)
            else:
                response = await self._request_http(request_id, payload)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            if method != "initialize":
                await self._cancel_request(request_id, f"{method} timed out")
            raise
        except asyncio.CancelledError:
            if method != "initialize":
                await asyncio.shield(
                    self._cancel_request(request_id, f"{method} was cancelled")
                )
            raise
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict):
                raise MCPProtocolError(f"{method} failed with an invalid error")
            code = error.get("code")
            message = error.get("message")
            error_data = {"data": error["data"]} if "data" in error else {}
            if isinstance(code, bool) or not isinstance(code, int):
                raise MCPProtocolError(
                    f"{method} failed with an invalid error code",
                    **error_data,
                )
            if not isinstance(message, str):
                raise MCPProtocolError(
                    f"{method} failed with an invalid error message",
                    code=code,
                    **error_data,
                )
            raise MCPProtocolError(
                f"{method} failed ({code}): {message}",
                code=code,
                **error_data,
            )
        result = response.get("result", {})
        return result if isinstance(result, dict) else {"value": result}

    async def _request_stdio(
        self, request_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None:
            raise MCPProtocolError("MCP stdio client is not connected")
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_message(payload)
            return await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _request_http(
        self, request_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response_http = await self._post_http(payload)
        matching: dict[str, Any] | None = None
        for message in _parse_http_messages(response_http):
            if message.get("id") == request_id and "method" not in message:
                matching = message
            else:
                await self._handle_incoming(message)
        if matching is None:
            raise MCPProtocolError(f"MCP HTTP response omitted request id {request_id}")
        return matching

    async def _cancel_request(self, request_id: int, reason: str) -> None:
        try:
            await self.notify(
                "notifications/cancelled",
                {"requestId": request_id, "reason": reason},
            )
        except (MCPProtocolError, httpx.HTTPError, OSError):
            return

    async def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        await self._send_message(
            {"jsonrpc": "2.0", "method": method, "params": params or {}}
        )

    async def _send_message(self, payload: dict[str, Any]) -> None:
        if self.config.transport == "stdio":
            if self._process is None or self._process.stdin is None:
                raise MCPProtocolError("MCP stdio client is not connected")
            async with self._write_lock:
                self._process.stdin.write(
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode()
                )
                await self._process.stdin.drain()
            return
        response = await self._post_http(payload)
        if response.status_code == 202 or not response.content:
            return
        for message in _parse_http_messages(response):
            await self._handle_incoming(message)

    async def _post_http(self, payload: dict[str, Any]) -> httpx.Response:
        if self._http is None:
            raise MCPProtocolError("MCP HTTP client is not connected")
        headers = {
            **self.config.resolved_headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._http_session_id:
            headers["Mcp-Session-Id"] = self._http_session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self._oauth is not None:
            headers["Authorization"] = await self._oauth.authorization_header()
        response = await self._http.post(
            self.config.resolved_url,
            json=payload,
            headers=headers,
        )
        if response.status_code == 401 and self._oauth is not None:
            rejected_access_token = headers["Authorization"].removeprefix("Bearer ")
            headers["Authorization"] = await self._oauth.authorization_header(
                force_refresh=True,
                rejected_access_token=rejected_access_token,
            )
            response = await self._http.post(
                self.config.resolved_url,
                json=payload,
                headers=headers,
            )
            if response.status_code == 401:
                raise MCPAuthorizationRequired(
                    f"MCP server {self.config.name!r} rejected refreshed OAuth "
                    f"credentials; run `ash mcp login {self.config.name}`"
                )
        if response.status_code == 403 and self._oauth is not None:
            challenge = bearer_challenge_parameters(
                response.headers.get("www-authenticate", "")
            )
            if challenge.get("error", "").casefold() == "insufficient_scope":
                try:
                    required_scope = normalize_oauth_scope(
                        challenge.get("scope", ""), "server-required OAuth scope"
                    )
                except MCPOAuthError:
                    required_scope = ""
                guidance = (
                    f"; required scopes: {required_scope}" if required_scope else ""
                )
                raise MCPAuthorizationRequired(
                    f"MCP server {self.config.name!r} requires additional OAuth "
                    f"scope{guidance}; rerun `ash mcp login {self.config.name}` "
                    "with --scope set to the server-required scopes"
                )
        response.raise_for_status()
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._http_session_id = session_id
        return response

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._list_paginated("tools/list", "tools")

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
        return await self._list_paginated("resources/list", "resources")

    async def list_resource_templates(self) -> list[dict[str, Any]]:
        return await self._list_paginated(
            "resources/templates/list", "resourceTemplates"
        )

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return await self.request("resources/read", {"uri": uri})

    async def list_prompts(self) -> list[dict[str, Any]]:
        return await self._list_paginated("prompts/list", "prompts")

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return await self.request(
            "prompts/get", {"name": name, "arguments": arguments or {}}
        )

    async def _list_paginated(self, method: str, key: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGINATION_PAGES):
            result = await self.request(
                method, {"cursor": cursor} if cursor is not None else {}
            )
            values = result.get(key, [])
            if not isinstance(values, list):
                raise MCPProtocolError(f"{method} returned non-list {key}")
            output.extend(item for item in values if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return output
            if not isinstance(next_cursor, str) or not next_cursor:
                raise MCPProtocolError(f"{method} returned an invalid nextCursor")
            if next_cursor in seen:
                raise MCPProtocolError(f"{method} repeated pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise MCPProtocolError(f"{method} exceeded {MAX_PAGINATION_PAGES} pages")

    async def notify_roots_changed(self) -> None:
        if self.roots:
            await self.notify("notifications/roots/list_changed")

    async def disconnect(self) -> None:
        self._initialized = False
        self._fail_pending(
            MCPProtocolError(f"MCP server {self.config.name!r} disconnected")
        )
        if self._http is not None and self._http_session_id:
            try:
                headers = {**self.config.resolved_headers}
                if self._oauth is not None:
                    headers["Authorization"] = await self._oauth.authorization_header()
                headers["Mcp-Session-Id"] = self._http_session_id
                if self.protocol_version:
                    headers["MCP-Protocol-Version"] = self.protocol_version
                response = await self._http.delete(
                    self.config.resolved_url,
                    headers=headers,
                )
                response.raise_for_status()
            except (httpx.HTTPError, MCPOAuthError):
                pass
            self._http_session_id = ""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None
        if self._process is not None:
            await terminate_process_tree(self._process)
            self._process = None
        for task in (self._reader_task, self._stderr_task, *self._server_tasks):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (self._reader_task, self._stderr_task, *self._server_tasks)
                if task
            ),
            return_exceptions=True,
        )
        self._reader_task = None
        self._stderr_task = None
        self._server_tasks.clear()
        self._incoming_requests.clear()
        self.protocol_version = ""
        self.server_capabilities = {}
        self.server_info = {}
        self.server_instructions = ""


def _parse_http_messages(response: httpx.Response) -> list[dict[str, Any]]:
    content_type = response.headers.get("content-type", "").casefold()
    if "text/event-stream" not in content_type:
        if not response.content:
            return []
        payload = response.json()
        if isinstance(payload, dict):
            return [payload]
        if isinstance(payload, list) and all(
            isinstance(item, dict) for item in payload
        ):
            return payload
        raise MCPProtocolError("MCP HTTP response must contain JSON-RPC objects")

    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in [*response.text.splitlines(), ""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line or not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            data_lines.clear()
            continue
        data_lines.clear()
        if isinstance(payload, dict):
            messages.append(payload)
    return messages


def _client_version() -> str:
    try:
        return version("ash-ai")
    except PackageNotFoundError:
        return "0.1.0"
