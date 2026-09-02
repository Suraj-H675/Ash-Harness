"""Async Model Context Protocol client with stdio and HTTP transports."""

from __future__ import annotations

import asyncio
import inspect
import json
import base64
from collections.abc import AsyncIterator
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urljoin, urlparse
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
MODERN_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {LATEST_PROTOCOL_VERSION, "2025-06-18", "2025-03-26", "2024-11-05"}
)
MODERN_HEADER_MISMATCH_ERROR = -32020
MODERN_MISSING_CAPABILITY_ERROR = -32021
MODERN_UNSUPPORTED_VERSION_ERROR = -32022
MAX_PAGINATION_PAGES = 100
MAX_PAGINATION_SESSION_RESTARTS = 1
MAX_STDIO_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_OUTBOUND_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_LEGACY_SSE_EVENT_BYTES = 8 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_HTTP_SSE_EVENT_BYTES = 8 * 1024 * 1024
MAX_BUFFERED_LEGACY_SSE_RESPONSES = 1000
SAFE_INTEGER_BOUND = 2**53 - 1
MAX_HTTP_SESSION_ID_BYTES = 1024
MIN_TASK_POLL_INTERVAL_SECONDS = 0.01
MAX_TASK_POLL_INTERVAL_SECONDS = 30.0
TASK_STATUS_NOTIFICATION = "notifications/tasks/status"
TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
TASK_STATUSES = TASK_TERMINAL_STATUSES | {"working", "input_required"}

RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
SessionReinitializedHandler = Callable[
    [int, str, dict[str, Any]], Awaitable[bool] | bool
]
ToolContractValidator = Callable[[str, str, int], bool]
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


class MCPSessionExpired(MCPProtocolError):
    """A session-bound HTTP POST was rejected because its session expired."""

    def __init__(self, session_id: str, generation: int) -> None:
        super().__init__("MCP Streamable HTTP session expired with 404 Not Found")
        self.session_id = session_id
        self.generation = generation


class _EndpointDiscovered(Exception):
    def __init__(self, endpoint: str) -> None:
        super().__init__("legacy MCP SSE endpoint discovered")
        self.endpoint = endpoint


