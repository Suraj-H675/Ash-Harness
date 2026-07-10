"""Authenticated HTTP/SSE adapter for the asynchronous Ash SDK."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ash.sdk import AshClient
from core.events import EVENT_SCHEMA_VERSION


class TurnRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=1_000_000)


class ResumeRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class SteeringRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=1_000_000)


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
            entries = self._requests[key]
            while entries and entries[0] <= now - 60:
                entries.popleft()
            if len(entries) >= self.limit:
                return False
            entries.append(now)
            return True


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ash_client = client
        yield
        if close_client_on_shutdown:
            await client.close()

    app = FastAPI(title="Ash API", version="1", lifespan=lifespan)

    async def authorize(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.casefold() != "bearer" or not hmac.compare_digest(
            supplied, bearer_token
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
                yield _sse(event.type, event.to_wire(include_type=False))

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/v1/turn/steer", dependencies=[Depends(authorize)])
    async def steer_turn(payload: SteeringRequest) -> dict[str, int]:
        try:
            pending = await client.steer(payload.input)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OverflowError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
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
        try:
            records = client.events(
                session_id,
                after_sequence=after_sequence,
                turn_id=turn_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "events": [
                {"sequence": item.sequence, "event": item.event.to_wire()}
                for item in records
            ],
            "next_sequence": records[-1].sequence if records else after_sequence,
        }

    @app.post("/v1/sessions", dependencies=[Depends(authorize)])
    async def new_session() -> dict[str, str]:
        return {"session_id": await client.new_session()}

    @app.post("/v1/sessions/resume", dependencies=[Depends(authorize)])
    async def resume_session(payload: ResumeRequest) -> dict[str, str]:
        return {"session_id": await client.resume(payload.session_id)}

    return app


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
