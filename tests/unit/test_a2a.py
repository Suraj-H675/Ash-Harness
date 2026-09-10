from __future__ import annotations

# ruff: noqa: E402 - optional protocol dependency is checked before importing it

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

a2a = pytest.importorskip("a2a")
pytestmark = pytest.mark.filterwarnings(
    "ignore:label\\(\\) is deprecated.*:DeprecationWarning"
)

from a2a.client import ClientConfig, ClientFactory
from a2a.server.tasks import InMemoryTaskStore
from a2a.types.a2a_pb2 import (
    CancelTaskRequest,
    AgentInterface,
    GetTaskRequest,
    ListTasksRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskStatus,
    TaskState,
)
from a2a.utils.constants import TransportProtocol
from a2a.utils.errors import TaskNotFoundError

from ash.sdk import AshEvent
from ash.agents.a2a_remote import (
    ListRemoteAgentsTool,
    RemoteAgentConfig,
    load_remote_agent_configs,
    send_remote_agent,
    validate_agent_card_origins,
)
from ash.config import AshConfig
from ash.safety.guard import SafetyGuard
from ash.server.a2a import (
    MAX_A2A_BODY_BYTES,
    MAX_A2A_INPUT_BYTES,
    A2ASessionRegistry,
    _create_a2a_task_engine,
    _request_text,
    create_a2a_app,
)


class FakeAshClient:
    def __init__(
        self, session_id: str, prompts: list[tuple[str, dict[str, Any]]]
    ) -> None:
        self.loop = SimpleNamespace(
            current_session=SimpleNamespace(session_id=session_id)
        )
        self.prompts = prompts
        self.closed = False

    async def stream_prompt(
        self, prompt: str, *, user_metadata: dict[str, Any] | None = None
    ) -> AsyncIterator[AshEvent]:
        self.prompts.append((prompt, user_metadata or {}))
        yield AshEvent("assistant.delta", {"text": "hel"})
        yield AshEvent("assistant.delta", {"text": "lo"})
        yield AshEvent("turn.completed", {"response": "hello"})

    async def close(self) -> None:
        self.closed = True


class _RemoteTaskEvent:
    def __init__(self, observed: asyncio.Event) -> None:
        self._observed = observed
        self._task = Task(
            id="remote-task",
            context_id="remote-context",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )

    def HasField(self, field: str) -> bool:
        return field == "task"

    @property
    def task(self) -> Task:
        self._observed.set()
        return self._task


class _BlockingRemoteClient:
    def __init__(
        self,
        *,
        emit_task: bool = True,
        cancel_error: BaseException | None = None,
    ) -> None:
        self.emit_task = emit_task
        self.cancel_error = cancel_error
        self.send_started = asyncio.Event()
        self.task_observed = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.cancel_finished = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.close_release = asyncio.Event()
        self.cancel_calls: list[str] = []
        self.cancel_task_instance: asyncio.Task[Any] | None = None
        self.close_task_instance: asyncio.Task[Any] | None = None

    async def send_message(self, _request: SendMessageRequest):
        self.send_started.set()
        if self.emit_task:
            yield _RemoteTaskEvent(self.task_observed)
        await self.cancel_release.wait()

    async def cancel_task(self, request: CancelTaskRequest) -> None:
        self.cancel_task_instance = asyncio.current_task()
        self.cancel_calls.append(request.id)
        self.cancel_started.set()
        await self.cancel_release.wait()
        self.cancel_finished.set()
        if self.cancel_error is not None:
            raise self.cancel_error

    async def close(self) -> None:
        self.close_task_instance = asyncio.current_task()
        self.close_started.set()
        await self.close_release.wait()
        self.close_finished.set()


def _patch_remote_client(monkeypatch, client: _BlockingRemoteClient) -> None:
    from a2a import client as a2a_client

    class FakeResolver:
        def __init__(self, _http: Any, _url: str) -> None:
            pass

        async def get_agent_card(self) -> Any:
            return SimpleNamespace(
                supported_interfaces=[
                    AgentInterface(
                        url="https://example.test", protocol_binding="JSONRPC"
                    )
                ]
            )

    class FakeConfig:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    class FakeFactory:
        def __init__(self, _config: Any) -> None:
            pass

        def create(self, _card: Any) -> _BlockingRemoteClient:
            return client

    monkeypatch.setattr(a2a_client, "A2ACardResolver", FakeResolver)
    monkeypatch.setattr(a2a_client, "ClientConfig", FakeConfig)
    monkeypatch.setattr(a2a_client, "ClientFactory", FakeFactory)


