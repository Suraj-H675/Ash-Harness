from __future__ import annotations

# ruff: noqa: E402 - optional protocol dependency is checked before importing it

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

acp = pytest.importorskip("acp")

from acp import PROTOCOL_VERSION, connect_to_agent, run_agent, text_block
from acp.schema import (
    AllowedOutcome,
    EnvVariable,
    HttpHeader,
    HttpMcpServer,
    ImageContentBlock,
    McpServerStdio,
    PermissionOption,
    RequestPermissionResponse,
    ResourceContentBlock,
)

from ash.sdk import AshEvent
from config import AshConfig
from core.session import Message, SessionStore, ToolCallRecord
from server.acp import AshACPAgent


class FakeACPConnection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.permission_requests: list[tuple[str, Any, list[PermissionOption]]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[PermissionOption],
    ) -> RequestPermissionResponse:
        self.permission_requests.append((session_id, tool_call, options))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id="allow_once")
        )


class FakeAshClient:
    def __init__(
        self,
        session_id: str,
        events: list[AshEvent],
        approval_callback: Any,
    ) -> None:
        self.loop = SimpleNamespace(
            current_session=SimpleNamespace(session_id=session_id)
        )
        self.events = events
        self.approval_callback = approval_callback
        self.prompts: list[str] = []
        self.closed = False

    async def stream_prompt(self, text: str) -> AsyncIterator[AshEvent]:
        self.prompts.append(text)
        for event in self.events:
            yield event

    async def close(self) -> None:
        self.closed = True


def _events() -> list[AshEvent]:
    return [
        AshEvent("turn.started", {}),
        AshEvent("reasoning.delta", {"text": "checking"}),
        AshEvent(
            "tool.requested",
            {
                "call_id": "call-1",
                "tool": "read_file",
                "arguments": {"file_path": "README.md"},
            },
        ),
        AshEvent(
            "tool.started",
            {
                "call_id": "call-1",
                "tool": "read_file",
                "arguments": {"file_path": "README.md"},
            },
        ),
        AshEvent(
            "tool.completed",
            {
                "call_id": "call-1",
                "tool": "read_file",
                "success": True,
                "output": "content",
            },
        ),
        AshEvent("context.usage", {"current": 100, "maximum": 1000}),
        AshEvent("assistant.delta", {"text": "done"}),
        AshEvent("turn.completed", {"response": "done"}),
    ]


@pytest.mark.asyncio
async def test_acp_maps_mcp_prompts_events_and_editor_permissions(
    tmp_path: Path,
) -> None:
    created: list[tuple[FakeAshClient, dict[str, Any]]] = []

    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        client = FakeAshClient(session_id or "session-1", _events(), approval_callback)
        created.append((client, mcp_configs))
        return client

    connection = FakeACPConnection()
    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(connection)  # type: ignore[arg-type]

    initialized = await agent.initialize(PROTOCOL_VERSION)
    assert initialized.protocol_version == 1
    assert initialized.agent_capabilities is not None
    assert initialized.agent_capabilities.load_session is True
    assert initialized.agent_capabilities.mcp_capabilities is not None
    assert initialized.agent_capabilities.mcp_capabilities.http is True
    assert initialized.agent_capabilities.session_capabilities is not None
    assert initialized.agent_capabilities.session_capabilities.close is not None

    session = await agent.new_session(
        str(tmp_path),
        mcp_servers=[
            HttpMcpServer(
                type="http",
                name="docs",
                url="https://mcp.example.test/rpc",
                headers=[HttpHeader(name="Authorization", value="Bearer secret")],
            )
        ],
    )
    assert session.session_id == "session-1"
    assert created[0][1]["docs"].headers == {
        "Authorization": "Bearer secret"
    }

    with pytest.raises(acp.RequestError) as duplicate_header:
        await agent.new_session(
            str(tmp_path),
            mcp_servers=[
                HttpMcpServer(
                    type="http",
                    name="bad-headers",
                    url="https://mcp.example.test/rpc",
                    headers=[
                        HttpHeader(name="Authorization", value="one"),
                        HttpHeader(name="authorization", value="two"),
                    ],
                )
            ],
        )
    assert duplicate_header.value.data["mcpServers"] == "header names are duplicated"

    with pytest.raises(acp.RequestError) as invalid_url:
        await agent.new_session(
            str(tmp_path),
            mcp_servers=[
                HttpMcpServer(
                    type="http",
                    name="bad-url",
                    url="file:///tmp/socket",
                    headers=[],
                )
            ],
        )
    assert "HTTP(S)" in invalid_url.value.data["mcpServers"]

    with pytest.raises(acp.RequestError) as duplicate_env:
        await agent.new_session(
            str(tmp_path),
            mcp_servers=[
                McpServerStdio(
                    name="bad-env",
                    command="server",
                    args=[],
                    env=[
                        EnvVariable(name="TOKEN", value="one"),
                        EnvVariable(name="TOKEN", value="two"),
                    ],
                )
            ],
        )
    assert "env names" in duplicate_env.value.data["mcpServers"]

    response = await agent.prompt(
        session.session_id,
        [
            text_block("Inspect this"),
            ResourceContentBlock(
                type="resource_link",
                name="README",
                uri="file:///workspace/README.md",
            ),
        ],
    )
    assert response.stop_reason == "end_turn"
    assert "Inspect this" in created[0][0].prompts[0]
    assert 'uri="file:///workspace/README.md"' in created[0][0].prompts[0]
    update_types = [item.session_update for _, item in connection.updates]
    assert "agent_thought_chunk" in update_types
    assert "tool_call" in update_types
    assert "tool_call_update" in update_types
    assert "usage_update" in update_types
    assert "agent_message_chunk" in update_types

    assert await created[0][0].approval_callback(
        "run_command", {"command": "echo ok", "token": "sk-secret-value"}
    )
    permission = connection.permission_requests[0]
    assert permission[0] == session.session_id
    assert permission[1].raw_input["token"] != "sk-secret-value"
    assert [option.option_id for option in permission[2]] == [
        "allow_once",
        "reject_once",
    ]

    await agent.aclose()
    assert created[0][0].closed


