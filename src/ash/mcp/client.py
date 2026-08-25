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
MAX_PAGINATION_SESSION_RESTARTS = 1
MAX_STDIO_MESSAGE_BYTES = 8 * 1024 * 1024
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
        self._write_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = http_client
        self._owns_http = http_client is None
        self._http_session_id = ""
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
        capabilities["tasks"] = {"list": {}}
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
            else:
                raise MCPProtocolError(
                    f"Unsupported MCP transport: {self.config.transport}"
                )
            try:
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
        bypass_session_readiness: bool = False,
    ) -> dict[str, Any]:
        response_http = await self._post_http(
            payload,
            expected_tool_contract=expected_tool_contract,
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
    ) -> None:
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
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode()
                )
                await self._process.stdin.drain()
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
        expected_tool_contract: tuple[str, str] | None = None,
        bypass_session_readiness: bool = False,
    ) -> httpx.Response:
        if self._http is None:
            raise MCPProtocolError("MCP HTTP client is not connected")
        headers = httpx.Headers(self.config.resolved_headers)
        headers["Accept"] = "application/json, text/event-stream"
        headers["Content-Type"] = "application/json"
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
        response = await self._http.post(
            self.config.resolved_url,
            json=payload,
            headers=headers,
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
        if response.status_code == 404 and sent_session_id:
            raise MCPSessionExpired(sent_session_id, sent_generation)
        response.raise_for_status()
        if is_initialize:
            session_id = response.headers.get("Mcp-Session-Id", "")
            if session_id:
                _validate_http_session_id(session_id)
                self._pending_initialize_session_id = session_id
        return response

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
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if not as_task:
            return await self.request(
                "tools/call",
                params,
                _expected_tool_contract=(name, expected_contract)
                if expected_contract is not None
                else None,
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
        self._fail_pending(
            MCPProtocolError(f"MCP server {self.config.name!r} disconnected")
        )
        session_to_close = self._http_session_id or self._pending_initialize_session_id
        if session_to_close:
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
            response = await self._http.delete(
                self.config.resolved_url,
                headers=headers,
            )
            if response.status_code != 405:
                response.raise_for_status()
        except (httpx.HTTPError, MCPOAuthError):
            return


def _validate_http_session_id(session_id: str) -> None:
    if len(session_id) > MAX_HTTP_SESSION_ID_BYTES or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in session_id
    ):
        raise MCPProtocolError(
            "MCP HTTP session ID must contain at most "
            f"{MAX_HTTP_SESSION_ID_BYTES} visible ASCII characters"
        )


def _parse_http_messages(response: httpx.Response) -> list[dict[str, Any]]:
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    )
    if response.content and content_type not in {
        "application/json",
        "text/event-stream",
    }:
        raise MCPProtocolError(
            "MCP HTTP response must use application/json or text/event-stream"
        )
    if content_type != "text/event-stream":
        if not response.content:
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
    for line in [*response.text.splitlines(), ""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line or not data_lines:
            continue
        event_data = "\n".join(data_lines)
        if not event_data:
            data_lines.clear()
            continue
        try:
            payload = json.loads(event_data)
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("MCP SSE event contained invalid JSON") from exc
        data_lines.clear()
        if isinstance(payload, dict):
            _validate_jsonrpc_message(payload)
            messages.append(payload)
        else:
            raise MCPProtocolError("MCP SSE data must contain a JSON-RPC object")
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