class MCPTaskTimeout(MCPProtocolError):
    """A task-augmented operation exceeded the configured task wait limit."""


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
        session_reinitialized_handler: SessionReinitializedHandler | None = None,
        tool_contract_validator: ToolContractValidator | None = None,
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
        self.session_reinitialized_handler = session_reinitialized_handler
        self.tool_contract_validator = tool_contract_validator
        self.protocol_version = ""
        self.server_capabilities: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}
        self.server_instructions = ""
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._server_tasks: set[asyncio.Task[None]] = set()
        self._incoming_requests: dict[str | int, asyncio.Task[None]] = {}
        self._task_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._probe_id = 0
        self._write_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = http_client
        self._owns_http = http_client is None
        self._http_session_id = ""
        self._legacy_sse_endpoint = ""
        self._legacy_sse_discovery: asyncio.Future[None] | None = None
        self._legacy_sse_responses: dict[int, dict[str, Any]] = {}
        self._sse_task: asyncio.Task[None] | None = None
        self._sse_generation = 0
        self._sse_last_event_id = ""
        self._sse_retry_ms = 1000
        self._sse_supported = True
        self._pending_initialize_session_id = ""
        self._session_generation = 0
        self._session_recovery_lock = asyncio.Lock()
        self._session_ready = asyncio.Event()
        self._session_ready.set()
        self._connect_lock = asyncio.Lock()
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
        capabilities["tasks"] = {"cancel": {}, "list": {}}
        return capabilities

    def supports_server_capability(self, name: str) -> bool:
        return isinstance(self.server_capabilities.get(name), dict)

    @property
    def session_generation(self) -> int:
        return self._session_generation

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._initialized:
                return
            if self.config.transport == "stdio":
                await self._connect_stdio()
            elif self.config.transport in {"http", "sse"}:
                if not self.config.resolved_url:
                    raise MCPProtocolError(
                        f"MCP server {self.config.name!r} has no URL"
                    )
                if self._http is None:
                    self._http = httpx.AsyncClient(timeout=self.timeout)
                if self._oauth is not None and self._oauth.http_client is None:
                    self._oauth.http_client = self._http
                if self.config.transport == "sse":
                    self._legacy_sse_discovery = (
                        asyncio.get_running_loop().create_future()
                    )
                    self._sse_task = asyncio.create_task(
                        self._read_legacy_sse_events(self._sse_generation + 1)
                    )
                    await asyncio.shield(self._legacy_sse_discovery)
            else:
                raise MCPProtocolError(
                    f"Unsupported MCP transport: {self.config.transport}"
                )
            try:
                if self.config.transport == "stdio":
                    await self._probe_modern_stdio()
                elif self.config.transport == "http":
                    await self._probe_modern_http()
                await self._initialize_protocol()
            except BaseException:
                await asyncio.shield(self.disconnect())
                raise

    async def _initialize_protocol(self) -> None:
        self._pending_initialize_session_id = ""
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
            _allow_session_recovery=False,
        )
        selected = str(result.get("protocolVersion", ""))
        if selected not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPProtocolError(
                f"MCP server selected unsupported protocol version {selected!r}"
            )
        capabilities = result.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise MCPProtocolError("MCP server capabilities must be an object")
        malformed_capabilities = [
            name for name, value in capabilities.items() if not isinstance(value, dict)
        ]
        if malformed_capabilities:
            raise MCPProtocolError(
                "MCP server capabilities must contain objects: "
                + ", ".join(sorted(malformed_capabilities))
            )
        self.protocol_version = selected
        self.server_capabilities = capabilities
        self.server_info = (
            dict(result["serverInfo"])
            if isinstance(result.get("serverInfo"), dict)
            else {}
        )
        instructions = result.get("instructions", "")
        self.server_instructions = instructions if isinstance(instructions, str) else ""
        self._http_session_id = self._pending_initialize_session_id
        self._pending_initialize_session_id = ""
        self._session_generation += 1
        await self.notify(
            "notifications/initialized", {}, _allow_session_recovery=False
        )
        self._initialized = True
        if (
            self.config.transport in {"http", "sse"}
            and selected != LATEST_PROTOCOL_VERSION
            and self._sse_supported
            and (self._sse_task is None or self._sse_task.done())
        ):
            self._sse_task = asyncio.create_task(self._read_http_events())

    def _modern_request_meta(self) -> dict[str, Any]:
        return {
            "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": {
                "name": "ash",
                "version": _client_version(),
            },
            "io.modelcontextprotocol/clientCapabilities": self.client_capabilities,
        }

    async def _probe_modern_stdio(self) -> None:
        try:
            result = await self.request(
                "server/discover",
                {"_meta": self._modern_request_meta()},
                _allow_session_recovery=False,
            )
        except MCPProtocolError as exc:
            data = exc.data if isinstance(exc.data, dict) else {}
            supported = data.get("supported")
            if exc.code == MODERN_UNSUPPORTED_VERSION_ERROR and isinstance(
                supported, list
            ):
                raise MCPProtocolError(
                    f"MCP server supports modern protocol versions {supported}, "
                    f"but Ash currently negotiates through {LATEST_PROTOCOL_VERSION}"
                ) from exc
            return
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return
        versions = result.get("supportedVersions")
        capabilities = result.get("capabilities")
        if not (
            result.get("resultType") == "complete"
            and isinstance(versions, list)
            and all(isinstance(version, str) for version in versions)
            and isinstance(capabilities, dict)
        ):
            raise MCPProtocolError("MCP server discovery returned an invalid result")
        if MODERN_PROTOCOL_VERSION in versions or any(
            version > LATEST_PROTOCOL_VERSION for version in versions
        ):
            raise MCPProtocolError(
                f"MCP server is modern-only; Ash currently negotiates through "
                f"{LATEST_PROTOCOL_VERSION}: {sorted(versions)}"
            )
        raise MCPProtocolError("MCP stdio discovery returned a legacy-era result")

    async def _probe_modern_http(self) -> None:
        try:
            await self._post_http(
                {
                    "jsonrpc": "2.0",
                    "id": self._probe_id,
                    "method": "ping",
                    "params": {"_meta": self._modern_request_meta()},
                },
                is_initialize=True,
                bypass_session_readiness=True,
                allow_oauth_refresh=False,
            )
            return
        except MCPAuthorizationRequired:
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            try:
                body = exc.response.json()
            except ValueError:
                return
            if not isinstance(body, dict):
                return
            error = body.get("error")
            if not isinstance(error, dict):
                return
            code = error.get("code")
            raw_data = error.get("data")
            data = raw_data if isinstance(raw_data, dict) else {}
            if code == MODERN_UNSUPPORTED_VERSION_ERROR and isinstance(
                data.get("supported"), list
            ):
                supported = [
                    str(version)
                    for version in data["supported"]
                    if isinstance(version, str)
                ]
                raise MCPProtocolError(
                    f"MCP server supports modern protocol versions {supported}, "
                    f"but Ash currently negotiates through {LATEST_PROTOCOL_VERSION}"
                ) from exc
            if code in {
                MODERN_HEADER_MISMATCH_ERROR,
                MODERN_MISSING_CAPABILITY_ERROR,
            }:
                raise MCPProtocolError(
                    "MCP server rejected the modern HTTP probe: "
                    + str(error.get("message", ""))
                ) from exc
            return
        except (asyncio.TimeoutError, httpx.TimeoutException, httpx.HTTPError):
            return

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
            except (UnicodeDecodeError, OverflowError, RecursionError) as exc:
                error = MCPProtocolError(
                    f"MCP server {self.config.name!r} sent invalid JSON: {exc}"
                )
                break
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
        for waiter in self._task_waiters.values():
            if not waiter.done():
                waiter.set_exception(error)
        self._task_waiters.clear()

    def _dispatch_incoming(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        try:
            _validate_jsonrpc_message(message)
        except MCPProtocolError as exc:
            if isinstance(request_id, int) and not isinstance(request_id, bool):
                future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_exception(exc)
            return
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
            if method == TASK_STATUS_NOTIFICATION:
                self._resolve_task_status_notification(params)
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
        *,
        _allow_session_recovery: bool = True,
        _expected_tool_contract: tuple[str, str] | None = None,
        _header_annotations: list[tuple[tuple[str, ...], str]] | None = None,
    ) -> dict[str, Any]:
        if self.config.transport != "stdio" and _allow_session_recovery:
            async with self._session_recovery_lock:
                if not self._initialized and self._session_generation:
                    await self._initialize_protocol()
                readiness = self._session_ready
            await readiness.wait()
        if (
            _expected_tool_contract is not None
            and self.tool_contract_validator is not None
            and not self.tool_contract_validator(
                _expected_tool_contract[0],
                _expected_tool_contract[1],
                self._session_generation,
            )
        ):
            raise MCPProtocolError(
                f"MCP tool {_expected_tool_contract[0]!r} no longer matches "
                "the active verified server contract"
            )
        request_id = 0
        recovery_attempted = False
        try:
            while True:
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
                        response = await self._request_stdio(
                            request_id,
                            payload,
                            expected_tool_contract=_expected_tool_contract,
                        )
                    else:
                        response = await self._request_http(
                            request_id,
                            payload,
                            expected_tool_contract=_expected_tool_contract,
                            header_annotations=_header_annotations,
                            bypass_session_readiness=not _allow_session_recovery,
                        )
                    break
                except MCPSessionExpired as exc:
                    if not _allow_session_recovery or method == "initialize":
                        raise
                    retry_allowed = await self._recover_http_session(
                        exc, method=method, params=params or {}
                    )
                    if method == "tools/call":
                        raise MCPProtocolError(
                            "tools/call was rejected after HTTP session recovery; "
                            "the operation was not replayed because a server-side "
                            "effect may have occurred"
                        ) from exc
                    if recovery_attempted:
                        raise MCPProtocolError(
                            f"{method} was rejected after one HTTP session recovery; "
                            "the operation was not attempted again"
                        ) from exc
                    if not retry_allowed:
                        raise MCPProtocolError(
                            f"{method} was not retried because the replacement "
                            "HTTP session changed the server contract"
                        ) from exc
                    recovery_attempted = True
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
        self,
        request_id: int,
        payload: dict[str, Any],
        *,
        expected_tool_contract: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None:
            raise MCPProtocolError("MCP stdio client is not connected")
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_message(
                payload, _expected_tool_contract=expected_tool_contract
            )
            return await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _request_http(
        self,
        request_id: int,
        payload: dict[str, Any],
        *,
        expected_tool_contract: tuple[str, str] | None = None,
        header_annotations: list[tuple[tuple[str, ...], str]] | None = None,
        bypass_session_readiness: bool = False,
    ) -> dict[str, Any]:
        if self.config.transport == "sse":
            return await self._request_legacy_sse(
                request_id,
                payload,
                header_annotations=header_annotations or [],
            )
        response_http = await self._post_http(
            payload,
            expected_tool_contract=expected_tool_contract,
            header_annotations=header_annotations or [],
            bypass_session_readiness=bypass_session_readiness,
        )
        matching: dict[str, Any] | None = None
        for message in _parse_http_messages(response_http):
            if message.get("id") == request_id and "method" not in message:
                if matching is not None:
                    raise MCPProtocolError(
                        f"MCP HTTP response repeated request id {request_id}"
                    )
                matching = message
            else:
                self._dispatch_incoming(message)
        if matching is None:
            raise MCPProtocolError(f"MCP HTTP response omitted request id {request_id}")
        return matching

    async def _request_legacy_sse(
        self,
        request_id: int,
        payload: dict[str, Any],
        *,
        header_annotations: list[tuple[tuple[str, ...], str]] | None = None,
    ) -> dict[str, Any]:
        endpoint = self._legacy_sse_endpoint
        if not endpoint:
            if self._legacy_sse_discovery is None:
                raise MCPProtocolError("MCP SSE client is not connected")
            await asyncio.shield(self._legacy_sse_discovery)
            endpoint = self._legacy_sse_endpoint
        if not endpoint:
            raise MCPProtocolError("MCP SSE server did not advertise a POST endpoint")
        buffered = self._legacy_sse_responses.pop(request_id, None)
        if buffered is not None:
            return buffered
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            if header_annotations:
                raise MCPProtocolError(
                    "MCP HTTP parameter headers require the http transport"
                )
            await self._post_legacy_sse(endpoint, payload)
            return await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _post_legacy_sse(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        header_annotations: list[tuple[tuple[str, ...], str]] | None = None,
    ) -> None:
        if self._http is None:
            raise MCPProtocolError("MCP HTTP client is not connected")
        encoded = _encode_outbound_message(payload)
        headers = httpx.Headers(self.config.resolved_headers)
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"
        if self._oauth is not None:
            headers["Authorization"] = await self._oauth.authorization_header()
        if header_annotations:
            raise MCPProtocolError(
                "MCP HTTP parameter headers require the http transport"
            )
        async with self._http.stream(
            "POST",
            endpoint,
            content=encoded,
            headers=headers,
        ) as response:
            response.raise_for_status()

    @staticmethod
    def _safe_header_value(value: str) -> str:
        if (
            value
            and all(
                0x20 <= ord(character) <= 0x7E or ord(character) == 0x09
                for character in value
            )
            and value == value.strip()
        ):
            return value
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"=?base64?{encoded}?="

    def _tool_request_headers(
        self,
        payload: dict[str, Any],
        annotations: list[tuple[tuple[str, ...], str]],
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        method = payload.get("method")
        if isinstance(method, str) and method:
            headers["Mcp-Method"] = self._safe_header_value(method)
        params = payload.get("params", {})
        if isinstance(params, dict):
            name = params.get("name")
            uri = params.get("uri")
            if isinstance(name, str):
                headers["Mcp-Name"] = self._safe_header_value(name)
            elif isinstance(uri, str):
                headers["Mcp-Name"] = self._safe_header_value(uri)
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            for path, annotation in annotations:
                value: Any = arguments
                valid = True
                for part in path:
                    if part == "properties":
                        continue
                    if not isinstance(value, dict) or part not in value:
                        valid = False
                        break
                    value = value[part]
                if not valid or value is None:
                    continue
                if isinstance(value, bool):
                    rendered = "true" if value else "false"
                elif isinstance(value, int) and abs(value) <= SAFE_INTEGER_BOUND:
                    rendered = str(value)
                elif isinstance(value, str):
                    rendered = value
                else:
                    raise MCPProtocolError(
                        f"MCP header parameter {path[-1]!r} must be a primitive value"
                    )
                headers[f"Mcp-Param-{annotation}"] = self._safe_header_value(rendered)
        return headers

    async def _cancel_request(self, request_id: int, reason: str) -> None:
        if self.config.transport != "stdio" and not self._initialized:
            return
        try:
            await self.notify(
                "notifications/cancelled",
                {"requestId": request_id, "reason": reason},
                _allow_session_recovery=False,
            )
        except (MCPProtocolError, httpx.HTTPError, OSError):
            return

    async def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        _allow_session_recovery: bool = True,
    ) -> None:
        await self._send_message(
            {"jsonrpc": "2.0", "method": method, "params": params or {}},
            _allow_session_recovery=_allow_session_recovery,
        )

    async def _send_message(
        self,
        payload: dict[str, Any],
        *,
        _allow_session_recovery: bool = True,
        _expected_tool_contract: tuple[str, str] | None = None,
        _header_annotations: list[tuple[tuple[str, ...], str]] | None = None,
    ) -> None:
        encoded = _encode_outbound_message(payload)
        if self.config.transport == "stdio":
            if self._process is None or self._process.stdin is None:
                raise MCPProtocolError("MCP stdio client is not connected")
            async with self._write_lock:
                if (
                    _expected_tool_contract is not None
                    and self.tool_contract_validator is not None
                    and not self.tool_contract_validator(
                        _expected_tool_contract[0],
                        _expected_tool_contract[1],
                        self._session_generation,
                    )
                ):
                    raise MCPProtocolError(
                        f"MCP tool {_expected_tool_contract[0]!r} no longer matches "
                        "the active verified server contract"
                    )
                self._process.stdin.write(
                    encoded + b"\n"
                )
                await self._process.stdin.drain()
            return
        if self.config.transport == "sse":
            endpoint = self._legacy_sse_endpoint
            if not endpoint:
                if self._legacy_sse_discovery is None:
                    raise MCPProtocolError("MCP SSE client is not connected")
                await asyncio.shield(self._legacy_sse_discovery)
                endpoint = self._legacy_sse_endpoint
            if endpoint:
                await self._post_legacy_sse(
                    endpoint,
                    payload,
                    header_annotations=_header_annotations or [],
                )
            return
        if _allow_session_recovery:
            async with self._session_recovery_lock:
                if not self._initialized and self._session_generation:
                    await self._initialize_protocol()
                readiness = self._session_ready
            await readiness.wait()
        try:
            response = await self._post_http(
                payload,
                header_annotations=_header_annotations or [],
                bypass_session_readiness=not _allow_session_recovery,
            )
        except MCPSessionExpired as exc:
            if not _allow_session_recovery:
                raise
            retry_allowed = await self._recover_http_session(
                exc,
                method=str(payload.get("method", "")),
                params=(
                    payload["params"] if isinstance(payload.get("params"), dict) else {}
                ),
            )
            method = payload.get("method")
            if (
                not retry_allowed
                or not isinstance(method, str)
                or method == "tools/call"
                or method == "notifications/cancelled"
            ):
                return
            try:
                response = await self._post_http(payload)
            except MCPSessionExpired as retry_exc:
                await self._recover_http_session(
                    retry_exc,
                    method=method,
                    params=(
                        payload["params"]
                        if isinstance(payload.get("params"), dict)
                        else {}
                    ),
                )
                raise MCPProtocolError(
                    f"{method} was rejected after one HTTP session recovery"
                ) from retry_exc
        if response.status_code == 202 or not response.content:
            return
        for message in _parse_http_messages(response):
            self._dispatch_incoming(message)

    async def _post_http(
        self,
        payload: dict[str, Any],
        *,
        is_initialize: bool = False,
        expected_tool_contract: tuple[str, str] | None = None,
        header_annotations: list[tuple[tuple[str, ...], str]] | None = None,
        bypass_session_readiness: bool = False,
        allow_oauth_refresh: bool = True,
    ) -> httpx.Response:
        if self._http is None:
            raise MCPProtocolError("MCP HTTP client is not connected")
        encoded = _encode_outbound_message(payload)
        headers = httpx.Headers(self.config.resolved_headers)
        headers["Accept"] = "application/json, text/event-stream"
        headers["Content-Type"] = "application/json"
        if not is_initialize:
            is_initialize = payload.get("method") == "initialize"
        while True:
            ready = self._session_ready.is_set()
            sent_session_id = "" if is_initialize else self._http_session_id
            sent_generation = self._session_generation
            sent_protocol_version = self.protocol_version
            if is_initialize or bypass_session_readiness or ready:
                break
            await self._session_ready.wait()
        if (
            expected_tool_contract is not None
            and self.tool_contract_validator is not None
            and not self.tool_contract_validator(
                expected_tool_contract[0],
                expected_tool_contract[1],
                sent_generation,
            )
        ):
            raise MCPProtocolError(
                f"MCP tool {expected_tool_contract[0]!r} no longer matches "
                "the active verified server contract"
            )
        if sent_session_id:
            headers["Mcp-Session-Id"] = sent_session_id
        else:
            headers.pop("Mcp-Session-Id", None)
        if sent_protocol_version and not is_initialize:
            headers["MCP-Protocol-Version"] = sent_protocol_version
        else:
            headers.pop("MCP-Protocol-Version", None)
        if self._oauth is not None:
            headers["Authorization"] = await self._oauth.authorization_header()
        if header_annotations:
            headers.update(
                self._tool_request_headers(
                    payload,
                    header_annotations,
                )
            )
        response = await self._post_http_request(
            self.config.resolved_url,
            content=encoded,
            headers=headers,
        )
        if (
            response.status_code == 401
            and self._oauth is not None
            and not allow_oauth_refresh
        ):
            raise MCPAuthorizationRequired(
                f"MCP server {self.config.name!r} rejected the compatibility "
                "probe credentials; falling back to legacy initialization"
            )
        if response.status_code == 401 and self._oauth is not None:
            if payload.get("method") == "tools/call":
                raise MCPAuthorizationRequired(
                    f"MCP server {self.config.name!r} rejected the tool-call "
                    "credentials; the operation was not replayed because a "
                    "server-side effect may have occurred"
                )
            rejected_access_token = headers["Authorization"].removeprefix("Bearer ")
            headers["Authorization"] = await self._oauth.authorization_header(
                force_refresh=True,
                rejected_access_token=rejected_access_token,
            )
            response = await self._post_http_request(
                self.config.resolved_url,
                content=encoded,
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
        if response.status_code == 404 and sent_session_id:
            raise MCPSessionExpired(sent_session_id, sent_generation)
        response.raise_for_status()
        if is_initialize:
            session_id = response.headers.get("Mcp-Session-Id", "")
            if session_id:
                _validate_http_session_id(session_id)
                self._pending_initialize_session_id = session_id
        return response

    async def _post_http_request(
        self,
        url: str,
        **request_kwargs: Any,
    ) -> httpx.Response:
        """Send a POST while bounding any response retained in memory."""

        if self._http is None:
            raise MCPProtocolError("MCP HTTP client is not connected")
        async with self._http.stream("POST", url, **request_kwargs) as response:
            # Error responses other than a 400 compatibility probe, and
            # notification acknowledgements, are only inspected for status and
            # headers. Do not wait for an attacker-controlled response body.
            if (
                response.status_code in {202, 204}
                or response.status_code >= 400
                and response.status_code != 400
            ):
                return _copy_http_response(response, b"")
            return await _read_bounded_http_response(response)

    async def _recover_http_session(
        self,
        expired: MCPSessionExpired,
        *,
        method: str = "",
        params: dict[str, Any] | None = None,
    ) -> bool:
        async with self._session_recovery_lock:
            if self._initialized and self._session_generation != expired.generation:
                generation = self._session_generation
            else:
                self._initialized = False
                self._session_ready.clear()
                self._http_session_id = ""
                self._pending_initialize_session_id = ""
                self.protocol_version = ""
                self.server_capabilities = {}
                self.server_info = {}
                self.server_instructions = ""
                self._stop_http_events()
                try:
                    await self._initialize_protocol()
                except BaseException:
                    session_to_close = (
                        self._http_session_id or self._pending_initialize_session_id
                    )
                    self._http_session_id = ""
                    self._pending_initialize_session_id = ""
                    try:
                        if session_to_close:
                            await asyncio.shield(
                                self._delete_http_session(session_to_close)
                            )
                    finally:
                        self._session_ready.set()
                    raise
                if (
                    self.config.transport in {"http", "sse"}
                    and self.protocol_version != LATEST_PROTOCOL_VERSION
                    and self._sse_supported
                ):
                    self._sse_generation += 1
                    self._sse_task = asyncio.create_task(self._read_http_events())
                generation = self._session_generation
        try:
            if self.session_reinitialized_handler is None:
                return True
            result = self.session_reinitialized_handler(
                generation, method, dict(params or {})
            )
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        finally:
            self._session_ready.set()

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._list_paginated("tools/list", "tools")

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        expected_contract: str | None = None,
        as_task: bool = False,
        task_ttl_ms: int | None = None,
        task_timeout: float | None = None,
        header_annotations: list[tuple[tuple[str, ...], str]] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if not as_task:
            return await self.request(
                "tools/call",
                params,
                _expected_tool_contract=(name, expected_contract)
                if expected_contract is not None
                else None,
                _header_annotations=header_annotations or [],
            )
        if task_ttl_ms is not None and (
            isinstance(task_ttl_ms, bool) or task_ttl_ms <= 0
        ):
            raise ValueError("MCP task TTL must be a positive number of milliseconds")
        wait_timeout = task_timeout if task_timeout is not None else self.timeout
        if isinstance(wait_timeout, bool) or wait_timeout <= 0:
            raise ValueError("MCP task timeout must be positive")
        tasks_capability = self.server_capabilities.get("tasks")
        task_requests = (
            tasks_capability.get("requests")
            if isinstance(tasks_capability, dict)
            else None
        )
        tools_task_requests = (
            task_requests.get("tools") if isinstance(task_requests, dict) else None
        )
        if (
            not isinstance(tools_task_requests, dict)
            or "call" not in tools_task_requests
        ):
            raise MCPProtocolError(
                f"MCP server does not advertise support for task-augmented "
                f"tool calls required by {name!r}"
            )
        metadata: dict[str, Any] = {}
        if task_ttl_ms is not None:
            metadata["ttl"] = task_ttl_ms
        params["task"] = metadata
        expected = (name, expected_contract) if expected_contract is not None else None
        created = self._validate_task_result(
            await self.request(
                "tools/call",
                params,
                _expected_tool_contract=expected,
                _allow_session_recovery=False,
                _header_annotations=header_annotations or [],
            ),
            method="tools/call",
        )
        task = created["task"]
        task_id = task["taskId"]
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._task_waiters[task_id] = waiter
        try:
            deadline = loop.time() + wait_timeout
            status = task["status"]
            while status == "working" or status == "input_required":
                delay = self._task_poll_delay(task)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError

                notified: dict[str, Any] | None = None
                try:
                    notified = await asyncio.wait_for(
                        asyncio.shield(waiter), min(delay, remaining)
                    )
                except asyncio.TimeoutError:
                    pass
                if notified is not None:
                    status = notified["status"]
                    task = notified
                    waiter = loop.create_future()
                    self._task_waiters[task_id] = waiter
                    continue

                if status == "input_required":
                    await self.request(
                        "tasks/result",
                        {"taskId": task_id},
                        _allow_session_recovery=False,
                    )
                    state = self._validate_task_result(
                        await self.request(
                            "tasks/get",
                            {"taskId": task_id},
                            _allow_session_recovery=False,
                        ),
                        method="tasks/get",
                    )
                    task = state["task"]
                    if task["taskId"] != task_id:
                        raise MCPProtocolError("MCP tasks/get returned another taskId")
                    status = task["status"]
                    continue

                state = self._validate_task_result(
                    await self.request(
                        "tasks/get",
                        {"taskId": task_id},
                        _allow_session_recovery=False,
                    ),
                    method="tasks/get",
                )
                state = state["task"]
                if state["taskId"] != task_id:
                    raise MCPProtocolError("MCP tasks/get returned another taskId")
                status = state["status"]
                task = state
            if status == "completed":
                result = self._validate_task_result(
                    await self.request(
                        "tasks/result",
                        {"taskId": task_id},
                        _allow_session_recovery=False,
                    ),
                    method="tasks/result",
                    require_task=False,
                )
                if "content" in result or "error" in result or "_meta" in result:
                    return result
                raise MCPProtocolError(
                    "MCP tasks/result returned an invalid tool result without content"
                )
            detail = f": {task['statusMessage']}" if task.get("statusMessage") else ""
            raise MCPProtocolError(f"MCP tool task {status}{detail}")
        except asyncio.TimeoutError as exc:
            await self._cancel_mcp_task(task_id)
            raise MCPTaskTimeout(
                f"MCP tool task timed out after {wait_timeout} seconds"
            ) from exc
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_mcp_task(task_id))
            raise
        finally:
            if self._task_waiters.get(task_id) is waiter:
                self._task_waiters.pop(task_id, None)

    async def _cancel_mcp_task(self, task_id: str) -> None:
        try:
            await self.request(
                "tasks/cancel", {"taskId": task_id}, _allow_session_recovery=False
            )
        except (MCPProtocolError, httpx.HTTPError, OSError):
            return

    async def cancel_mcp_task(self, task_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("MCP taskId is required")
        if not self._supports_tasks_cancel():
            raise MCPProtocolError(
                "MCP server does not advertise support for tasks/cancel"
            )
        result = self._validate_task_result(
            await self.request(
                "tasks/cancel",
                {"taskId": task_id},
                _allow_session_recovery=False,
            ),
            method="tasks/cancel",
        )
        cancelled = result["task"]
        if cancelled["taskId"] != task_id:
            raise MCPProtocolError("MCP tasks/cancel returned another taskId")
        return cancelled

    async def list_mcp_tasks(self) -> list[dict[str, Any]]:
        if not self._supports_tasks_list():
            raise MCPProtocolError(
                "MCP server does not advertise support for tasks/list"
            )
        output: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGINATION_PAGES):
            result = await self.request(
                "tasks/list",
                {"cursor": cursor} if cursor is not None else {},
                _allow_session_recovery=False,
            )
            if not isinstance(result, dict) or "tasks" not in result:
                raise MCPProtocolError("MCP tasks/list returned no valid task list")
            tasks = result["tasks"]
            if not isinstance(tasks, list) or not all(
                isinstance(task, dict) for task in tasks
            ):
                raise MCPProtocolError("MCP tasks/list returned an invalid task entry")
            output.extend(
                self._validate_task_result({"task": task}, method="tasks/list")["task"]
                for task in tasks
            )
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return output
            if not isinstance(next_cursor, str) or not next_cursor:
                raise MCPProtocolError("MCP tasks/list returned an invalid nextCursor")
            if next_cursor in seen:
                raise MCPProtocolError("MCP tasks/list repeated pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise MCPProtocolError(f"MCP tasks/list exceeded {MAX_PAGINATION_PAGES} pages")

    def _supports_tasks_list(self) -> bool:
        capability = self.server_capabilities.get("tasks")
        return isinstance(capability, dict) and isinstance(capability.get("list"), dict)

    def _supports_tasks_cancel(self) -> bool:
        capability = self.server_capabilities.get("tasks")
        return isinstance(capability, dict) and isinstance(
            capability.get("cancel"), dict
        )

    def _resolve_task_status_notification(self, params: Any) -> None:
        task_id = params.get("taskId") if isinstance(params, dict) else None
        if not isinstance(task_id, str):
            return
        waiter = self._task_waiters.get(task_id)
        if waiter is None or waiter.done():
            return
        try:
            task = self._validate_task_state(
                params, method="notifications/tasks/status"
            )
        except MCPProtocolError:
            return
        if task["taskId"] != task_id:
            return
        waiter.set_result(task)

    @staticmethod
    def _validate_task_result(
        result: dict[str, Any],
        *,
        method: str,
        require_task: bool = True,
    ) -> dict[str, Any]:
        task = result.get("task")
        if not isinstance(task, dict):
            if require_task:
                raise MCPProtocolError(f"{method} returned no valid task")
            return result
        validated = MCPClient._validate_task_state(task, method=method)
        return {**result, "task": validated}

    @staticmethod
    def _validate_task_state(task: Any, *, method: str) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise MCPProtocolError(f"{method} returned an invalid task")
        task_id = task.get("taskId")
        status = task.get("status")
        if not isinstance(task_id, str) or not task_id:
            raise MCPProtocolError(f"{method} returned an invalid taskId")
        if status not in TASK_STATUSES:
            raise MCPProtocolError(f"{method} returned invalid task status {status!r}")
        poll_interval = task.get("pollInterval")
        if poll_interval is not None and (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, int)
            or poll_interval < 0
        ):
            raise MCPProtocolError(f"{method} returned an invalid pollInterval")
        return task

    @staticmethod
    def _task_poll_delay(task: dict[str, Any]) -> float:
        interval = task.get("pollInterval")
        if interval is None:
            return 1.0
        return min(
            max(interval / 1000.0, MIN_TASK_POLL_INTERVAL_SECONDS),
            MAX_TASK_POLL_INTERVAL_SECONDS,
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
        capability = "resources" if method.startswith("resources/") else key
        for restart in range(MAX_PAGINATION_SESSION_RESTARTS + 1):
            if self.config.transport != "stdio":
                async with self._session_recovery_lock:
                    pass
            if restart and not self.supports_server_capability(capability):
                return []
            output: list[dict[str, Any]] = []
            cursor: str | None = None
            seen: set[str] = set()
            generation = self._session_generation
            for _ in range(MAX_PAGINATION_PAGES):
                if self._session_generation != generation:
                    break
                try:
                    result = await self.request(
                        method,
                        {"cursor": cursor} if cursor is not None else {},
                        _allow_session_recovery=False,
                    )
                except MCPSessionExpired as exc:
                    recovered = await self._recover_http_session(
                        exc, method=method, params={}
                    )
                    if not recovered:
                        raise MCPProtocolError(
                            f"{method} was not restarted after HTTP session recovery"
                        ) from exc
                    if not self.supports_server_capability(capability):
                        return []
                    break
                if self._session_generation != generation:
                    break
                values = result.get(key, [])
                if not isinstance(values, list):
                    raise MCPProtocolError(f"{method} returned non-list {key}")
                if not all(isinstance(item, dict) for item in values):
                    raise MCPProtocolError(
                        f"{method} returned a non-object {key} entry"
                    )
                output.extend(values)
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    return output
                if not isinstance(next_cursor, str) or not next_cursor:
                    raise MCPProtocolError(f"{method} returned an invalid nextCursor")
                if next_cursor in seen:
                    raise MCPProtocolError(f"{method} repeated pagination cursor")
                seen.add(next_cursor)
                cursor = next_cursor
            else:
                raise MCPProtocolError(
                    f"{method} exceeded {MAX_PAGINATION_PAGES} pages"
                )
            if restart == MAX_PAGINATION_SESSION_RESTARTS:
                raise MCPProtocolError(
                    f"{method} session expired repeatedly during pagination"
                )
        raise AssertionError("unreachable pagination restart state")

    async def notify_roots_changed(self) -> None:
        if self.roots:
            await self.notify("notifications/roots/list_changed")

    async def disconnect(self) -> None:
        self._initialized = False
        self._stop_http_events()
        if self._legacy_sse_discovery and not self._legacy_sse_discovery.done():
            self._legacy_sse_discovery.set_exception(
                MCPProtocolError(f"MCP server {self.config.name!r} disconnected")
            )
        self._legacy_sse_discovery = None
        self._legacy_sse_endpoint = ""
        self._fail_pending(
            MCPProtocolError(f"MCP server {self.config.name!r} disconnected")
        )
        session_to_close = self._http_session_id or self._pending_initialize_session_id
        if session_to_close and self.config.transport != "sse":
            await self._delete_http_session(session_to_close)
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
        self._pending_initialize_session_id = ""

    async def _delete_http_session(self, session_id: str) -> None:
        if self._http is None or not session_id:
            return
        try:
            headers = httpx.Headers(self.config.resolved_headers)
            if self._oauth is not None:
                headers["Authorization"] = await self._oauth.authorization_header()
            headers["Mcp-Session-Id"] = session_id
            if self.protocol_version:
                headers["MCP-Protocol-Version"] = self.protocol_version
            async with self._http.stream(
                "DELETE",
                self.config.resolved_url,
                headers=headers,
            ) as response:
                if response.status_code != 405:
                    response.raise_for_status()
        except (httpx.HTTPError, MCPOAuthError):
            return

    async def _read_legacy_sse_events(self, generation: int) -> None:
        if self._http is None:
            raise MCPProtocolError("MCP HTTP client is not connected")
        headers = httpx.Headers(self.config.resolved_headers)
        headers["Accept"] = "text/event-stream"
        if self._oauth is not None:
            headers["Authorization"] = await self._oauth.authorization_header()

        try:
            async with self._http.stream(
                "GET", self.config.resolved_url, headers=headers
            ) as response:
                response.raise_for_status()
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .casefold()
                )
                if content_type != "text/event-stream":
                    raise MCPProtocolError(
                        "MCP SSE discovery must use text/event-stream"
                    )
                event_name = ""
                data_lines: list[str] = []
                endpoint_discovered = False

                def finish_event() -> None:
                    nonlocal event_name, data_lines
                    nonlocal endpoint_discovered
                    event_data = "\n".join(data_lines)
                    data_lines = []
                    name = event_name
                    event_name = ""
                    if name == "endpoint" and event_data:
                        try:
                            resolved_endpoint = urljoin(
                                self.config.resolved_url, event_data
                            )
                            source = urlparse(self.config.resolved_url)
                            target = urlparse(resolved_endpoint)
                        except ValueError as exc:
                            raise MCPProtocolError(
                                "MCP SSE server advertised an invalid URL"
                            ) from exc
                        if (
                            (source.scheme, source.netloc)
                            != (target.scheme, target.netloc)
                            or target.scheme not in {"http", "https"}
                            or not target.netloc
                        ):
                            raise MCPProtocolError(
                                "MCP SSE POST endpoint origin does not match "
                                "the connection origin: "
                                f"{event_data}"
                            )
                        self._legacy_sse_endpoint = resolved_endpoint
                        if (
                            self._legacy_sse_discovery
                            and not self._legacy_sse_discovery.done()
                        ):
                            self._legacy_sse_discovery.set_result(None)
                        endpoint_discovered = True
                        return
                    if name != "message" or not event_data:
                        return
                    try:
                        payload = json.loads(event_data)
                    except json.JSONDecodeError as exc:
                        raise MCPProtocolError(
                            "MCP SSE event contained invalid JSON"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise MCPProtocolError(
                            "MCP SSE data must contain a JSON-RPC object"
                        )
                    _validate_jsonrpc_message(payload)
                    if (
                        "method" not in payload
                        and isinstance(payload.get("id"), int)
                        and not isinstance(payload.get("id"), bool)
                    ):
                        response_id = payload["id"]
                        pending = self._pending.get(response_id)
                        if pending is None:
                            if len(self._legacy_sse_responses) >= (
                                MAX_BUFFERED_LEGACY_SSE_RESPONSES
                            ):
                                raise MCPProtocolError(
                                    "MCP SSE response buffer exceeded "
                                    f"{MAX_BUFFERED_LEGACY_SSE_RESPONSES} entries"
                                )
                            self._legacy_sse_responses[response_id] = payload
                        else:
                            self._pending.pop(response_id, None)
                            if pending.done():
                                return
                            pending.set_result(payload)
                    else:
                        self._dispatch_incoming(payload)

                async for line in _iter_bounded_sse_lines(
                    response, MAX_HTTP_SSE_EVENT_BYTES
                ):
                    if line.startswith("retry:"):
                        try:
                            self._sse_retry_ms = max(0, int(line[6:].strip()))
                        except ValueError as exc:
                            raise MCPProtocolError(
                                "MCP SSE stream contained an invalid retry field"
                            ) from exc
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif not line:
                        finish_event()
                if not endpoint_discovered:
                    raise MCPProtocolError(
                        "MCP SSE discovery requires an endpoint event before "
                        "the first message"
                    )
        except (httpx.HTTPError, MCPProtocolError) as exc:
            error = (
                exc
                if isinstance(exc, MCPProtocolError)
                else MCPProtocolError(f"Could not connect to MCP SSE endpoint: {exc}")
            )
            if self._legacy_sse_discovery and not self._legacy_sse_discovery.done():
                self._legacy_sse_discovery.set_exception(error)
            return

    async def _read_http_events(self) -> None:
        generation = self._sse_generation
        if self.config.transport == "sse":
            return await self._read_legacy_sse_events(generation)
        while self._initialized and generation == self._sse_generation:
            if not self._sse_supported or self._http is None:
                return
            headers = httpx.Headers(self.config.resolved_headers)
            headers["Accept"] = "text/event-stream"
            if self._http_session_id:
                headers["Mcp-Session-Id"] = self._http_session_id
            if self.protocol_version:
                headers["MCP-Protocol-Version"] = self.protocol_version
            if self._sse_last_event_id:
                headers["Last-Event-ID"] = self._sse_last_event_id
            if self._oauth is not None:
                headers["Authorization"] = await self._oauth.authorization_header()
            try:
                async with self._http.stream(
                    "GET", self.config.resolved_url, headers=headers
                ) as response:
                    if response.status_code == 405:
                        self._sse_supported = False
                        return
                    response.raise_for_status()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .casefold()
                    )
                    if content_type != "text/event-stream":
                        raise MCPProtocolError(
                            "MCP HTTP GET stream must use text/event-stream"
                        )
                    data_lines: list[str] = []
                    event_id = ""
                    async for line in _iter_bounded_sse_lines(
                        response, MAX_HTTP_SSE_EVENT_BYTES
                    ):
                        if line.startswith("retry:"):
                            try:
                                self._sse_retry_ms = max(0, int(line[6:].strip()))
                            except ValueError as exc:
                                raise MCPProtocolError(
                                    "MCP SSE stream contained an invalid retry field"
                                ) from exc
                            continue
                        if line.startswith("id:"):
                            event_id = line[3:].strip()
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                            continue
                        if line:
                            continue
                        if event_id:
                            self._sse_last_event_id = event_id
                        if data_lines and any(data_lines):
                            try:
                                payload = json.loads("\n".join(data_lines))
                            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                                raise MCPProtocolError(
                                    "MCP SSE event contained invalid JSON"
                                ) from exc
                            if not isinstance(payload, dict):
                                raise MCPProtocolError(
                                    "MCP SSE data must contain a JSON-RPC object"
                                )
                            _validate_jsonrpc_message(payload)
                            self._dispatch_incoming(payload)
                        data_lines.clear()
                        event_id = ""
            except (httpx.HTTPError, MCPProtocolError):
                pass
            await asyncio.sleep(self._sse_retry_ms / 1000)

    def _stop_http_events(self) -> None:
        self._sse_generation += 1
        task = self._sse_task
        self._sse_task = None
        if task is not None:
            task.cancel()


