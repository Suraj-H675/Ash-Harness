"""Authenticated A2A 1.0 server backed by isolated durable Ash sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import aiosqlite
from a2a.auth.user import User
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    DefaultServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import DatabaseTaskStore, TaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Message,
    Part,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message as ASGIMessage, Receive, Scope, Send

from ash.sdk import AshClient
from ash.sqlite_utils import PinnedSQLiteDatabase, SQLitePathError
from ash.config import AshConfig
from ash.core.redaction import redact_urls_in_text
from ash.core.session import normalize_project_path


MAX_A2A_INPUT_BYTES = 1_000_000
MAX_A2A_BODY_BYTES = 8 * 1024 * 1024
MAX_A2A_CONTEXT_ID_BYTES = 512
MAX_A2A_SESSION_MAPPINGS = 100_000
MAX_A2A_RATE_LIMIT_KEYS = 10_000
MAX_A2A_IN_FLIGHT_TASKS = 16
MAX_A2A_PREAUTH_REQUESTS_PER_MINUTE = 240


class _TokenUser(User):
    def __init__(self, owner_scope: str) -> None:
        self._owner_scope = owner_scope

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._owner_scope


class _AuthenticatedContextBuilder(DefaultServerCallContextBuilder):
    def __init__(self, workspace: Path) -> None:
        self._owner_scope = _a2a_owner_scope(workspace)

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.user = _TokenUser(self._owner_scope)
        return context


class _SlidingWindowLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        if requests_per_minute < 1:
            raise ValueError("A2A rate limit must be positive")
        self.limit = requests_per_minute
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            if (
                key not in self._requests
                and len(self._requests) >= MAX_A2A_RATE_LIMIT_KEYS
            ):
                stale = [
                    item_key
                    for item_key, values in self._requests.items()
                    if not values or values[-1] <= now - 60
                ]
                for item_key in stale:
                    self._requests.pop(item_key, None)
                if len(self._requests) >= MAX_A2A_RATE_LIMIT_KEYS:
                    return False
            entries = self._requests[key]
            while entries and entries[0] <= now - 60:
                entries.popleft()
            if len(entries) >= self.limit:
                return False
            entries.append(now)
            return True


class A2AAuthMiddleware:
    """Protect every route except health and the intentionally public card."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        bearer_token: str,
        requests_per_minute: int,
    ) -> None:
        if (
            len(bearer_token) < 16
            or not bearer_token.isascii()
            or any(character.isspace() for character in bearer_token)
        ):
            raise ValueError(
                "A2A bearer token must contain at least 16 non-whitespace ASCII characters"
            )
        self.app = app
        self._token = bearer_token.encode("ascii")
        self._preauth_limiter = _SlidingWindowLimiter(
            max(MAX_A2A_PREAUTH_REQUESTS_PER_MINUTE, requests_per_minute * 4)
        )
        self._limiter = _SlidingWindowLimiter(requests_per_minute)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in {
            "/health",
            "/.well-known/agent-card.json",
        }:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        key = str(client[0]) if client else "unknown"
        if not await self._preauth_limiter.allow(key):
            response = JSONResponse(
                {"detail": "Too many authentication attempts"},
                status_code=429,
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
                {"detail": "Invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        if not await self._limiter.allow(key):
            response = JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return
        if scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        for header_name, value in scope.get("headers", []):
            if header_name.lower() != b"content-length":
                continue
            try:
                content_length = int(value)
            except ValueError:
                break
            if content_length > MAX_A2A_BODY_BYTES:
                await self._reject_oversized_body(scope, receive, send)
                return
            break

        buffered: list[ASGIMessage] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b""))
            if total > MAX_A2A_BODY_BYTES:
                await self._reject_oversized_body(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay() -> ASGIMessage:
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
            {"detail": "Request body exceeds the A2A server limit"},
            status_code=413,
        )
        await response(scope, receive, send)