async def _start_blocking_remote_call(
    client: _BlockingRemoteClient,
) -> asyncio.Task[Any]:
    call = asyncio.create_task(
        send_remote_agent(
            RemoteAgentConfig(name="remote", url="https://example.test"),
            "prompt",
        )
    )
    await asyncio.wait_for(client.task_observed.wait(), timeout=1)
    return call


@pytest.mark.asyncio
async def test_a2a_remote_repeated_cancellation_owns_remote_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _BlockingRemoteClient()
    _patch_remote_client(monkeypatch, client)
    call = await _start_blocking_remote_call(client)

    call.cancel()
    await asyncio.wait_for(client.cancel_started.wait(), timeout=1)
    call.cancel()
    await asyncio.sleep(0)

    assert not client.close_started.is_set()
    assert not client.close_finished.is_set()
    assert client.cancel_calls == ["remote-task"]

    client.cancel_release.set()
    await asyncio.wait_for(client.cancel_finished.wait(), timeout=1)
    await asyncio.wait_for(client.close_started.wait(), timeout=1)
    assert client.cancel_task_instance is not None
    assert client.cancel_task_instance.done()

    client.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, timeout=1)

    assert client.close_finished.is_set()
    assert client.close_task_instance is not None
    assert client.close_task_instance.done()


@pytest.mark.asyncio
async def test_a2a_remote_cancellation_failure_still_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _BlockingRemoteClient(cancel_error=RuntimeError("remote cancellation failed"))
    _patch_remote_client(monkeypatch, client)
    call = await _start_blocking_remote_call(client)

    call.cancel()
    await asyncio.wait_for(client.cancel_started.wait(), timeout=1)
    client.cancel_release.set()
    await asyncio.wait_for(client.cancel_finished.wait(), timeout=1)
    await asyncio.wait_for(client.close_started.wait(), timeout=1)
    assert client.cancel_task_instance is not None
    assert client.cancel_task_instance.done()

    client.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, timeout=1)

    assert client.close_finished.is_set()
    assert client.close_task_instance is not None
    assert client.close_task_instance.done()


@pytest.mark.asyncio
async def test_a2a_remote_repeated_cancellation_owns_client_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _BlockingRemoteClient()
    _patch_remote_client(monkeypatch, client)
    call = await _start_blocking_remote_call(client)

    call.cancel()
    await asyncio.wait_for(client.cancel_started.wait(), timeout=1)
    client.cancel_release.set()
    await asyncio.wait_for(client.cancel_finished.wait(), timeout=1)
    await asyncio.wait_for(client.close_started.wait(), timeout=1)

    call.cancel()
    call.cancel()
    await asyncio.sleep(0)
    assert not client.close_finished.is_set()

    client.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, timeout=1)

    assert client.close_finished.is_set()
    assert client.close_task_instance is not None
    assert client.close_task_instance.done()


@pytest.mark.asyncio
async def test_a2a_remote_cancellation_before_task_id_only_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _BlockingRemoteClient(emit_task=False)
    _patch_remote_client(monkeypatch, client)
    call = asyncio.create_task(
        send_remote_agent(
            RemoteAgentConfig(name="remote", url="https://example.test"),
            "prompt",
        )
    )
    await asyncio.wait_for(client.send_started.wait(), timeout=1)

    call.cancel()
    await asyncio.wait_for(client.close_started.wait(), timeout=1)
    call.cancel()
    await asyncio.sleep(0)
    assert client.cancel_calls == []
    assert not client.close_finished.is_set()

    client.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, timeout=1)

    assert client.close_finished.is_set()
    assert client.close_task_instance is not None
    assert client.close_task_instance.done()


def test_a2a_registry_rejects_linked_database_file_and_parent(tmp_path: Path) -> None:
    import sqlite3

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target.db"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
    linked_file = tmp_path / "a2a_sessions.db"
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-db"
    try:
        linked_file.symlink_to(target)
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    for database in (linked_file, linked_parent / "a2a_sessions.db"):
        with pytest.raises(ValueError, match="symlink or junction"):
            A2ASessionRegistry(database, workspace)

    assert not (outside / "a2a_sessions.db").exists()
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='ash_context_sessions'"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a2a_task_engine_rechecks_link_before_lazy_connect(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "a2a_tasks.db"
    target = tmp_path / "target.db"
    engine = _create_a2a_task_engine(database)
    try:
        with sqlite3.connect(target) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
        try:
            database.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlinks are unavailable: {exc}")

        with pytest.raises(ValueError, match="symlink or junction"):
            async with engine.begin():
                pass
    finally:
        await engine.dispose()

    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()[0] == 0


