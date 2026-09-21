from __future__ import annotations

# ruff: noqa: E402 - optional protocol dependency is checked before importing it

import asyncio
import json
import os
import sys
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

acp = pytest.importorskip("acp")

from acp import PROTOCOL_VERSION, connect_to_agent, run_agent, text_block
from acp.schema import (
    AllowedOutcome,
    AudioContentBlock,
    EnvVariable,
    HttpHeader,
    HttpMcpServer,
    ImageContentBlock,
    McpServerStdio,
    PermissionOption,
    RequestPermissionResponse,
    ResourceContentBlock,
)

from ash.sdk import AshClient, AshEvent
from ash.config import AshConfig
from ash.core.session import Message, SessionStore, ToolCallRecord
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.trust import set_workspace_trusted
from ash.server.acp import AshACPAgent, _tool_kind


def test_acp_classifies_core_read_tool_names() -> None:
    assert _tool_kind("list_dir") == "read"
    # Preserve historical replay/display compatibility if an older event used
    # the pre-runtime alias.
    assert _tool_kind("list_directory") == "read"


def test_acp_classifies_browser_tab_tools_by_effect() -> None:
    assert _tool_kind("browser_tabs") == "read"
    assert _tool_kind("browser_open_tab") == "fetch"
    assert _tool_kind("browser_focus_tab") == "other"
    assert _tool_kind("browser_close_tab") == "other"


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
            current_session=SimpleNamespace(session_id=session_id),
            provider=SimpleNamespace(capabilities=SimpleNamespace(vision=True)),
        )
        self.events = events
        self.approval_callback = approval_callback
        self.prompts: list[str] = []
        self.prompt_metadata: list[dict[str, Any] | None] = []
        self.closed = False

    async def stream_prompt(
        self,
        text: str,
        *,
        user_metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[AshEvent]:
        self.prompts.append(text)
        self.prompt_metadata.append(user_metadata)
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
    assert initialized.agent_capabilities.prompt_capabilities is not None
    assert initialized.agent_capabilities.prompt_capabilities.image is True
    assert initialized.agent_capabilities.prompt_capabilities.audio is False
    assert initialized.agent_capabilities.prompt_capabilities.embedded_context is False
    assert initialized.agent_capabilities.mcp_capabilities is not None
    assert initialized.agent_capabilities.mcp_capabilities.http is True
    assert initialized.agent_capabilities.session_capabilities is not None
    assert initialized.agent_capabilities.session_capabilities.close is not None
    assert initialized.agent_capabilities.session_capabilities.fork is not None
    assert initialized.agent_capabilities.session_capabilities.resume is not None

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
    assert created[0][1]["docs"].headers == {"Authorization": "Bearer secret"}

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
async def test_acp_forwards_inline_images_as_ephemeral_ash_metadata(
    tmp_path: Path,
) -> None:
    clients: list[FakeAshClient] = []

    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        client = FakeAshClient(session_id or "image-session", _events(), approval_callback)
        clients.append(client)
        return client

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))

    response = await agent.prompt(
        session.session_id,
        [
            text_block("Before image"),
            ImageContentBlock(type="image", data="YWJj", mime_type="image/png"),
            text_block("After image"),
        ],
    )

    assert response.stop_reason == "end_turn"
    assert clients[0].prompts == ["Before image\n\nAfter image"]
    metadata = clients[0].prompt_metadata[0]
    assert metadata is not None
    assert metadata["content_blocks"] == [
        {"type": "text", "text": "Before image"},
        {"type": "image", "media_type": "image/png", "data": "YWJj"},
        {"type": "text", "text": "After image"},
    ]
    assert metadata["images"][0]["media_type"] == "image/png"
    assert metadata["images"][0]["sha256"]
    assert "data" not in metadata["images"][0]

    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_accepts_image_only_prompt(tmp_path: Path) -> None:
    clients: list[FakeAshClient] = []

    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        client = FakeAshClient(session_id or "image-only", _events(), approval_callback)
        clients.append(client)
        return client

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))

    response = await agent.prompt(
        session.session_id,
        [ImageContentBlock(type="image", data="YWJj", mime_type="image/png")],
    )

    assert response.stop_reason == "end_turn"
    assert clients[0].prompts == [""]
    assert clients[0].prompt_metadata[0]["content_blocks"] == [
        {"type": "image", "media_type": "image/png", "data": "YWJj"}
    ]
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_image_prompt_requires_active_model_vision(tmp_path: Path) -> None:
    class NoVisionProvider(ProviderABC):
        model_name = "no-vision"

        def __init__(self) -> None:
            self.calls = 0

        def count_tokens(self, text: str) -> int:
            return len(text)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            raise AssertionError("non-vision image prompt must fail before provider I/O")
            yield StreamChunk(is_done=True)  # pragma: no cover

    provider = NoVisionProvider()
    config = AshConfig(
        model="custom/no-vision",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )

    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        return await AshClient.create(
            config=config,
            workspace=workspace,
            provider=provider,
            approval_callback=approval_callback,
            workspace_trusted=True,
            session_id=session_id,
            additional_mcp_configs=mcp_configs,
            run_maintenance=False,
        )

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))

    with pytest.raises(acp.RequestError) as unsupported:
        await agent.prompt(
            session.session_id,
            [ImageContentBlock(type="image", data="YWJj", mime_type="image/png")],
        )

    assert "vision" in unsupported.value.data["message"].casefold()
    assert provider.calls == 0
    await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "mime_type", "message"),
    [
        ("secret-not-base64", "image/png", "base64"),
        ("YWJj", "image/svg+xml", "media type"),
    ],
)
async def test_acp_rejects_invalid_inline_images(
    tmp_path: Path,
    data: str,
    mime_type: str,
    message: str,
) -> None:
    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        return FakeAshClient(session_id or "invalid-image", _events(), approval_callback)

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))
    with pytest.raises(acp.RequestError) as invalid:
        await agent.prompt(
            session.session_id,
            [ImageContentBlock(type="image", data=data, mime_type=mime_type)],
        )
    assert message in str(invalid.value.data).casefold()
    assert data not in str(invalid.value.data)
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_rejects_oversized_inline_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[FakeAshClient] = []

    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        client = FakeAshClient(session_id or "oversized", _events(), approval_callback)
        clients.append(client)
        return client

    monkeypatch.setattr("ash.server.acp.MAX_ACP_IMAGE_BYTES", 2, raising=False)
    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))

    with pytest.raises(acp.RequestError) as oversized:
        await agent.prompt(
            session.session_id,
            [ImageContentBlock(type="image", data="YWJj", mime_type="image/png")],
        )
    assert "image" in oversized.value.data["prompt"].casefold()
    assert "large" in oversized.value.data["prompt"].casefold()
    assert clients[0].prompts == []
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_rejects_inline_images_over_total_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[FakeAshClient] = []

    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        client = FakeAshClient(session_id or "total-limit", _events(), approval_callback)
        clients.append(client)
        return client

    monkeypatch.setattr("ash.server.acp.MAX_ACP_TOTAL_IMAGE_BYTES", 5)
    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))

    with pytest.raises(acp.RequestError) as oversized:
        await agent.prompt(
            session.session_id,
            [
                ImageContentBlock(type="image", data="YWJj", mime_type="image/png"),
                ImageContentBlock(type="image", data="ZGVm", mime_type="image/png"),
            ],
        )
    assert "total" in oversized.value.data["prompt"].casefold()
    assert clients[0].prompts == []
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_contains_downstream_mcp_config_validation_errors(
    tmp_path: Path,
) -> None:
    agent = AshACPAgent()

    with pytest.raises(acp.RequestError) as invalid_fragment:
        await agent.new_session(
            str(tmp_path),
            mcp_servers=[
                HttpMcpServer(
                    type="http",
                    name="fragment-url",
                    url="https://mcp.example.test/rpc#fragment",
                    headers=[],
                )
            ],
        )

    assert "mcpServers" in invalid_fragment.value.data


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
        return BlockingAshClient(session_id or "cancel-session", [], approval_callback)

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))
    prompt = asyncio.create_task(agent.prompt(session.session_id, [text_block("wait")]))
    await asyncio.wait_for(started.wait(), timeout=2)

    await agent.cancel(session.session_id)
    response = await asyncio.wait_for(prompt, timeout=2)

    assert response.stop_reason == "cancelled"
    assert cancelled.is_set()
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_close_cancels_active_prompt_and_closes_client(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    clients: list[FakeAshClient] = []

    class BlockingAshClient(FakeAshClient):
        async def stream_prompt(self, text: str) -> AsyncIterator[AshEvent]:
            self.prompts.append(text)
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
        client = BlockingAshClient(
            session_id or "close-session", [], approval_callback
        )
        clients.append(client)
        return client

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))
    prompt = asyncio.create_task(agent.prompt(session.session_id, [text_block("wait")]))
    await asyncio.wait_for(started.wait(), timeout=2)

    await agent.close_session(session.session_id)
    response = await asyncio.wait_for(prompt, timeout=2)

    assert response.stop_reason == "cancelled"
    assert cancelled.is_set()
    assert clients[0].closed is True
    with pytest.raises(acp.RequestError) as missing:
        await agent.close_session(session.session_id)
    assert missing.value.code == -32002


