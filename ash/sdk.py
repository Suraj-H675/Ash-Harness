"""Stable asynchronous library API for embedding Ash without a terminal UI."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from ash.runtime import build_runtime
from config import AshConfig
from core.events import EVENT_SCHEMA_VERSION, envelope_event, event_data
from core.loop import AshLoop
from core.redaction import redact_text
from core.session import SessionSummary
from providers.base import ProviderABC
from ui.headless import HeadlessUI


ApprovalCallback = Callable[[str, dict], Awaitable[bool]]


@dataclass(frozen=True)
class AshResult:
    response: str
    session_id: str
    model: str
    context_tokens: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    usage_source: str = "unavailable"
    estimated_prompt_tokens: int = 0
    estimated_completion_tokens: int = 0
    estimated_cost_usd: float = 0.0

    @property
    def usage(self) -> dict[str, int | float | str | bool]:
        has_estimates = self.usage_source in {"estimated", "mixed"}
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "usage_source": self.usage_source,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "estimated_completion_tokens": self.estimated_completion_tokens,
            "has_estimates": has_estimates,
            "cache_hit_rate": (
                self.cache_read_tokens / self.prompt_tokens
                if self.prompt_tokens
                else 0.0
            ),
            "cost_usd": self.cost_usd,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cost_is_estimated": self.estimated_cost_usd > 0,
        }


@dataclass(frozen=True)
class AshEvent:
    type: str
    data: dict[str, Any]
    schema_version: int = EVENT_SCHEMA_VERSION
    event_id: str = ""
    timestamp: str = ""
    source: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None
    turn_id: str | None = None
    operation_id: str | None = None
    parent_event_id: str | None = None

    def __post_init__(self) -> None:
        metadata = {
            "schema_version": self.schema_version,
            **({"event_id": self.event_id} if self.event_id else {}),
            **({"timestamp": self.timestamp} if self.timestamp else {}),
            **({"source": self.source} if self.source else {}),
            **({"session_id": self.session_id} if self.session_id else {}),
            **({"turn_id": self.turn_id} if self.turn_id else {}),
            **({"operation_id": self.operation_id} if self.operation_id else {}),
            **(
                {"parent_event_id": self.parent_event_id}
                if self.parent_event_id
                else {}
            ),
        }
        payload = envelope_event({"type": self.type, **self.data, **metadata})
        for name in (
            "schema_version",
            "event_id",
            "timestamp",
            "source",
            "session_id",
            "turn_id",
            "operation_id",
            "parent_event_id",
        ):
            object.__setattr__(self, name, payload[name])

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "AshEvent":
        event = envelope_event(payload)
        return cls(
            type=event["type"],
            data=event_data(event),
            schema_version=event["schema_version"],
            event_id=event["event_id"],
            timestamp=event["timestamp"],
            source=event["source"],
            session_id=event["session_id"],
            turn_id=event["turn_id"],
            operation_id=event["operation_id"],
            parent_event_id=event["parent_event_id"],
        )

    def to_wire(self, *, include_type: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "source": dict(self.source),
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "operation_id": self.operation_id,
            "parent_event_id": self.parent_event_id,
            **self.data,
        }
        if include_type:
            payload["type"] = self.type
        return payload


class AshClient:
    """Own one Ash runtime and its provider, tools, sessions, and subprocesses."""

    def __init__(self, loop: AshLoop, config: AshConfig) -> None:
        self.loop = loop
        self.config = config
        self._started = False
        self._turn_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        *,
        config: AshConfig | None = None,
        workspace: Path | None = None,
        provider: ProviderABC | None = None,
        approval_callback: ApprovalCallback | None = None,
        workspace_trusted: bool | None = None,
    ) -> "AshClient":
        """Create a client, honoring persisted workspace trust unless overridden."""

        runtime_config = config or AshConfig.load()
        if workspace is not None:
            runtime_config = runtime_config.model_copy(
                update={"workspace_root": workspace.resolve()}
            )
        runtime = build_runtime(
            runtime_config,
            ui=HeadlessUI(output_format="text", stream=io.StringIO()),
            provider=provider,
            workspace_trusted=workspace_trusted,
            approval_callback=approval_callback,
        )
        client = cls(runtime.loop, runtime_config)
        await client.start()
        return client

    async def start(self, session_id: str | None = None) -> str:
        async with self._turn_lock:
            return await self._start_unlocked(session_id)

    async def _start_unlocked(self, session_id: str | None = None) -> str:
        if self._started and self.loop.current_session is not None:
            return self.loop.current_session.session_id
        session = await self.loop.start_session(session_id)
        self._started = True
        return session.session_id

    async def prompt(self, text: str) -> AshResult:
        async with self._turn_lock:
            return await self._prompt_unlocked(text)

    async def steer(self, text: str) -> int:
        """Queue guidance for the currently running turn without waiting on it."""

        if not self.loop.is_turn_running:
            raise RuntimeError("no turn is currently running")
        return self.loop.queue_steering(text)

    async def _prompt_unlocked(self, text: str) -> AshResult:
        if not text.strip():
            raise ValueError("prompt cannot be empty")
        if not self._started:
            await self._start_unlocked()
        response = await self.loop.run_turn(text)
        assert self.loop.current_session is not None
        usage = self.loop.last_turn_usage
        return AshResult(
            response=response,
            session_id=self.loop.current_session.session_id,
            model=self.config.model,
            context_tokens=self.loop._last_context_tokens,
            prompt_tokens=int(usage["prompt_tokens"]),
            completion_tokens=int(usage["completion_tokens"]),
            cache_read_tokens=int(usage["cache_read_tokens"]),
            cache_write_tokens=int(usage["cache_write_tokens"]),
            cost_usd=float(usage["cost_usd"]),
            usage_source=str(usage["usage_source"]),
            estimated_prompt_tokens=int(usage["estimated_prompt_tokens"]),
            estimated_completion_tokens=int(usage["estimated_completion_tokens"]),
            estimated_cost_usd=float(usage["estimated_cost_usd"]),
        )

    async def stream_prompt(self, text: str) -> AsyncIterator[AshEvent]:
        """Yield real runtime deltas and one terminal completion/error event."""

        if not text.strip():
            raise ValueError("prompt cannot be empty")
        async with self._turn_lock:
            ui = self.loop.ui
            if not isinstance(ui, HeadlessUI):
                raise RuntimeError("stream_prompt requires Ash's headless event UI")
            queue: asyncio.Queue[AshEvent] = asyncio.Queue()

            def receive(payload: dict[str, Any]) -> None:
                queue.put_nowait(AshEvent.from_wire(payload))

            unsubscribe = ui.subscribe(receive)

            async def run() -> None:
                try:
                    result = await self._prompt_unlocked(text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    queue.put_nowait(
                        AshEvent("turn.error", {"error": redact_text(str(exc))})
                    )
                    return
                queue.put_nowait(
                    AshEvent(
                        "turn.completed",
                        {
                            "response": result.response,
                            "session_id": result.session_id,
                            "model": result.model,
                            "context_tokens": result.context_tokens,
                            "usage": result.usage,
                        },
                    )
                )

            task = asyncio.create_task(run())
            try:
                yield AshEvent("turn.started", {})
                while True:
                    event = await queue.get()
                    yield event
                    if event.type in {"turn.completed", "turn.error"}:
                        break
            finally:
                unsubscribe()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def sessions(self, *, query: str = "", limit: int = 20) -> list[SessionSummary]:
        return self.loop.session_store.list_sessions(
            project_path=str(self.loop.project_root), query=query, limit=limit
        )

    async def resume(self, session_id: str) -> str:
        async with self._turn_lock:
            session = await self.loop.start_session(session_id)
            self._started = True
            return session.session_id

    async def new_session(self) -> str:
        async with self._turn_lock:
            session = await self.loop.start_session()
            self._started = True
            return session.session_id

    async def close(self) -> None:
        async with self._turn_lock:
            await self.loop.aclose()
            self._started = False

    async def __aenter__(self) -> "AshClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