def test_a2a_default_task_store_rejects_preexisting_linked_database(
    tmp_path: Path,
) -> None:
    import sqlite3

    workspace = tmp_path / "workspace"
    database_dir = tmp_path / "db"
    workspace.mkdir()
    database_dir.mkdir()
    target = tmp_path / "target.db"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
    try:
        (database_dir / "a2a_tasks.db").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    config = AshConfig(
        model="ollama/test",
        workspace_root=workspace,
        db_directory=database_dir,
        memory_backend="off",
    )

    with pytest.raises(ValueError, match="symlink or junction"):
        create_a2a_app(
            config,
            public_url="https://testserver",
            bearer_token="0123456789abcdef",
        )


def test_a2a_request_text_stops_reading_parts_after_input_limit() -> None:
    accesses = 0

    class TextPart:
        def __init__(self, value: str | None = None) -> None:
            self.value = value

        def WhichOneof(self, name: str) -> str:
            assert name == "content"
            return "text"

        @property
        def text(self) -> str:
            nonlocal accesses
            accesses += 1
            if self.value is None:
                raise AssertionError("request parser read past the byte limit")
            return self.value

    context = SimpleNamespace(
        message=SimpleNamespace(
            parts=[
                TextPart("x" * (MAX_A2A_INPUT_BYTES + 1)),
                TextPart(),
            ]
        )
    )

    assert _request_text(context) == ""
    assert accesses == 1