class A2ASessionRegistry:
    """Durably map opaque A2A context IDs to project-scoped Ash sessions."""

    def __init__(self, db_path: Path, workspace: Path) -> None:
        try:
            self._database = PinnedSQLiteDatabase.prepare(
                db_path,
                label="A2A session registry database",
            )
        except SQLitePathError as exc:
            raise ValueError(str(exc)) from exc
        self.db_path = self._database.path
        self.workspace = normalize_project_path(workspace)
        self._lock = asyncio.Lock()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ash_context_sessions (
                    context_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    project_path TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        try:
            return self._database.connect(
                label="A2A session registry database",
                check_same_thread=True,
            )
        except (SQLitePathError, sqlite3.Error) as exc:
            raise ValueError(f"cannot open A2A session registry database: {exc}") from exc

    async def get(self, context_id: str) -> str | None:
        _validate_context_id(context_id)
        async with self._lock:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT session_id, project_path FROM ash_context_sessions "
                    "WHERE context_id = ?",
                    (context_id,),
                ).fetchone()
        if row is None:
            return None
        if normalize_project_path(row[1]) != self.workspace:
            raise ValueError("A2A context belongs to a different workspace")
        return str(row[0])

    async def bind(self, context_id: str, session_id: str) -> None:
        _validate_context_id(context_id)
        if not session_id or len(session_id.encode("utf-8")) > 512:
            raise ValueError("invalid Ash session ID")
        async with self._lock:
            with closing(self._connect()) as conn, conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM ash_context_sessions"
                ).fetchone()[0]
                existing = conn.execute(
                    "SELECT session_id, project_path FROM ash_context_sessions "
                    "WHERE context_id = ?",
                    (context_id,),
                ).fetchone()
                if existing is not None:
                    if existing != (session_id, self.workspace):
                        raise ValueError("A2A context is already bound")
                    conn.execute(
                        "UPDATE ash_context_sessions SET updated_at = CURRENT_TIMESTAMP "
                        "WHERE context_id = ?",
                        (context_id,),
                    )
                    return
                if count >= MAX_A2A_SESSION_MAPPINGS:
                    raise RuntimeError("A2A context mapping limit reached")
                conn.execute(
                    "INSERT INTO ash_context_sessions "
                    "(context_id, session_id, project_path) VALUES (?, ?, ?)",
                    (context_id, session_id, self.workspace),
                )


def _create_a2a_task_engine(db_path: Path) -> AsyncEngine:
    """Create the SDK task engine with link checks at each new connection."""

    try:
        database = PinnedSQLiteDatabase.prepare(
            db_path,
            label="A2A task database",
        )
    except SQLitePathError as exc:
        raise ValueError(str(exc)) from exc

    async def connect() -> aiosqlite.Connection:
        connection: aiosqlite.Connection | None = None
        try:
            database.verify(label="A2A task database")
            connection = await aiosqlite.connect(
                database.read_write_uri(),
                uri=True,
            )
            database.verify(label="A2A task database")
            return connection
        except SQLitePathError as exc:
            if connection is not None:
                await connection.close()
            raise ValueError(str(exc)) from exc
        except BaseException:
            if connection is not None:
                await connection.close()
            raise

    return create_async_engine(
        f"sqlite+aiosqlite:///{database.path}",
        async_creator=connect,
    )


async def _settle_a2a_cleanup_task(
    task: asyncio.Task[object],
) -> tuple[BaseException | None, bool]:
    """Finish owned A2A cleanup despite repeated caller cancellation."""

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except BaseException:
            break
    try:
        task.result()
    except BaseException as exc:
        return exc, interrupted
    return None, interrupted


@dataclass(slots=True)
class _A2AContextGate:
    lock: asyncio.Lock
    references: int = 0


