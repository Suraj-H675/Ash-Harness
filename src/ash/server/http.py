"""Authenticated HTTP/SSE adapter for the asynchronous Ash SDK."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, StrictInt
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ash.sdk import AshClient
from ash.core.events import EVENT_SCHEMA_VERSION
from ash.core.redaction import redact_text, redact_value
from ash.server.jsonrpc import JSONRPCServer


class TurnRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=1_000_000)


class ResumeRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class SteeringRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=1_000_000)


class ForkSessionRequest(BaseModel):
    message_count: StrictInt | None = Field(default=None, ge=0)
    branch_name: str = Field(default="", max_length=128)
    branch_summary: str = Field(default="", max_length=12_000)


MAX_HTTP_RATE_LIMIT_KEYS = 10_000


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


class _BoundedRequestBodyMiddleware:
    """Bound inbound REST bodies before framework parsing allocates them."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("path") == "/rpc"
            or scope.get("method") not in {"POST", "PUT", "PATCH"}
        ):
            await self.app(scope, receive, send)
            return

        for key, value in scope.get("headers", []):
            if key.lower() != b"content-length":
                continue
            try:
                content_length = int(value)
            except ValueError:
                break
            if content_length > self.max_bytes:
                await self._reject(scope, receive, send)
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
                await self._reject(scope, receive, send)
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
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
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
) -> FastAPI:
    if len(bearer_token) < 16:
        raise ValueError("HTTP bearer token must contain at least 16 characters")
    limiter = SlidingWindowLimiter(requests_per_minute)
    rpc = JSONRPCServer(client)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ash_client = client
        yield
        if close_client_on_shutdown:
            await client.close()

    app = FastAPI(title="Ash API", version="1", lifespan=lifespan)
    app.add_middleware(_BoundedRequestBodyMiddleware, max_bytes=MAX_HTTP_BODY_BYTES)

    async def authorize(request: Request) -> None:
        authorization_values = [
            value.decode("latin-1")
            for key, value in request.scope.get("headers", [])
            if key.lower() == b"authorization"
        ]
        scheme, _, supplied = (
            authorization_values[0] if len(authorization_values) == 1 else ""
        ).partition(" ")
        if (
            len(authorization_values) != 1
            or scheme.casefold() != "bearer"
            or not hmac.compare_digest(supplied, bearer_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        key = request.client.host if request.client else "unknown"
        if not await limiter.allow(key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={"Retry-After": "60"},
            )

    @app.get("/health")
    async def health() -> dict[str, str | int]:
        return {
            "status": "ok",
            "service": "ash",
            "event_schema_version": EVENT_SCHEMA_VERSION,
        }

    @app.post("/rpc", dependencies=[Depends(authorize)])
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

    @app.post("/v1/turn", dependencies=[Depends(authorize)])
    async def run_turn(payload: TurnRequest) -> dict:
        result = await client.prompt(payload.input)
        return {
            "response": result.response,
            "session_id": result.session_id,
            "model": result.model,
            "context_tokens": result.context_tokens,
            "usage": result.usage,
        }

    @app.post("/v1/turn/stream", dependencies=[Depends(authorize)])
    async def stream_turn(payload: TurnRequest) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            async for event in client.stream_prompt(payload.input):
                yield _sse(event.type, redact_value(event.to_wire(include_type=False)))

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/v1/turn/steer", dependencies=[Depends(authorize)])
    async def steer_turn(payload: SteeringRequest) -> dict[str, int]:
        try:
            pending = await client.steer(payload.input)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=redact_text(str(exc))) from exc
        except OverflowError as exc:
            raise HTTPException(status_code=429, detail=redact_text(str(exc))) from exc
        return {"pending": pending}

    @app.get("/v1/sessions", dependencies=[Depends(authorize)])
    async def sessions(query: str = "", limit: int = 20) -> dict:
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="limit must be 1..100")
        return {
            "sessions": [
                item.model_dump(mode="json")
                for item in client.sessions(query=query, limit=limit)
            ]
        }

    @app.get("/v1/sessions/{session_id}/events", dependencies=[Depends(authorize)])
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

    @app.get("/v1/sessions/{session_id}/tree", dependencies=[Depends(authorize)])
    async def session_tree(session_id: str) -> dict:
        try:
            tree = client.session_tree(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=redact_text(str(exc))) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=redact_text(str(exc))) from exc
        return {"sessions": [item.model_dump(mode="json") for item in tree]}

    @app.post("/v1/sessions/{session_id}/fork", dependencies=[Depends(authorize)])
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

    @app.post("/v1/sessions", dependencies=[Depends(authorize)])
    async def new_session() -> dict[str, str]:
        return {"session_id": await client.new_session()}

    @app.post("/v1/sessions/resume", dependencies=[Depends(authorize)])
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
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


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