@pytest.mark.asyncio
async def test_a2a_rejects_oversized_body_without_reading_remainder(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AshConfig(
        model="ollama/test",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    app = create_a2a_app(
        config,
        public_url="https://testserver",
        bearer_token="0123456789abcdef",
        requests_per_minute=100,
        task_store=InMemoryTaskStore(),
    )
    consumed: list[int] = []
    chunk_size = MAX_A2A_BODY_BYTES // 2 + 1

    async def oversized_body() -> AsyncIterator[bytes]:
        for index in range(3):
            consumed.append(index)
            yield b"x" * chunk_size

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/a2a",
            content=oversized_body(),
            headers={
                "Authorization": "Bearer 0123456789abcdef",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body exceeds the A2A server limit"}
    assert consumed == [0, 1]


@pytest.mark.asyncio
async def test_a2a_rejects_oversized_content_length_without_reading_body(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AshConfig(
        model="ollama/test",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    app = create_a2a_app(
        config,
        public_url="https://testserver",
        bearer_token="0123456789abcdef",
        requests_per_minute=100,
        task_store=InMemoryTaskStore(),
    )
    consumed: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        consumed.append(0)
        yield b"{}"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/a2a",
            content=body(),
            headers={
                "Authorization": "Bearer 0123456789abcdef",
                "Content-Type": "application/json",
                "Content-Length": str(MAX_A2A_BODY_BYTES + 1),
            },
        )

    assert response.status_code == 413
    assert consumed == []


@pytest.mark.asyncio
async def test_a2a_official_client_streams_and_resumes_durable_context(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AshConfig(
        model="ollama/test",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    prompts: list[tuple[str, dict[str, Any]]] = []
    created: list[tuple[str | None, FakeAshClient]] = []

    async def create_client(**kwargs: Any) -> FakeAshClient:
        requested = kwargs.get("session_id")
        client = FakeAshClient(requested or "ash-session-1", prompts)
        created.append((requested, client))
        return client

    monkeypatch.setattr("ash.server.a2a.AshClient.create", create_client)
    app = create_a2a_app(
        config,
        public_url="https://testserver",
        bearer_token="0123456789abcdef",
        requests_per_minute=100,
        task_store=InMemoryTaskStore(),
    )
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as anonymous:
            card_response = await anonymous.get("/.well-known/agent-card.json")
            assert card_response.status_code == 200
            card = card_response.json()
            assert card["capabilities"]["streaming"] is True
            assert (
                card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"]
                == "bearer"
            )
            denied = await anonymous.post("/a2a", json={})
            assert denied.status_code == 401
            duplicate_auth = await anonymous.post(
                "/a2a",
                json={},
                headers=[
                    ("Authorization", "Bearer 0123456789abcdef"),
                    ("Authorization", "Bearer 0123456789abcdef"),
                ],
            )
            assert duplicate_auth.status_code == 401

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer 0123456789abcdef"},
        ) as http:
            factory = ClientFactory(
                ClientConfig(
                    httpx_client=http,
                    streaming=True,
                    supported_protocol_bindings=[TransportProtocol.JSONRPC],
                    accepted_output_modes=["text/plain"],
                )
            )
            client = await factory.create_from_url("http://testserver")
            first_request = SendMessageRequest(
                message=Message(
                    message_id="message-1",
                    context_id="context-1",
                    role=Role.ROLE_USER,
                    parts=[Part(text="say hello")],
                )
            )
            async with asyncio.timeout(5):
                first_events = [
                    event async for event in client.send_message(first_request)
                ]

            assert any(event.HasField("artifact_update") for event in first_events)
            terminal = [
                event.status_update.status.state
                for event in first_events
                if event.HasField("status_update")
            ]
            assert terminal[-1] == TaskState.TASK_STATE_COMPLETED
            task_id = next(
                event.status_update.task_id
                for event in first_events
                if event.HasField("status_update")
            )
            artifact_text = "".join(
                part.text
                for event in first_events
                if event.HasField("artifact_update")
                for part in event.artifact_update.artifact.parts
            )
            assert artifact_text == "hello"
            stored = await client.get_task(GetTaskRequest(id=task_id))
            assert stored.status.state == TaskState.TASK_STATE_COMPLETED
            listed = await client.list_tasks(ListTasksRequest(page_size=10))
            assert task_id in {task.id for task in listed.tasks}

            second_request = SendMessageRequest(
                message=Message(
                    message_id="message-2",
                    context_id="context-1",
                    role=Role.ROLE_USER,
                    parts=[Part(text="continue")],
                )
            )
            async with asyncio.timeout(5):
                second_events = [
                    event async for event in client.send_message(second_request)
                ]
            assert any(event.HasField("artifact_update") for event in second_events)

            monkeypatch.setenv("REMOTE_A2A_TOKEN", "0123456789abcdef")
            delegated = await send_remote_agent(
                RemoteAgentConfig(
                    name="local",
                    url="https://testserver",
                    token_env="REMOTE_A2A_TOKEN",
                ),
                "delegated task",
                context_id="delegated-context",
                transport=transport,
            )
            assert delegated.response == "hello"
            assert delegated.context_id == "delegated-context"
            assert delegated.state == "TASK_STATE_COMPLETED"

            unsupported = SendMessageRequest(
                message=Message(
                    message_id="message-unsupported",
                    role=Role.ROLE_USER,
                    parts=[Part(raw=b"data", media_type="application/octet-stream")],
                )
            )
            async with asyncio.timeout(5):
                unsupported_events = [
                    event async for event in client.send_message(unsupported)
                ]
            assert any(
                event.HasField("status_update")
                and event.status_update.status.state == TaskState.TASK_STATE_REJECTED
                for event in unsupported_events
            )

            rest_client = await ClientFactory(
                ClientConfig(
                    httpx_client=http,
                    streaming=True,
                    supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                    use_client_preference=True,
                    accepted_output_modes=["text/plain"],
                )
            ).create_from_url("http://testserver")
            rest_events = [
                event
                async for event in rest_client.send_message(
                    SendMessageRequest(
                        message=Message(
                            message_id="rest-message",
                            role=Role.ROLE_USER,
                            parts=[Part(text="REST task")],
                        )
                    )
                )
            ]
            assert any(
                event.HasField("status_update")
                and event.status_update.status.state == TaskState.TASK_STATE_COMPLETED
                for event in rest_events
            )
            await rest_client.close()

    assert [requested for requested, _ in created] == [
        None,
        "ash-session-1",
        None,
        None,
    ]
    assert prompts == [
        ("say hello", {"source": "a2a", "a2a_task_id": task_id}),
        (
            "continue",
            {
                "source": "a2a",
                "a2a_task_id": next(
                    event.status_update.task_id
                    for event in second_events
                    if event.HasField("status_update")
                ),
            },
        ),
        (
            "delegated task",
            {
                "source": "a2a",
                "a2a_task_id": delegated.task_id,
            },
        ),
        (
            "REST task",
            {
                "source": "a2a",
                "a2a_task_id": next(
                    event.status_update.task_id
                    for event in rest_events
                    if event.HasField("status_update")
                ),
            },
        ),
    ]
    assert all(client.closed for _, client in created)


def test_a2a_remote_config_respects_trust_and_rejects_duplicates(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    user_config = home / ".ash" / "a2a.json"
    project_config = workspace / ".ash" / "a2a.json"
    user_config.parent.mkdir(parents=True)
    project_config.parent.mkdir(parents=True)
    user_config.write_text(
        '{"agents":{"review":{"url":"https://review.example.com",'
        '"description":"Remote reviewer","token_env":"REVIEW_TOKEN"}}}',
        encoding="utf-8",
    )
    project_config.write_text(
        '{"agents":{"review":{"url":"https://other.example.com"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    agents = load_remote_agent_configs(workspace, include_project=False)
    assert agents["review"].token_env == "REVIEW_TOKEN"
    with pytest.raises(ValueError, match="duplicate A2A agent name"):
        load_remote_agent_configs(workspace, include_project=True)
    with pytest.raises(ValueError, match="changed origin"):
        validate_agent_card_origins(
            "https://review.example.com",
            [
                AgentInterface(
                    url="https://attacker.example/a2a",
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                )
            ],
        )


def test_a2a_remote_config_rejects_duplicate_json_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    path = home / ".ash" / "a2a.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"agents":{"review":{"url":"https://first.example"},'
        '"review":{"url":"https://second.example"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        load_remote_agent_configs(tmp_path / "workspace", include_project=False)


def test_a2a_remote_config_rejects_plaintext_remote_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config_path = home / ".ash" / "a2a.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"agents":{"review":{"url":"http://agent.example.com",'
        '"token_env":"REVIEW_TOKEN"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="must use HTTPS"):
        load_remote_agent_configs(tmp_path / "workspace", include_project=False)


def test_a2a_remote_config_allows_loopback_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config_path = home / ".ash" / "a2a.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"agents":{"local":{"url":"http://127.0.0.1:8765"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    agents = load_remote_agent_configs(tmp_path / "workspace", include_project=False)
    assert agents["local"].url == "http://127.0.0.1:8765"


def test_a2a_remote_config_does_not_follow_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "outside-a2a.json"
    target.write_text('{"agents": {}}', encoding="utf-8")
    home = tmp_path / "home"
    config_path = home / ".ash" / "a2a.json"
    config_path.parent.mkdir(parents=True)
    try:
        config_path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="symlinked A2A config"):
        load_remote_agent_configs(tmp_path / "workspace", include_project=False)


def test_project_a2a_config_rejects_symlinked_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    home.mkdir()
    workspace.mkdir()
    outside.mkdir()
    (outside / "a2a.json").write_text(
        '{"agents":{"escaped":{"url":"https://agent.example"}}}',
        encoding="utf-8",
    )
    try:
        (workspace / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="symlink or junction"):
        load_remote_agent_configs(workspace, include_project=True)


@pytest.mark.asyncio
async def test_remote_agent_inventory_hides_private_endpoint(tmp_path: Path) -> None:
    tool = ListRemoteAgentsTool(
        SafetyGuard(tmp_path),
        {
            "private": RemoteAgentConfig(
                name="private",
                url="https://internal-agent.example.com",
                description="Private reviewer",
            )
        },
    )

    result = await tool.run()

    assert result.success
    assert "private" in result.output
    assert "internal-agent.example.com" not in result.output


@pytest.mark.asyncio
async def test_a2a_rate_limits_authenticated_operations(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AshConfig(
        model="ollama/test",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    app = create_a2a_app(
        config,
        public_url="http://testserver",
        bearer_token="0123456789abcdef",
        requests_per_minute=1,
        task_store=InMemoryTaskStore(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer 0123456789abcdef"},
        ) as client:
            first = await client.post("/a2a", json={})
            second = await client.post("/a2a", json={})
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"


@pytest.mark.asyncio
async def test_a2a_registry_rejects_cross_workspace_context(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    db_path = tmp_path / "a2a.db"
    registry = A2ASessionRegistry(db_path, first)
    await registry.bind("context", "session")

    with pytest.raises(ValueError, match="different workspace"):
        await A2ASessionRegistry(db_path, second).get("context")


@pytest.mark.asyncio
async def test_a2a_default_task_store_isolates_workspaces(
    tmp_path: Path, monkeypatch
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    database = tmp_path / "db"
    base_config = AshConfig(
        model="ollama/test",
        workspace_root=first_workspace,
        db_directory=database,
        memory_backend="off",
    )

    async def create_client(**kwargs: Any) -> FakeAshClient:
        requested = kwargs.get("session_id")
        return FakeAshClient(requested or "first-session", [])

    monkeypatch.setattr("ash.server.a2a.AshClient.create", create_client)
    first_app = create_a2a_app(
        base_config,
        public_url="http://first.test",
        bearer_token="first-token-0001",
        requests_per_minute=100,
    )
    second_app = create_a2a_app(
        base_config.model_copy(update={"workspace_root": second_workspace}),
        public_url="http://second.test",
        bearer_token="second-token-001",
        requests_per_minute=100,
    )

    async with first_app.router.lifespan_context(first_app):
        async with second_app.router.lifespan_context(second_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=first_app),
                base_url="http://first.test",
                headers={"Authorization": "Bearer first-token-0001"},
            ) as first_http:
                first_client = await ClientFactory(
                    ClientConfig(
                        httpx_client=first_http,
                        streaming=True,
                        supported_protocol_bindings=[TransportProtocol.JSONRPC],
                        accepted_output_modes=["text/plain"],
                    )
                ).create_from_url("http://first.test")
                events = [
                    event
                    async for event in first_client.send_message(
                        SendMessageRequest(
                            message=Message(
                                message_id="first-message",
                                context_id="first-context",
                                role=Role.ROLE_USER,
                                parts=[Part(text="private first-workspace task")],
                            )
                        )
                    )
                ]
                task_id = next(
                    event.status_update.task_id
                    for event in events
                    if event.HasField("status_update")
                )
                stored = await first_client.get_task(GetTaskRequest(id=task_id))
                assert stored.id == task_id

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second_app),
                base_url="http://second.test",
                headers={"Authorization": "Bearer second-token-001"},
            ) as second_http:
                second_client = await ClientFactory(
                    ClientConfig(
                        httpx_client=second_http,
                        streaming=True,
                        supported_protocol_bindings=[TransportProtocol.JSONRPC],
                        accepted_output_modes=["text/plain"],
                    )
                ).create_from_url("http://second.test")
                with pytest.raises(TaskNotFoundError):
                    await second_client.get_task(GetTaskRequest(id=task_id))
                listed = await second_client.list_tasks(ListTasksRequest(page_size=10))

            assert task_id not in {task.id for task in listed.tasks}


@pytest.mark.asyncio
async def test_a2a_cancel_preempts_active_ash_turn(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AshConfig(
        model="ollama/test",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    started = asyncio.Event()
    closed = asyncio.Event()

    class BlockingAshClient:
        loop = SimpleNamespace(
            current_session=SimpleNamespace(session_id="blocking-session")
        )

        async def stream_prompt(
            self, *args: Any, **kwargs: Any
        ) -> AsyncIterator[AshEvent]:
            started.set()
            yield AshEvent("assistant.delta", {"text": "partial"})
            await asyncio.Event().wait()

        async def close(self) -> None:
            closed.set()

    async def create_client(**kwargs: Any) -> BlockingAshClient:
        return BlockingAshClient()

    monkeypatch.setattr("ash.server.a2a.AshClient.create", create_client)
    app = create_a2a_app(
        config,
        public_url="http://testserver",
        bearer_token="0123456789abcdef",
        task_store=InMemoryTaskStore(),
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer 0123456789abcdef"},
        ) as http:
            client = await ClientFactory(
                ClientConfig(
                    httpx_client=http,
                    streaming=False,
                    polling=True,
                    supported_protocol_bindings=[TransportProtocol.JSONRPC],
                    accepted_output_modes=["text/plain"],
                )
            ).create_from_url("http://testserver")
            response = [
                event
                async for event in client.send_message(
                    SendMessageRequest(
                        message=Message(
                            message_id="blocking-message",
                            role=Role.ROLE_USER,
                            parts=[Part(text="wait")],
                        )
                    )
                )
            ][0]
            assert response.HasField("task")
            await asyncio.wait_for(started.wait(), timeout=2)
            cancelled = await client.cancel_task(CancelTaskRequest(id=response.task.id))
            assert cancelled.status.state == TaskState.TASK_STATE_CANCELED
            await asyncio.wait_for(closed.wait(), timeout=2)
            await client.close()