class AshA2AExecutor(AgentExecutor):
    """Translate A2A tasks into cancellation-safe Ash SDK turns."""

    def __init__(
        self,
        config: AshConfig,
        registry: A2ASessionRegistry,
        *,
        max_in_flight_tasks: int = MAX_A2A_IN_FLIGHT_TASKS,
    ) -> None:
        if max_in_flight_tasks < 1:
            raise ValueError("A2A in-flight task limit must be positive")
        self.config = config
        self.registry = registry
        self._max_in_flight_tasks = max_in_flight_tasks
        self._active_tasks = 0
        self._context_gates: dict[str, _A2AContextGate] = {}
        self._retired_clients: set[AshClient] = set()
        self._cleanup_lock = asyncio.Lock()

    def _try_acquire_task_slot(self) -> bool:
        if self._active_tasks >= self._max_in_flight_tasks:
            return False
        self._active_tasks += 1
        return True

    def _release_task_slot(self) -> None:
        if self._active_tasks <= 0:
            raise RuntimeError("A2A task admission accounting underflow")
        self._active_tasks -= 1

    def _retain_context_gate(self, context_id: str) -> _A2AContextGate:
        """Keep the shared gate alive while a task owns or waits for it."""
        gate = self._context_gates.get(context_id)
        if gate is None:
            gate = _A2AContextGate(asyncio.Lock())
            self._context_gates[context_id] = gate
        gate.references += 1
        return gate

    def _release_context_gate(
        self, context_id: str, gate: _A2AContextGate, *, acquired: bool
    ) -> None:
        if acquired:
            gate.lock.release()
        gate.references -= 1
        if gate.references == 0 and self._context_gates.get(context_id) is gate:
            del self._context_gates[context_id]

    async def _retry_retired_clients(self) -> int:
        async with self._cleanup_lock:
            clients = tuple(self._retired_clients)
            if not clients:
                return 0
            outcomes = await asyncio.gather(
                *(client.close() for client in clients),
                return_exceptions=True,
            )
            failed = {
                client
                for client, outcome in zip(clients, outcomes, strict=True)
                if isinstance(outcome, BaseException)
            }
            self._retired_clients.difference_update(set(clients) - failed)
            return len(failed)

    def _retain_retired_client(self, client: AshClient) -> None:
        # Retention must not await: this is the last ownership handoff after a
        # failed close, and caller cancellation cannot be allowed to drop it.
        # Cleanup works from a snapshot, so clients added during a retry remain
        # in the set for the next pass.
        self._retired_clients.add(client)

    async def aclose(self) -> None:
        cleanup = asyncio.create_task(
            self._retry_retired_clients(),
            name="ash-a2a-retired-client-cleanup",
        )
        cleanup_error, cleanup_interrupted = await _settle_a2a_cleanup_task(cleanup)
        if cleanup_error is not None:
            raise cleanup_error
        failed = cleanup.result()
        if failed:
            raise RuntimeError(f"failed to close {failed} retired A2A client(s)")
        if cleanup_interrupted:
            raise asyncio.CancelledError

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        updater = TaskUpdater(event_queue, task_id, context_id)
        if context.current_task is None:
            timestamp = Timestamp()
            timestamp.FromDatetime(datetime.now(timezone.utc))
            await event_queue.enqueue_event(
                Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_SUBMITTED,
                        timestamp=timestamp,
                    ),
                    history=[context.message] if context.message is not None else [],
                )
            )
        prompt = _request_text(context)
        if not prompt:
            await updater.reject(
                _agent_message(updater, "A non-empty text prompt is required.")
            )
            return
        accepted = (
            set(context.configuration.accepted_output_modes)
            if context.configuration
            else set()
        )
        if accepted and "text/plain" not in accepted:
            await updater.reject(
                _agent_message(updater, "Ash currently returns text/plain only.")
            )
            return
        if await self._retry_retired_clients():
            await updater.failed(
                _agent_message(
                    updater,
                    "Ash runtime cleanup is incomplete; retry this task later.",
                )
            )
            return
        if not self._try_acquire_task_slot():
            await updater.failed(
                _agent_message(
                    updater,
                    "Ash is busy; retry this task later.",
                )
            )
            return

        client: AshClient | None = None
        cancellation: asyncio.CancelledError | None = None
        terminal_published = False
        gate: _A2AContextGate | None = None
        gate_acquired = False
        try:
            gate = self._retain_context_gate(context_id)
            await gate.lock.acquire()
            gate_acquired = True
            session_id = await self.registry.get(context_id)
            client = await AshClient.create(
                config=self.config,
                session_id=session_id,
                run_maintenance=False,
            )
            assert client.loop.current_session is not None
            await self.registry.bind(
                context_id,
                client.loop.current_session.session_id,
            )
            await updater.start_work()
            artifact_id = f"ash-response-{uuid4()}"
            pending = ""
            emitted = False
            fallback = ""
            failure = ""
            cancelled = False
            async for event in client.stream_prompt(
                prompt,
                user_metadata={"source": "a2a", "a2a_task_id": task_id},
            ):
                if event.type == "assistant.delta":
                    text = str(event.data.get("text", ""))
                    if text:
                        if pending:
                            await updater.add_artifact(
                                [Part(text=pending)],
                                artifact_id=artifact_id,
                                name="Ash response",
                                append=emitted,
                                last_chunk=False,
                            )
                            emitted = True
                        pending = text
                elif event.type == "turn.completed":
                    fallback = str(event.data.get("response", ""))
                elif event.type == "turn.error":
                    failure = redact_urls_in_text(
                        str(event.data.get("error", "turn failed"))
                    )
                elif event.type == "turn.cancelled":
                    cancelled = True
            if not pending and not emitted:
                pending = fallback
            if pending:
                await updater.add_artifact(
                    [Part(text=pending)],
                    artifact_id=artifact_id,
                    name="Ash response",
                    append=emitted,
                    last_chunk=True,
                )
            if cancelled:
                await updater.cancel(_agent_message(updater, "Task cancelled."))
                terminal_published = True
            elif failure:
                await updater.failed(_agent_message(updater, failure))
                terminal_published = True
            else:
                await updater.complete()
                terminal_published = True
        except asyncio.CancelledError as exc:
            cancellation = exc
            raise
        except Exception as exc:  # noqa: BLE001 - stable remote failure boundary
            await updater.failed(
                _agent_message(
                    updater,
                    redact_urls_in_text(str(exc) or "Ash task failed"),
                )
            )
            terminal_published = True
        finally:
            try:
                if client is not None:
                    close_task = asyncio.create_task(
                        client.close(), name=f"ash-a2a-client-close-{task_id}"
                    )
                    close_error, close_interrupted = await _settle_a2a_cleanup_task(
                        close_task
                    )
                    if close_error is not None:
                        self._retain_retired_client(client)
                        if cancellation is not None:
                            cancellation.add_note("A2A client closure failed")
                        elif not terminal_published:
                            raise close_error
                    if close_interrupted:
                        if cancellation is not None:
                            cancellation.add_note("A2A client closure was interrupted")
                        elif not terminal_published:
                            raise asyncio.CancelledError
            finally:
                try:
                    if gate is not None:
                        self._release_context_gate(
                            context_id, gate, acquired=gate_acquired
                        )
                finally:
                    self._release_task_slot()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(
            event_queue,
            context.task_id or "",
            context.context_id or "",
        )
        await updater.cancel(_agent_message(updater, "Task cancelled."))


