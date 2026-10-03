"""Authenticated HTTP/SSE adapter for the asynchronous Ash SDK."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, AsyncIterator, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, StrictInt
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ash.sdk import AshClient
from ash.core.events import EVENT_SCHEMA_VERSION
from ash.core.redaction import redact_text, redact_value
from ash.server.jsonrpc import JSONRPCServer
from ash.tools.base import redact_tool_arguments


class TurnRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=1_000_000)
    session_id: str | None = Field(default=None, min_length=1, max_length=512)


class ResumeRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class SteeringRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=1_000_000)


class ForkSessionRequest(BaseModel):
    message_count: StrictInt | None = Field(default=None, ge=0)
    branch_name: str = Field(default="", max_length=128)
    branch_summary: str = Field(default="", max_length=12_000)


class ApprovalDecisionRequest(BaseModel):
    approved: bool


MAX_HTTP_RATE_LIMIT_KEYS = 10_000
MAX_PENDING_HTTP_APPROVALS = 64
MAX_HTTP_APPROVAL_TIMEOUT_SECONDS = 3600.0
DEFAULT_HTTP_APPROVAL_TIMEOUT_SECONDS = 300.0


@dataclass
class _PendingHTTPApproval:
    request_id: str
    tool: str
    arguments: dict[str, Any]
    future: asyncio.Future[bool]


class HTTPApprovalBroker:
    """Bounded live approval broker for authenticated remote operators."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_HTTP_APPROVAL_TIMEOUT_SECONDS,
        max_pending: int = MAX_PENDING_HTTP_APPROVALS,
    ) -> None:
        if not 1 <= timeout_seconds <= MAX_HTTP_APPROVAL_TIMEOUT_SECONDS:
            raise ValueError(
                "HTTP approval timeout must be between 1 and "
                f"{int(MAX_HTTP_APPROVAL_TIMEOUT_SECONDS)} seconds"
            )
        if max_pending < 1:
            raise ValueError("HTTP approval capacity must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.max_pending = max_pending
        self._pending: dict[str, _PendingHTTPApproval] = {}
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._closed = False
        self._tool_provider: Callable[[str], Any] | None = None

    def set_tool_provider(self, provider: Callable[[str], Any]) -> None:
        self._tool_provider = provider

    async def request(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        request_id = uuid4().hex
        tool = self._tool_provider(tool_name) if self._tool_provider is not None else None
        display_arguments = redact_tool_arguments(tool, arguments)
        pending = _PendingHTTPApproval(
            request_id=request_id,
            tool=redact_text(tool_name),
            arguments=display_arguments,
            future=future,
        )
        async with self._lock:
            if self._closed or len(self._pending) >= self.max_pending:
                return False
            self._pending[request_id] = pending
            self._changed.set()
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except TimeoutError:
            return False
        finally:
            async with self._lock:
                if self._pending.get(request_id) is pending:
                    self._pending.pop(request_id, None)

    async def list_pending(self) -> list[dict[str, Any]]:
        async with self._lock:
            return self._pending_payload_locked()

    async def wait_pending(self, timeout_seconds: float) -> list[dict[str, Any]]:
        if not 0 <= timeout_seconds <= 30:
            raise ValueError("approval wait must be between 0 and 30 seconds")
        deadline = time.monotonic() + timeout_seconds
        while True:
            async with self._lock:
                if self._pending or self._closed or timeout_seconds == 0:
                    return self._pending_payload_locked()
                self._changed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except TimeoutError:
                return []

    async def resolve(self, request_id: str, *, approved: bool) -> bool:
        async with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        self._changed.set()
        if not pending.future.done():
            pending.future.set_result(bool(approved))
        return True

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending.values())
            self._pending.clear()
            self._changed.set()
        for item in pending:
            if not item.future.done():
                item.future.set_result(False)

    def _pending_payload_locked(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.request_id,
                "tool": item.tool,
                "arguments": item.arguments,
            }
            for item in self._pending.values()
        ]


class SlidingWindowLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        self.limit = requests_per_minute
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            if (
                key not in self._requests
                and len(self._requests) >= MAX_HTTP_RATE_LIMIT_KEYS
            ):
                stale = [
                    item_key
                    for item_key, values in self._requests.items()
                    if not values or values[-1] <= now - 60
                ]
                for item_key in stale:
                    self._requests.pop(item_key, None)
                if len(self._requests) >= MAX_HTTP_RATE_LIMIT_KEYS:
                    return False
            entries = self._requests[key]
            while entries and entries[0] <= now - 60:
                entries.popleft()
            if len(entries) >= self.limit:
                return False
            entries.append(now)
        return True


