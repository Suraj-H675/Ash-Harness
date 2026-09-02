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
    TaskState,
)
from a2a.utils.constants import TransportProtocol

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
from ash.server.a2a import A2ASessionRegistry, create_a2a_app


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
        public_url="http://testserver",
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
                    url="http://testserver",
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