def _validate_http_session_id(session_id: str) -> None:
    if len(session_id) > MAX_HTTP_SESSION_ID_BYTES or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in session_id
    ):
        raise MCPProtocolError(
            "MCP HTTP session ID must contain at most "
            f"{MAX_HTTP_SESSION_ID_BYTES} visible ASCII characters"
        )


def _encode_outbound_message(payload: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise MCPProtocolError("MCP outbound message is not JSON-serializable") from exc
    if len(encoded) + 1 > MAX_OUTBOUND_MESSAGE_BYTES:
        raise MCPProtocolError(
            "MCP outbound message exceeds "
            f"{MAX_OUTBOUND_MESSAGE_BYTES} bytes"
        )
    return encoded


def _copy_http_response(response: httpx.Response, content: bytes) -> httpx.Response:
    try:
        request = response.request
    except RuntimeError:
        request = None
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=content,
        request=request,
        extensions=response.extensions,
    )


async def _read_bounded_http_response(response: httpx.Response) -> httpx.Response:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = -1
        if declared_length > MAX_HTTP_RESPONSE_BYTES:
            raise MCPProtocolError(
                f"MCP HTTP response exceeded {MAX_HTTP_RESPONSE_BYTES} bytes"
            )
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_HTTP_RESPONSE_BYTES:
            raise MCPProtocolError(
                f"MCP HTTP response exceeded {MAX_HTTP_RESPONSE_BYTES} bytes"
            )
        body.extend(chunk)
    return _copy_http_response(response, bytes(body))