@pytest.mark.asyncio
async def test_acp_cancel_returns_cancelled_stop_reason(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingAshClient(FakeAshClient):
        async def stream_prompt(self, text: str) -> AsyncIterator[AshEvent]:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            if False:
                yield AshEvent("turn.completed", {"response": ""})

    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        return BlockingAshClient(
            session_id or "cancel-session", [], approval_callback
        )

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))
    prompt = asyncio.create_task(
        agent.prompt(session.session_id, [text_block("wait")])
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    await agent.cancel(session.session_id)
    response = await asyncio.wait_for(prompt, timeout=2)

    assert response.stop_reason == "cancelled"
    assert cancelled.is_set()
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_load_replays_and_lists_durable_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AshConfig(
        model="ollama/test",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    store = SessionStore(config.db_directory / "sessions.db")
    stored = store.create_session(str(workspace), model="test")
    now = datetime.now(timezone.utc)
    store.save_message(
        stored.session_id, Message(role="user", content="hello", timestamp=now)
    )
    store.save_message(
        stored.session_id, Message(role="assistant", content="hi", timestamp=now)
    )
    store.save_tool_call(
        stored.session_id,
        ToolCallRecord(
            call_id="persisted-call",
            tool_name="read_file",
            arguments={"path": "README.md", "token": "secret"},
            approved=True,
            executed=True,
            result="contents",
            timestamp=now,
        ),
    )
    store.rename_session(stored.session_id, "Durable ACP session")
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    clients: list[FakeAshClient] = []

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        client = FakeAshClient(session_id or "new", [], approval_callback)
        clients.append(client)
        return client

    connection = FakeACPConnection()
    agent = AshACPAgent(client_factory=factory, max_sessions=1)  # type: ignore[arg-type]
    agent.on_connect(connection)  # type: ignore[arg-type]

    await agent.load_session(str(workspace), stored.session_id)
    replay_types = [update.session_update for _, update in connection.updates]
    assert replay_types == [
        "user_message_chunk",
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
    ]
    replayed_tool = connection.updates[2][1]
    assert replayed_tool.raw_input == {"path": "README.md", "token": "[REDACTED]"}
    listed = await agent.list_sessions(cwd=str(workspace))
    assert [(item.session_id, item.title) for item in listed.sessions] == [
        (stored.session_id, "Durable ACP session")
    ]
    with pytest.raises(acp.RequestError, match="session limit"):
        await agent.new_session(str(workspace))
    with pytest.raises(acp.RequestError) as duplicate:
        await agent.load_session(str(workspace), stored.session_id)
    assert duplicate.value.data["reason"] == "session already loaded"
    with pytest.raises(acp.RequestError) as unsupported:
        await agent.prompt(
            stored.session_id,
            [ImageContentBlock(type="image", data="AA==", mime_type="image/png")],
        )
    assert unsupported.value.data["prompt"] == "unsupported content type: image"

    await agent.aclose()
    assert clients[0].closed


class WireClient(FakeACPConnection):
    def on_connect(self, conn: Any) -> None:
        self.connection = conn


async def _pipe_reader(descriptor: int) -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    pipe = os.fdopen(descriptor, "rb", buffering=0)
    await loop.connect_read_pipe(lambda: protocol, pipe)
    return reader


async def _pipe_writer(descriptor: int) -> asyncio.StreamWriter:
    loop = asyncio.get_running_loop()
    pipe = os.fdopen(descriptor, "wb", buffering=0)
    transport, protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop), pipe
    )
    return asyncio.StreamWriter(transport, protocol, None, loop)


@pytest.mark.asyncio
async def test_acp_official_sdk_wire_round_trip(tmp_path: Path) -> None:
    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        return FakeAshClient(session_id or "wire-session", _events(), approval_callback)

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    client_to_agent_read, client_to_agent_write = os.pipe()
    agent_to_client_read, agent_to_client_write = os.pipe()
    agent_reader = await _pipe_reader(client_to_agent_read)
    agent_writer = await _pipe_writer(agent_to_client_write)
    client_reader = await _pipe_reader(agent_to_client_read)
    client_writer = await _pipe_writer(client_to_agent_write)
    agent_task = asyncio.create_task(
        run_agent(agent, input_stream=agent_writer, output_stream=agent_reader)
    )
    wire_client = WireClient()
    connection = connect_to_agent(wire_client, client_writer, client_reader)  # type: ignore[arg-type]
    try:
        initialized = await asyncio.wait_for(
            connection.initialize(protocol_version=PROTOCOL_VERSION), timeout=2
        )
        session = await asyncio.wait_for(
            connection.new_session(cwd=str(tmp_path), mcp_servers=[]), timeout=2
        )
        response = await asyncio.wait_for(
            connection.prompt(
                session_id=session.session_id,
                prompt=[text_block("wire prompt")],
            ),
            timeout=2,
        )

        assert initialized.protocol_version == 1
        assert session.session_id == "wire-session"
        assert response.stop_reason == "end_turn"
        assert any(
            update.session_update == "agent_message_chunk"
            for _, update in wire_client.updates
        )
    finally:
        client_writer.close()
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.TimeoutError, ConnectionError):
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
        agent_writer.close()
        await asyncio.sleep(0)
        await connection.close()
        await agent.aclose()