MAX_JSONRPC_BODY_BYTES = 1_048_576
MAX_JSONRPC_BATCH_REQUESTS = 32
MAX_EVENT_LIST_LIMIT = 10_000
MAX_HTTP_BODY_BYTES = 16 * 1024 * 1024
MAX_HTTP_IN_FLIGHT_TURNS = 16
MAX_HTTP_PREAUTH_REQUESTS_PER_MINUTE = 240
PUBLIC_HTTP_PATHS = frozenset(
    {"/health", "/ui", "/ui/control.css", "/ui/control.js"}
)
CONTROL_UI_ASSETS = frozenset({"control.html", "control.css", "control.js"})
CONTROL_UI_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class _HTTPBoundaryMiddleware:
    """Authenticate and rate-limit before buffering protected REST bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        bearer_token: str,
        requests_per_minute: int,
        max_bytes: int,
    ) -> None:
        if (
            len(bearer_token) < 16
            or not bearer_token.isascii()
            or any(character.isspace() for character in bearer_token)
        ):
            raise ValueError(
                "HTTP bearer token must contain at least 16 non-whitespace ASCII characters"
            )
        self.app = app
        self._token = bearer_token.encode("ascii")
        self._preauth_limiter = SlidingWindowLimiter(
            max(MAX_HTTP_PREAUTH_REQUESTS_PER_MINUTE, requests_per_minute * 4)
        )
        self._limiter = SlidingWindowLimiter(requests_per_minute)
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] != "http" or path in PUBLIC_HTTP_PATHS:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        key = str(client[0]) if client else "unknown"
        if not await self._preauth_limiter.allow(key):
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many authentication attempts"},
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return

        authorization_values = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"authorization"
        ]
        scheme, _, supplied = (
            authorization_values[0] if len(authorization_values) == 1 else b""
        ).partition(b" ")
        if (
            len(authorization_values) != 1
            or scheme.lower() != b"bearer"
            or not hmac.compare_digest(supplied, self._token)
        ):
            response = JSONResponse(
                status_code=401,
                content={"detail": "Invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        if not await self._limiter.allow(key):
            response = JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return

        if (
            scope.get("path") == "/rpc"
            or scope.get("method") not in {"POST", "PUT", "PATCH"}
        ):
            await self.app(scope, receive, send)
            return

        for header_name, value in scope.get("headers", []):
            if header_name.lower() != b"content-length":
                continue
            try:
                content_length = int(value)
            except ValueError:
                break
            if content_length > self.max_bytes:
                await self._reject_oversized_body(scope, receive, send)
                return
            break

        buffered: list[Message] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                await self._reject_oversized_body(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject_oversized_body(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body exceeds the server limit"},
        )
        await response(scope, receive, send)


def create_app(
    client: AshClient,
    *,
    bearer_token: str,
    requests_per_minute: int = 60,
    close_client_on_shutdown: bool = False,
    max_in_flight_turns: int = MAX_HTTP_IN_FLIGHT_TURNS,
    approval_broker: HTTPApprovalBroker | None = None,
) -> FastAPI:
    if (
        len(bearer_token) < 16
        or not bearer_token.isascii()
        or any(character.isspace() for character in bearer_token)
    ):
        raise ValueError(
            "HTTP bearer token must contain at least 16 non-whitespace ASCII characters"
        )
    if max_in_flight_turns < 1:
        raise ValueError("HTTP in-flight turn limit must be positive")
    rpc = JSONRPCServer(client)
    active_turns = 0

    def try_acquire_turn_slot() -> bool:
        nonlocal active_turns
        if active_turns >= max_in_flight_turns:
            return False
        active_turns += 1
        return True

    def release_turn_slot() -> None:
        nonlocal active_turns
        if active_turns <= 0:
            raise RuntimeError("HTTP turn admission accounting underflow")
        active_turns -= 1

    def require_turn_slot() -> None:
        if try_acquire_turn_slot():
            return
        raise HTTPException(
            status_code=503,
            detail="Ash is busy; retry this turn later",
            headers={"Retry-After": "1"},
        )

    class AdmittedTurnStreamingResponse(StreamingResponse):
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if not try_acquire_turn_slot():
                response = JSONResponse(
                    status_code=503,
                    content={"detail": "Ash is busy; retry this turn later"},
                    headers={"Retry-After": "1"},
                )
                await response(scope, receive, send)
                return
            try:
                await super().__call__(scope, receive, send)
            finally:
                release_turn_slot()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ash_client = client
        primary_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                await rpc.close(close_client=close_client_on_shutdown)
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    "HTTP JSON-RPC shutdown cleanup failed: "
                    + redact_text(str(cleanup_error))
                )
            if approval_broker is not None:
                try:
                    await approval_broker.close()
                except BaseException as cleanup_error:
                    if primary_error is None:
                        raise
                    primary_error.add_note(
                        "HTTP approval broker shutdown failed: "
                        + redact_text(str(cleanup_error))
                    )

    app = FastAPI(
        title="Ash API",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        _HTTPBoundaryMiddleware,
        bearer_token=bearer_token,
        requests_per_minute=requests_per_minute,
        max_bytes=MAX_HTTP_BODY_BYTES,
    )

    @app.get("/ui")
    async def control_ui() -> Response:
        return _control_ui_asset("control.html", "text/html; charset=utf-8")

    @app.get("/ui/control.css")
    async def control_ui_css() -> Response:
        return _control_ui_asset("control.css", "text/css; charset=utf-8")

    @app.get("/ui/control.js")
    async def control_ui_js() -> Response:
        return _control_ui_asset(
            "control.js",
            "text/javascript; charset=utf-8",
        )

    @app.get("/health")
    async def health() -> dict[str, str | int]:
        return {
            "status": "ok",
            "service": "ash",
            "event_schema_version": EVENT_SCHEMA_VERSION,
        }

    @app.post("/rpc")
    async def json_rpc(request: Request) -> Response:
        content_type = request.headers.get("content-type", "").partition(";")[0]
        if content_type.casefold() != "application/json":
            return JSONResponse(
                status_code=415,
                content={
                    "error": {"code": -32700, "message": "Unsupported media type"}
                },
            )
        body = await _read_bounded_body(request, MAX_JSONRPC_BODY_BYTES)
        if body is None or not body:
            return JSONResponse(
                status_code=413,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"},
                },
            )
        try:
            payload = json.loads(
                body,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                },
            )

        if isinstance(payload, list):
            if not payload or len(payload) > MAX_JSONRPC_BATCH_REQUESTS:
                response: dict[str, Any] | list[dict[str, Any]] = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"},
                }
            else:
                responses = [
                    handled
                    for handled in await asyncio.gather(
                        *(rpc.handle_request(item) for item in payload)
                    )
                    if handled is not None
                ]
                response = responses
                if not responses:
                    return Response(status_code=204)
            return JSONResponse(content=response)

        handled = await rpc.handle_request(payload)
        if handled is None:
            return Response(status_code=204)
        return JSONResponse(content=handled)

    @app.post("/v1/turn")
    async def run_turn(payload: TurnRequest) -> dict:
        require_turn_slot()
        try:
            result = (
                await client.prompt(payload.input, session_id=payload.session_id)
                if payload.session_id is not None
                else await client.prompt(payload.input)
            )
            return {
                "response": result.response,
                "session_id": result.session_id,
                "model": result.model,
                "context_tokens": result.context_tokens,
                "usage": result.usage,
            }
        finally:
            release_turn_slot()

    @app.post("/v1/turn/stream")
    async def stream_turn(payload: TurnRequest) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            stream = (
                client.stream_prompt(payload.input, session_id=payload.session_id)
                if payload.session_id is not None
                else client.stream_prompt(payload.input)
            )
            async for event in stream:
                yield _sse(
                    event.type,
                    redact_value(event.to_wire(include_type=False)),
                )

        return AdmittedTurnStreamingResponse(events(), media_type="text/event-stream")

    @app.post("/v1/turn/steer")
    async def steer_turn(payload: SteeringRequest) -> dict[str, int]:
        try:
            pending = await client.steer(payload.input)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=redact_text(str(exc))) from exc
        except OverflowError as exc:
            raise HTTPException(status_code=429, detail=redact_text(str(exc))) from exc
        return {"pending": pending}

    @app.get("/v1/approvals")
    async def approvals(wait_seconds: float = 0) -> dict[str, Any]:
        if not 0 <= wait_seconds <= 30:
            raise HTTPException(
                status_code=422,
                detail="wait_seconds must be between 0 and 30",
            )
        return {
            "enabled": approval_broker is not None,
            "approvals": (
                await approval_broker.wait_pending(wait_seconds)
                if approval_broker is not None
                else []
            ),
        }

    @app.post("/v1/approvals/{request_id}")
    async def resolve_approval(
        request_id: str,
        payload: ApprovalDecisionRequest,
    ) -> dict[str, bool]:
        if approval_broker is None:
            raise HTTPException(
                status_code=409,
                detail="Remote approvals are unavailable",
            )
        resolved = await approval_broker.resolve(
            request_id,
            approved=payload.approved,
        )
        if not resolved:
            raise HTTPException(status_code=404, detail="Approval request not found")
        return {"resolved": True, "approved": payload.approved}

    @app.get("/v1/sessions")
    async def sessions(query: str = "", limit: int = 20) -> dict:
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="limit must be 1..100")
        return {
            "sessions": [
                item.model_dump(mode="json")
                for item in client.sessions(query=query, limit=limit)
            ]
        }

    @app.get("/v1/sessions/{session_id}/messages")
    async def session_messages(session_id: str, limit: int = 200) -> dict:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be 1..500")
        try:
            messages = client.session_messages(session_id, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=redact_text(str(exc))) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=redact_text(str(exc))) from exc
        return {"messages": messages}

    @app.get("/v1/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        after_sequence: int = 0,
        turn_id: str | None = None,
        limit: int = 1000,
    ) -> dict:
        if after_sequence < 0:
            raise HTTPException(
                status_code=422, detail="after_sequence cannot be negative"
            )
        if not 1 <= limit <= MAX_EVENT_LIST_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=f"limit must be 1..{MAX_EVENT_LIST_LIMIT}",
            )
        try:
            records = client.events(
                session_id,
                after_sequence=after_sequence,
                turn_id=turn_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=redact_text(str(exc))) from exc
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "events": [
                {"sequence": item.sequence, "event": item.event.to_wire()}
                for item in records
            ],
            "next_sequence": records[-1].sequence if records else after_sequence,
        }

    @app.get("/v1/sessions/{session_id}/tree")
    async def session_tree(session_id: str) -> dict:
        try:
            tree = client.session_tree(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=redact_text(str(exc))) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=redact_text(str(exc))) from exc
        return {"sessions": [item.model_dump(mode="json") for item in tree]}

    @app.post("/v1/sessions/{session_id}/fork")
    async def fork_session(
        session_id: str, payload: ForkSessionRequest
    ) -> dict[str, str]:
        try:
            forked_id = await client.fork(
                session_id,
                message_count=payload.message_count,
                branch_name=payload.branch_name,
                branch_summary=payload.branch_summary,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=redact_text(str(exc))) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=redact_text(str(exc))) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=redact_text(str(exc))) from exc
        return {"session_id": forked_id}

    @app.post("/v1/sessions")
    async def new_session() -> dict[str, str]:
        return {"session_id": await client.new_session()}

    @app.post("/v1/sessions/resume")
    async def resume_session(payload: ResumeRequest) -> dict[str, str]:
        try:
            session_id = await client.resume(payload.session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=redact_text(str(exc))) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=redact_text(str(exc))) from exc
        return {"session_id": session_id}

    return app


def _sse(event: str, payload: dict) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    return f"event: {event}\ndata: {encoded}\n\n"


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes | None:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            return None
        body.extend(chunk)
    return bytes(body)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_json_constant(raw: str) -> None:
    raise ValueError(f"invalid JSON constant: {raw}")


@lru_cache(maxsize=len(CONTROL_UI_ASSETS))
def _control_ui_bytes(name: str) -> bytes:
    if name not in CONTROL_UI_ASSETS:
        raise ValueError("unknown Ash control UI asset")
    return resources.files("ash.server").joinpath("static", name).read_bytes()


def _control_ui_asset(name: str, media_type: str) -> Response:
    return Response(
        content=_control_ui_bytes(name),
        media_type=media_type,
        headers=CONTROL_UI_HEADERS,
    )