async def _iter_bounded_sse_lines(
    response: httpx.Response,
    max_event_bytes: int,
) -> AsyncIterator[str]:
    """Yield SSE lines without allowing an unterminated event to grow forever."""

    pending = bytearray()
    event_bytes = 0
    async for chunk in response.aiter_bytes():
        if len(chunk) > max_event_bytes:
            raise MCPProtocolError(f"MCP SSE event exceeded {max_event_bytes} bytes")
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(pending[:newline])
            del pending[: newline + 1]
            event_bytes += len(raw_line) + 1
            if event_bytes > max_event_bytes:
                raise MCPProtocolError(
                    f"MCP SSE event exceeded {max_event_bytes} bytes"
                )
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MCPProtocolError(
                    "MCP SSE stream contained invalid UTF-8"
                ) from exc
            if not raw_line:
                event_bytes = 0
            yield line
        if len(pending) > max_event_bytes:
            raise MCPProtocolError(f"MCP SSE event exceeded {max_event_bytes} bytes")
    if pending:
        event_bytes += len(pending) + 1
        if event_bytes > max_event_bytes:
            raise MCPProtocolError(f"MCP SSE event exceeded {max_event_bytes} bytes")
        if pending.endswith(b"\r"):
            del pending[-1:]
        try:
            yield bytes(pending).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MCPProtocolError("MCP SSE stream contained invalid UTF-8") from exc