def create_a2a_app(
    config: AshConfig,
    *,
    public_url: str,
    bearer_token: str,
    requests_per_minute: int = 60,
    task_store: TaskStore | None = None,
) -> Starlette:
    """Build the official A2A routes with durable task and Ash-session state."""

    base_url = _public_url(public_url)
    task_db_path = config.db_directory / "a2a_tasks.db"
    mapping_db_path = config.db_directory / "a2a_sessions.db"
    engine: AsyncEngine | None = None
    if task_store is None:
        engine = _create_a2a_task_engine(task_db_path)
        task_store = DatabaseTaskStore(engine)
    registry = A2ASessionRegistry(mapping_db_path, config.workspace_root)
    card = build_agent_card(base_url)
    executor = AshA2AExecutor(config, registry)
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=card,
    )
    context_builder = _AuthenticatedContextBuilder(config.workspace_root)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "service": "ash-a2a", "protocol_version": "1.0"}
        )

    routes = [
        Route(
            "/health",
            health,
            methods=["GET"],
        ),
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(
            handler,
            rpc_url="/a2a",
            context_builder=context_builder,
        ),
        *create_rest_routes(
            handler,
            context_builder=context_builder,
            path_prefix="/a2a",
        ),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if isinstance(task_store, DatabaseTaskStore):
            await task_store.initialize()
        primary_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_errors: list[BaseException] = []
            try:
                await handler.aclose()
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                await executor.aclose()
            except BaseException as exc:
                cleanup_errors.append(exc)
            if engine is not None:
                try:
                    await engine.dispose()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                if primary_error is not None:
                    for _error in cleanup_errors:
                        primary_error.add_note("A2A shutdown cleanup also failed")
                else:
                    primary = cleanup_errors[0]
                    for extra in cleanup_errors[1:]:
                        primary.add_note(f"additional A2A shutdown error: {extra}")
                    raise primary

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(
        A2AAuthMiddleware,
        bearer_token=bearer_token,
        requests_per_minute=requests_per_minute,
    )
    app.state.a2a_task_store = task_store
    app.state.a2a_engine = engine
    app.state.a2a_executor = executor
    return app


def build_agent_card(public_url: str) -> AgentCard:
    base_url = _public_url(public_url)
    return AgentCard(
        name="Ash",
        description="A project-scoped coding and general-purpose AI agent harness.",
        version=version("ash-ai"),
        provider=AgentProvider(
            organization="Ash Harness",
            url="https://github.com/Suraj-H675/Ash-Harness",
        ),
        supported_interfaces=[
            AgentInterface(
                url=f"{base_url}/a2a",
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            ),
            AgentInterface(
                url=f"{base_url}/a2a",
                protocol_binding=TransportProtocol.HTTP_JSON.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            ),
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        security_schemes={
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="opaque",
                    description="Bearer token supplied out of band by the operator.",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={"bearer": StringList()})],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="project-agent",
                name="Project agent",
                description=(
                    "Inspect, explain, modify, and validate the configured project using "
                    "Ash's provider, tools, skills, agents, and safety policy."
                ),
                tags=["coding", "analysis", "automation"],
                examples=["Inspect this project and fix the reported defect."],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
    )


def _request_text(context: RequestContext) -> str:
    message = context.message
    if message is None or not message.parts:
        return ""
    chunks: list[str] = []
    total_bytes = 0
    for part in message.parts:
        if part.WhichOneof("content") != "text":
            return ""
        value = part.text
        if not isinstance(value, str):
            return ""
        contribution = len(value.encode("utf-8"))
        if chunks:
            contribution += 1
        total_bytes += contribution
        if total_bytes > MAX_A2A_INPUT_BYTES:
            return ""
        chunks.append(value)
    text = "\n".join(chunks)
    return text if text.strip() else ""


def _agent_message(updater: TaskUpdater, text: str) -> Message:
    return updater.new_agent_message([Part(text=text)])


def _validate_context_id(value: str) -> None:
    if not value or len(value.encode("utf-8")) > MAX_A2A_CONTEXT_ID_BYTES:
        raise ValueError("invalid A2A context ID")


def _a2a_owner_scope(workspace: Path) -> str:
    project_key = normalize_project_path(workspace).encode("utf-8")
    return f"ash-a2a:{hashlib.sha256(project_key).hexdigest()}"


def _public_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid A2A public URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 0 < port <= 65535)
    ):
        raise ValueError(
            "A2A public URL must be an HTTP(S) origin without credentials, query, or fragment"
        )
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


async def dispose_a2a_engine(engine: AsyncEngine) -> None:
    """Small explicit hook for tests and embedded servers."""

    await engine.dispose()
