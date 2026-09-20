from dataclasses import replace

import asyncio
import pytest

import ash.agents.approval_channel as approval_channel_module
from ash.agents._agent_driver import _run_durable_task
from ash.agents.approval_channel import (
    ApprovalChannelError,
    ApprovalDecision,
    ApprovalRuleDelta,
    ForegroundApprovalServer,
    request_foreground_approval,
)
from ash.agents.shared_state import SharedState
from ash.config import AshConfig
from ash.safety.grants import PermissionRule


@pytest.mark.asyncio
async def test_foreground_approval_channel_round_trip_denial_feedback() -> None:
    calls: list[tuple[str, dict]] = []

    async def handler(tool_name: str, arguments: dict) -> ApprovalDecision:
        calls.append((tool_name, arguments))
        return ApprovalDecision(False, "not this write")

    server = ForegroundApprovalServer(
        task_id="task-1",
        agent_id="agent-1",
        attempt=1,
        handler=handler,
    )
    endpoint = await server.start()
    try:
        decision = await request_foreground_approval(
            endpoint,
            tool_name="write_file",
            arguments={"file_path": "one.txt", "content": "secret\n"},
        )
    finally:
        await server.aclose()

    assert decision.approved is False
    assert decision.feedback == "not this write"
    assert decision.rules == ()
    assert calls == [
        ("write_file", {"file_path": "one.txt", "content": "secret\n"})
    ]


@pytest.mark.asyncio
async def test_foreground_approval_channel_rejects_wrong_capability_token() -> None:
    called = False

    async def handler(tool_name: str, arguments: dict) -> ApprovalDecision:
        nonlocal called
        called = True
        return ApprovalDecision(True)

    server = ForegroundApprovalServer(
        task_id="task-1",
        agent_id="agent-1",
        attempt=1,
        handler=handler,
    )
    endpoint = await server.start()
    forged = replace(endpoint, token="00" * 32)
    try:
        with pytest.raises(ApprovalChannelError, match="closed without a response"):
            await request_foreground_approval(
                forged,
                tool_name="write_file",
                arguments={"file_path": "one.txt", "content": "secret\n"},
            )
    finally:
        await server.aclose()

    assert called is False


@pytest.mark.asyncio
async def test_foreground_approval_channel_rejects_nonmatching_rule_delta() -> None:
    async def handler(tool_name: str, arguments: dict) -> ApprovalDecision:
        del tool_name, arguments
        return ApprovalDecision(
            True,
            rules=(
                ApprovalRuleDelta(
                    "session_rules",
                    PermissionRule.create("allow", "run_command"),
                ),
            ),
        )

    server = ForegroundApprovalServer(
        task_id="task-1",
        agent_id="agent-1",
        attempt=1,
        handler=handler,
    )
    endpoint = await server.start()
    try:
        with pytest.raises(ApprovalChannelError, match="does not match"):
            await request_foreground_approval(
                endpoint,
                tool_name="write_file",
                arguments={"file_path": "one.txt", "content": "secret\n"},
            )
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_foreground_approval_channel_close_cancels_active_handler() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(tool_name: str, arguments: dict) -> ApprovalDecision:
        del tool_name, arguments
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    server = ForegroundApprovalServer(
        task_id="task-1",
        agent_id="agent-1",
        attempt=1,
        handler=handler,
    )
    endpoint = await server.start()
    request = asyncio.create_task(
        request_foreground_approval(
            endpoint,
            tool_name="write_file",
            arguments={"file_path": "one.txt", "content": "secret\n"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    await server.aclose()

    assert cancelled.is_set()
    with pytest.raises(ApprovalChannelError, match="closed without a response"):
        await request


@pytest.mark.asyncio
async def test_foreground_approval_channel_drops_silent_pre_auth_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def handler(tool_name: str, arguments: dict) -> ApprovalDecision:
        nonlocal called
        del tool_name, arguments
        called = True
        return ApprovalDecision(True)

    monkeypatch.setattr(
        approval_channel_module,
        "APPROVAL_AUTH_TIMEOUT_SECONDS",
        0.05,
    )
    server = ForegroundApprovalServer(
        task_id="task-1",
        agent_id="agent-1",
        attempt=1,
        handler=handler,
    )
    endpoint = await server.start()
    reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
    try:
        assert await asyncio.wait_for(reader.read(1), timeout=1) == b""
        assert called is False
    finally:
        writer.close()
        await server.aclose()


@pytest.mark.asyncio
async def test_foreground_approval_channel_caps_pre_auth_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def handler(tool_name: str, arguments: dict) -> ApprovalDecision:
        nonlocal called
        del tool_name, arguments
        called = True
        return ApprovalDecision(True)

    monkeypatch.setattr(
        approval_channel_module,
        "MAX_APPROVAL_CONNECTIONS",
        2,
    )
    monkeypatch.setattr(
        approval_channel_module,
        "APPROVAL_AUTH_TIMEOUT_SECONDS",
        5.0,
    )
    server = ForegroundApprovalServer(
        task_id="task-1",
        agent_id="agent-1",
        attempt=1,
        handler=handler,
    )
    endpoint = await server.start()
    reader_one, writer_one = await asyncio.open_connection(
        endpoint.host,
        endpoint.port,
    )
    await asyncio.sleep(0)
    reader_two, writer_two = await asyncio.open_connection(
        endpoint.host,
        endpoint.port,
    )
    await asyncio.sleep(0)
    reader_three, writer_three = await asyncio.open_connection(
        endpoint.host,
        endpoint.port,
    )
    try:
        assert await asyncio.wait_for(reader_three.read(1), timeout=1) == b""
        assert not reader_one.at_eof()
        assert not reader_two.at_eof()
        assert called is False
    finally:
        for writer in (writer_one, writer_two, writer_three):
            writer.close()
        await server.aclose()


@pytest.mark.asyncio
async def test_agent_driver_rejects_stale_live_approval_attempt(tmp_path) -> None:
    state = SharedState(tmp_path / "agents.db")
    durable = state.tasks.create_task(
        "write one file",
        role="coder",
        metadata={"agent_id": "agent-1"},
    )
    config = AshConfig(
        workspace_root=tmp_path,
        model="openai/test-model",
        memory_backend="off",
    )
    spec = {
        "version": 1,
        "kind": "durable_task",
        "db_path": str(state.db_path),
        "task_id": durable.task_id,
        "workspace_root": str(tmp_path),
        "config": config.model_dump(mode="json", exclude={"openai_api_key"}),
        "provider_env": {},
        "permission_policy": {
            "mode": "interactive",
            "managed_rules": [],
            "persistent_rules": [],
            "session_rules": [],
        },
        "custom_agent": None,
        "max_return_chars": 20_000,
        "max_turn_iterations": 12,
        "require_dispatchable": False,
        "approval_channel": {
            "version": 1,
            "host": "127.0.0.1",
            "port": 9,
            "token": "ab" * 32,
            "task_id": durable.task_id,
            "agent_id": "agent-1",
            "attempt": 2,
        },
    }

    with pytest.raises(ValueError, match="does not match durable task"):
        await _run_durable_task(spec)