def _parse_http_messages(response: httpx.Response) -> list[dict[str, Any]]:
    content = response.content
    if len(content) > MAX_HTTP_RESPONSE_BYTES:
        raise MCPProtocolError(
            f"MCP HTTP response exceeded {MAX_HTTP_RESPONSE_BYTES} bytes"
        )
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    )
    if content and content_type not in {
        "application/json",
        "text/event-stream",
    }:
        raise MCPProtocolError(
            "MCP HTTP response must use application/json or text/event-stream"
        )
    if content_type != "text/event-stream":
        if not content:
            return []
        try:
            payload = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPProtocolError("MCP HTTP response contained invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MCPProtocolError(
                "MCP HTTP application/json response must contain one JSON-RPC object"
            )
        _validate_jsonrpc_message(payload)
        return [payload]

    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    event_bytes = 0

    def finish_event() -> None:
        nonlocal event_bytes
        event_data = "\n".join(data_lines)
        data_lines.clear()
        event_bytes = 0
        if not event_data:
            return
        try:
            payload = json.loads(event_data)
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("MCP SSE event contained invalid JSON") from exc
        if isinstance(payload, dict):
            _validate_jsonrpc_message(payload)
            messages.append(payload)
        else:
            raise MCPProtocolError("MCP SSE data must contain a JSON-RPC object")

    try:
        for line in response.iter_lines():
            event_bytes += len(line.encode("utf-8")) + 1
            if event_bytes > MAX_HTTP_SSE_EVENT_BYTES:
                raise MCPProtocolError(
                    f"MCP SSE event exceeded {MAX_HTTP_SSE_EVENT_BYTES} bytes"
                )
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if line:
                continue
            finish_event()
    except UnicodeDecodeError as exc:
        raise MCPProtocolError("MCP SSE response contained invalid UTF-8") from exc
    finish_event()
    return messages


def _validate_jsonrpc_message(message: dict[str, Any]) -> None:
    if message.get("jsonrpc") != "2.0":
        raise MCPProtocolError("MCP message must declare JSON-RPC 2.0")
    has_method = "method" in message
    has_result = "result" in message
    has_error = "error" in message
    if has_method:
        if not isinstance(message["method"], str) or not message["method"]:
            raise MCPProtocolError("MCP request method must be a non-empty string")
        if has_result or has_error:
            raise MCPProtocolError("MCP request cannot contain result or error")
    elif has_result == has_error:
        raise MCPProtocolError(
            "MCP response must contain exactly one of result or error"
        )
    if "id" in message:
        request_id = message["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            raise MCPProtocolError("MCP message id must be a string or integer")
    elif not has_method:
        raise MCPProtocolError("MCP response must contain an id")


def _client_version() -> str:
    try:
        return version("ash-ai")
    except PackageNotFoundError:
        return "0.1.0"