@pytest.mark.asyncio
async def test_acp_prompt_cannot_start_after_concurrent_session_close(
    tmp_path: Path,
) -> None:
    lookup_finished = asyncio.Event()
    release_lookup = asyncio.Event()

    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        return FakeAshClient(
            session_id or "close-race-session",
            [AshEvent("turn.completed", {"response": "should not run"})],
            approval_callback,
        )

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))
    original_session = agent._session

    async def delayed_session(session_id: str):
        state = await original_session(session_id)
        lookup_finished.set()
        await release_lookup.wait()
        return state

    agent._session = delayed_session  # type: ignore[method-assign]
    prompt_task = asyncio.create_task(
        agent.prompt(session.session_id, [text_block("race close")])
    )
    await asyncio.wait_for(lookup_finished.wait(), timeout=2)

    await agent.close_session(session.session_id)
    release_lookup.set()

    with pytest.raises(acp.RequestError):
        await asyncio.wait_for(prompt_task, timeout=2)


@pytest.mark.asyncio
async def test_acp_new_session_cancellation_closes_created_client(
    tmp_path: Path,
) -> None:
    factory_started = asyncio.Event()
    release_factory = asyncio.Event()
    clients: list[FakeAshClient] = []

    async def factory(
        workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        client = FakeAshClient("cancelled-new-session", [], approval_callback)
        clients.append(client)
        factory_started.set()
        await release_factory.wait()
        return client

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    task = asyncio.create_task(agent.new_session(str(tmp_path)))
    await asyncio.wait_for(factory_started.wait(), timeout=2)
    await agent._lock.acquire()
    try:
        release_factory.set()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        agent._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert clients[0].closed is True


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
    for malformed_cursor in (
        "ash-v1:" + "9" * 5000,
        "ash-v1:²",
    ):
        with pytest.raises(acp.RequestError) as invalid_cursor:
            await agent.list_sessions(cwd=str(workspace), cursor=malformed_cursor)
        assert invalid_cursor.value.data["cursor"] == "invalid cursor"
    with pytest.raises(acp.RequestError, match="session limit"):
        await agent.new_session(str(workspace))
    with pytest.raises(acp.RequestError) as duplicate:
        await agent.load_session(str(workspace), stored.session_id)
    assert duplicate.value.data["reason"] == "session already loaded"
    with pytest.raises(acp.RequestError) as unsupported:
        await agent.prompt(
            stored.session_id,
            [AudioContentBlock(type="audio", data="AA==", mime_type="audio/wav")],
        )
    assert unsupported.value.data["prompt"] == "unsupported content type: audio"

    await agent.aclose()
    assert clients[0].closed


@pytest.mark.asyncio
async def test_acp_fork_creates_independent_durable_runtime(
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
    parent = store.create_session(str(workspace), model="test")
    store.save_message(
        parent.session_id,
        Message(
            role="user",
            content="fork this",
            timestamp=datetime.now(timezone.utc),
        ),
    )
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    clients: list[FakeAshClient] = []
    configs_seen: list[dict[str, Any]] = []

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        client = FakeAshClient(session_id or "new", _events(), approval_callback)
        clients.append(client)
        configs_seen.append(mcp_configs)
        return client

    connection = FakeACPConnection()
    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(connection)  # type: ignore[arg-type]
    await agent.load_session(str(workspace), parent.session_id)
    connection.updates.clear()

    forked = await agent.fork_session(
        parent.session_id,
        str(workspace),
        mcp_servers=[
            HttpMcpServer(
                type="http",
                name="fork-docs",
                url="https://mcp.example.test/fork",
                headers=[],
            )
        ],
    )
    child = store.load_session(forked.session_id)

    assert forked.session_id != parent.session_id
    assert child.parent_session_id == parent.session_id
    assert [message.content for message in child.messages] == ["fork this"]
    assert clients[0].closed is False
    assert clients[0].loop.current_session.session_id == parent.session_id
    assert clients[1].loop.current_session.session_id == forked.session_id
    assert configs_seen[1]["fork-docs"].url == "https://mcp.example.test/fork"

    await agent.prompt(parent.session_id, [text_block("parent still works")])
    await agent.prompt(forked.session_id, [text_block("child works")])
    assert clients[0].prompts == ["parent still works"]
    assert clients[1].prompts == ["child works"]
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_resume_attaches_without_replaying_history(
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
    store.save_message(
        stored.session_id,
        Message(
            role="assistant",
            content="do not replay",
            timestamp=datetime.now(timezone.utc),
        ),
    )
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    clients: list[FakeAshClient] = []
    configs_seen: list[dict[str, Any]] = []

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        client = FakeAshClient(session_id or "new", _events(), approval_callback)
        clients.append(client)
        configs_seen.append(mcp_configs)
        return client

    connection = FakeACPConnection()
    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(connection)  # type: ignore[arg-type]

    await agent.resume_session(
        stored.session_id,
        str(workspace),
        mcp_servers=[
            McpServerStdio(
                name="resume-tools",
                command="server",
                args=["--stdio"],
                env=[],
            )
        ],
    )
    assert connection.updates == []
    assert clients[0].loop.current_session.session_id == stored.session_id
    assert configs_seen[0]["resume-tools"].command == "server"

    with pytest.raises(acp.RequestError) as duplicate:
        await agent.resume_session(stored.session_id, str(workspace))
    assert duplicate.value.data["reason"] == "session already loaded"

    await agent.prompt(stored.session_id, [text_block("continue")])
    assert clients[0].prompts == ["continue"]
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_resume_preserves_primary_error_when_cleanup_fails(
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
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    class UncleanClient(FakeAshClient):
        async def close(self) -> None:
            self.closed = True
            raise RuntimeError("runtime cleanup failed")

    client: UncleanClient | None = None

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        nonlocal client
        client = UncleanClient("wrong-session", _events(), approval_callback)
        return client

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="did not resume"):
        await agent.resume_session(stored.session_id, str(workspace))

    assert client is not None and client.closed is True
    assert store.load_session(stored.session_id).session_id == stored.session_id


@pytest.mark.asyncio
async def test_acp_fork_and_resume_reject_additional_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    config = AshConfig(
        model="ollama/test",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    store = SessionStore(config.db_directory / "sessions.db")
    stored = store.create_session(str(workspace), model="test")
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    agent = AshACPAgent()  # factory must never be reached
    for operation in (
        lambda: agent.fork_session(
            stored.session_id,
            str(workspace),
            additional_directories=[str(extra)],
        ),
        lambda: agent.resume_session(
            stored.session_id,
            str(workspace),
            additional_directories=[str(extra)],
        ),
    ):
        with pytest.raises(acp.RequestError) as unsupported:
            await operation()
        assert "additionalDirectories" in unsupported.value.data


@pytest.mark.asyncio
async def test_acp_concurrent_resume_reserves_session_id_once(
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
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    attach_started = asyncio.Event()
    release_attach = asyncio.Event()
    clients: list[FakeAshClient] = []

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        attach_started.set()
        await release_attach.wait()
        client = FakeAshClient(session_id or "new", _events(), approval_callback)
        clients.append(client)
        return client

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    first = asyncio.create_task(agent.resume_session(stored.session_id, str(workspace)))
    await asyncio.wait_for(attach_started.wait(), timeout=2)

    with pytest.raises(acp.RequestError) as duplicate:
        await agent.resume_session(stored.session_id, str(workspace))
    assert duplicate.value.data["reason"] == "session already loaded"
    assert clients == []

    release_attach.set()
    await asyncio.wait_for(first, timeout=2)
    assert len(clients) == 1
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_fork_blocks_parent_prompt_until_child_is_published(
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
    parent = store.create_session(str(workspace), model="test")
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    child_attach_started = asyncio.Event()
    release_child = asyncio.Event()

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        if session_id != parent.session_id:
            child_attach_started.set()
            await release_child.wait()
        return FakeAshClient(session_id or "new", _events(), approval_callback)

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    await agent.load_session(str(workspace), parent.session_id)

    fork_task = asyncio.create_task(agent.fork_session(parent.session_id, str(workspace)))
    await asyncio.wait_for(child_attach_started.wait(), timeout=2)
    child_ids = [
        item.session_id
        for item in store.list_sessions()
        if item.session_id != parent.session_id
    ]
    assert len(child_ids) == 1
    with pytest.raises(acp.RequestError) as child_pending:
        await asyncio.wait_for(
            agent.resume_session(child_ids[0], str(workspace)),
            timeout=0.5,
        )
    assert child_pending.value.data["reason"] == "session already loaded"
    with pytest.raises(acp.RequestError) as blocked:
        await agent.prompt(parent.session_id, [text_block("race")])
    assert "lifecycle" in str(blocked.value.data).casefold()

    release_child.set()
    forked = await asyncio.wait_for(fork_task, timeout=2)
    await agent.prompt(parent.session_id, [text_block("after fork")])
    assert store.load_session(forked.session_id).parent_session_id == parent.session_id
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_fork_fails_before_side_effect_at_session_limit(
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
    parent = store.create_session(str(workspace), model="test")
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        return FakeAshClient(session_id or "new", _events(), approval_callback)

    agent = AshACPAgent(client_factory=factory, max_sessions=1)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    await agent.load_session(str(workspace), parent.session_id)
    before = [item.session_id for item in store.list_sessions()]

    with pytest.raises(acp.RequestError, match="session limit"):
        await agent.fork_session(parent.session_id, str(workspace))

    assert [item.session_id for item in store.list_sessions()] == before
    await agent.aclose()


@pytest.mark.asyncio
async def test_acp_fork_cancellation_discards_unpublished_child(
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
    parent = store.create_session(str(workspace), model="test")
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    attach_started = asyncio.Event()

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        assert session_id is not None
        attach_started.set()
        await asyncio.Event().wait()

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]
    task = asyncio.create_task(agent.fork_session(parent.session_id, str(workspace)))
    await asyncio.wait_for(attach_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [item.session_id for item in store.list_sessions()] == [parent.session_id]


@pytest.mark.asyncio
async def test_acp_fork_failure_preserves_child_with_durable_activity(
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
    parent = store.create_session(str(workspace), model="test")
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        assert session_id is not None
        store.append_audit_log(
            session_id,
            action_type="command_run",
            target_resource="factory-side-effect",
            details={"source": "test"},
            result="FAILURE",
        )
        raise RuntimeError("factory failed after durable activity")

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="factory failed"):
        await agent.fork_session(parent.session_id, str(workspace))

    children = [
        item for item in store.list_sessions() if item.session_id != parent.session_id
    ]
    assert len(children) == 1
    assert children[0].parent_session_id == parent.session_id
    assert len(store.list_audit_logs(children[0].session_id)) == 1


@pytest.mark.asyncio
async def test_acp_fork_preserves_child_when_runtime_cleanup_is_unconfirmed(
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
    parent = store.create_session(str(workspace), model="test")
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    class UncleanClient(FakeAshClient):
        async def close(self) -> None:
            self.closed = True
            raise RuntimeError("runtime cleanup failed")

    client: UncleanClient | None = None

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        nonlocal client
        client = UncleanClient("wrong-session", _events(), approval_callback)
        return client

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="did not attach"):
        await agent.fork_session(parent.session_id, str(workspace))

    assert client is not None and client.closed is True
    children = [
        item for item in store.list_sessions() if item.session_id != parent.session_id
    ]
    assert len(children) == 1
    assert children[0].parent_session_id == parent.session_id


@pytest.mark.asyncio
async def test_acp_fork_and_resume_enforce_workspace_and_complete_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    base_config = AshConfig(
        model="ollama/test",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    store = SessionStore(base_config.db_directory / "sessions.db")
    stored = store.create_session(str(workspace), model="test")
    interrupted = store.create_session(str(workspace), model="test")
    store.start_turn(interrupted.session_id, "turn-started", "unfinished")
    store.save_message(
        interrupted.session_id,
        Message(
            role="user",
            content="unfinished",
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id="turn-started",
    )

    def load_config(cls: type[AshConfig], **kwargs: Any) -> AshConfig:
        selected = kwargs.get("workspace_root") or workspace
        return base_config.model_copy(update={"workspace_root": selected})

    monkeypatch.setattr(AshConfig, "load", classmethod(load_config))

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        return FakeAshClient(session_id or "new", _events(), approval_callback)

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(FakeACPConnection())  # type: ignore[arg-type]

    for operation in (
        lambda: agent.fork_session(stored.session_id, str(other_workspace)),
        lambda: agent.resume_session(stored.session_id, str(other_workspace)),
    ):
        with pytest.raises(acp.RequestError) as mismatch:
            await operation()
        assert "different workspace" in str(mismatch.value.data).casefold()

    with pytest.raises(acp.RequestError) as unfinished:
        await agent.fork_session(interrupted.session_id, str(workspace))
    assert "unfinished" in str(unfinished.value.data).casefold()
    assert {item.session_id for item in store.list_sessions()} == {
        stored.session_id,
        interrupted.session_id,
    }


@pytest.mark.asyncio
async def test_acp_load_does_not_succeed_after_concurrent_session_close(
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
    store.save_message(
        stored.session_id,
        Message(
            role="assistant",
            content="replay me",
            timestamp=datetime.now(timezone.utc),
        ),
    )
    monkeypatch.setattr(
        AshConfig,
        "load",
        classmethod(lambda cls, **kwargs: config),
    )

    async def factory(
        selected_workspace: Path,
        session_id: str | None,
        mcp_configs: dict[str, Any],
        approval_callback: Any,
    ) -> Any:
        return FakeAshClient(session_id or "new", [], approval_callback)

    replay_started = asyncio.Event()
    release_replay = asyncio.Event()

    class BlockingReplayConnection(FakeACPConnection):
        async def session_update(self, session_id: str, update: Any) -> None:
            replay_started.set()
            await release_replay.wait()
            await super().session_update(session_id, update)

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(BlockingReplayConnection())  # type: ignore[arg-type]
    load_task = asyncio.create_task(agent.load_session(str(workspace), stored.session_id))
    await asyncio.wait_for(replay_started.wait(), timeout=2)

    await agent.close_session(stored.session_id)
    release_replay.set()

    with pytest.raises(acp.RequestError):
        await asyncio.wait_for(load_task, timeout=2)


@pytest.mark.asyncio
async def test_acp_load_cancellation_closes_and_unregisters_client(
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
    store.save_message(
        stored.session_id,
        Message(
            role="assistant",
            content="block replay",
            timestamp=datetime.now(timezone.utc),
        ),
    )
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

    replay_started = asyncio.Event()

    class BlockingReplayConnection(FakeACPConnection):
        async def session_update(self, session_id: str, update: Any) -> None:
            replay_started.set()
            await asyncio.Event().wait()

    agent = AshACPAgent(client_factory=factory)  # type: ignore[arg-type]
    agent.on_connect(BlockingReplayConnection())  # type: ignore[arg-type]
    load_task = asyncio.create_task(agent.load_session(str(workspace), stored.session_id))
    await asyncio.wait_for(replay_started.wait(), timeout=2)
    load_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await load_task

    assert clients[0].closed is True
    with pytest.raises(acp.RequestError):
        await agent.close_session(stored.session_id)


class WireClient(FakeACPConnection):
    def on_connect(self, conn: Any) -> None:
        self.connection = conn


async def _loopback_stream_pair() -> tuple[
    asyncio.StreamReader,
    asyncio.StreamWriter,
    asyncio.StreamReader,
    asyncio.StreamWriter,
    asyncio.Server,
]:
    loop = asyncio.get_running_loop()
    accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        loop.create_future()
    )

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not accepted.done():
            accepted.set_result((reader, writer))

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    try:
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        client_reader, client_writer = await asyncio.open_connection(host, port)
        agent_reader, agent_writer = await asyncio.wait_for(accepted, timeout=2)
    except BaseException:
        server.close()
        await server.wait_closed()
        raise
    server.close()
    return agent_reader, agent_writer, client_reader, client_writer, server


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
    agent_reader, agent_writer, client_reader, client_writer, server = (
        await _loopback_stream_pair()
    )
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
        await client_writer.wait_closed()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.TimeoutError, ConnectionError):
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
        agent_writer.close()
        await agent_writer.wait_closed()
        await connection.close()
        await agent.aclose()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_production_acp_entrypoint_exposes_fork_resume_and_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the shipped ACP process through the official client wire."""

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    database = tmp_path / "db"
    workspace.mkdir()
    home.mkdir()
    database.mkdir()
    (workspace / "README.md").write_text("ACP smoke workspace\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    set_workspace_trusted(workspace, True)

    requests: list[tuple[str, dict[str, Any]]] = []

    class ProviderHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - HTTP handler API
            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length))
            requests.append((self.path, payload))
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            body = (
                'data: {"id":"acp","choices":[{"delta":{"content":'
                '"acp-real-ok"},"finish_reason":null}]}\n\n'
                'data: {"id":"acp","choices":[{"delta":{},'
                '"finish_reason":"stop"}]}\n\n'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "ASH_WORKSPACE_ROOT": str(workspace),
            "ASH_DB_DIRECTORY": str(database),
            "ASH_MODEL": "openai/acp-real-model",
            "OPENAI_API_KEY": "acp-loopback-key",
            "OPENAI_API_BASE": (
                f"http://127.0.0.1:{provider.server_port}/v1"
            ),
            "PYTHONUNBUFFERED": "1",
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ash",
        "acp",
        cwd=workspace,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    wire_client = WireClient()
    connection = connect_to_agent(wire_client, process.stdin, process.stdout)
    try:
        initialized = await asyncio.wait_for(
            connection.initialize(protocol_version=PROTOCOL_VERSION), timeout=15
        )
        capabilities = initialized.agent_capabilities
        assert capabilities is not None
        assert capabilities.prompt_capabilities is not None
        assert capabilities.prompt_capabilities.image is True
        assert capabilities.session_capabilities is not None
        assert capabilities.session_capabilities.close is not None
        assert capabilities.session_capabilities.fork is not None
        assert capabilities.session_capabilities.resume is not None
        session = await asyncio.wait_for(
            connection.new_session(cwd=str(workspace), mcp_servers=[]), timeout=30
        )
        prompted = await asyncio.wait_for(
            connection.prompt(
                session_id=session.session_id,
                prompt=[
                    text_block("Return the ACP response"),
                    ImageContentBlock(
                        type="image",
                        data="YWJj",
                        mime_type="image/png",
                    ),
                ],
            ),
            timeout=45,
        )
        assert prompted.stop_reason == "end_turn"
        assert any(
            update.session_update == "agent_message_chunk"
            and update.content.text == "acp-real-ok"
            for _, update in wire_client.updates
        )

        forked = await asyncio.wait_for(
            connection.fork_session(
                session.session_id, cwd=str(workspace), mcp_servers=[]
            ),
            timeout=30,
        )
        assert forked.session_id != session.session_id
        child_prompted = await asyncio.wait_for(
            connection.prompt(
                session_id=forked.session_id,
                prompt=[text_block("Continue on the child branch")],
            ),
            timeout=45,
        )
        parent_prompted = await asyncio.wait_for(
            connection.prompt(
                session_id=session.session_id,
                prompt=[text_block("Continue on the parent branch")],
            ),
            timeout=45,
        )
        assert child_prompted.stop_reason == "end_turn"
        assert parent_prompted.stop_reason == "end_turn"

        await asyncio.wait_for(
            connection.close_session(session.session_id), timeout=15
        )
        wire_client.updates.clear()
        await asyncio.wait_for(
            connection.resume_session(
                session.session_id, cwd=str(workspace), mcp_servers=[]
            ),
            timeout=30,
        )
        assert wire_client.updates == []
        resumed = await asyncio.wait_for(
            connection.prompt(
                session_id=session.session_id,
                prompt=[text_block("Continue after resume")],
            ),
            timeout=45,
        )
        assert resumed.stop_reason == "end_turn"

        await asyncio.wait_for(
            connection.close_session(forked.session_id), timeout=15
        )
        await asyncio.wait_for(
            connection.close_session(session.session_id), timeout=15
        )
        with pytest.raises(acp.RequestError) as already_closed:
            await asyncio.wait_for(
                connection.close_session(session.session_id), timeout=10
            )
        assert already_closed.value.code == -32002
        with pytest.raises(acp.RequestError) as missing:
            await asyncio.wait_for(
                connection.prompt(
                    session_id=session.session_id,
                    prompt=[text_block("must be rejected")],
                ),
                timeout=10,
            )
        assert missing.value.code == -32002
        assert len(requests) == 4
        assert requests[0][0] == "/v1/chat/completions"
        assert requests[0][1]["model"] == "acp-real-model"
        assert requests[0][1]["stream"] is True
        user_message = next(
            message
            for message in requests[0][1]["messages"]
            if message["role"] == "user"
        )
        assert user_message["content"] == [
            {"type": "text", "text": "Return the ACP response"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,YWJj"},
            },
        ]
    finally:
        await connection.close()
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except TimeoutError:
            process.kill()
            await process.wait()
        stderr = (
            await process.stderr.read()
            if process.stderr is not None
            else b""
        ).decode("utf-8", errors="replace")
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
        assert process.returncode == 0, stderr
