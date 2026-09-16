# tests/unit/test_shared_state.py
import asyncio
import pytest
from ash.agents.shared_state import SharedState
import tempfile
from pathlib import Path


@pytest.fixture
def state() -> SharedState:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield SharedState(Path(tmpdir) / "test.db")


@pytest.mark.asyncio
async def test_concurrent_status_updates_do_not_race(state):
    """Multiple concurrent update_status calls should not corrupt state."""
    # Register agents first (update_status does UPDATE not INSERT)
    state.register_agent("agent-a", role="general")
    state.register_agent("agent-b", role="general")
    state.register_agent("agent-c", role="general")

    async def update_many(agent_id, count):
        for i in range(count):
            await state.update_status_async(
                agent_id, "working", current_task=f"task-{i}"
            )

    await asyncio.gather(
        update_many("agent-a", 10),
        update_many("agent-b", 10),
        update_many("agent-c", 10),
    )

    agents = {st.agent_id: st for st in state.list_agents()}
    assert len(agents) == 3
    # All agents should have completed without exceptions
    for agent_id in ["agent-a", "agent-b", "agent-c"]:
        assert agent_id in agents


@pytest.mark.asyncio
async def test_concurrent_send_and_fetch(state):
    """Concurrent IPC send + fetch should not lose messages."""

    async def send_messages(sender, count):
        for i in range(count):
            state.send_message(sender, "lead", "test", f"msg-{i}")

    await asyncio.gather(
        send_messages("agent-1", 5),
        send_messages("agent-2", 5),
    )

    messages = state.fetch_messages("lead", undelivered_only=False)
    assert len(messages) == 10


def test_agent_to_agent_messages(state):
    state.register_agent("agent-a", role="general")
    state.register_agent("agent-b", role="general")

    state.send_to_agent("agent-a", "agent-b", "test", "hello from a")
    messages = state.fetch_messages("agent-b", undelivered_only=False)
    assert len(messages) == 1
    assert messages[0].content == {"content": "hello from a"}


def test_broadcast(state):
    state.register_agent("agent-a", role="general")
    state.register_agent("agent-b", role="general")
    state.register_agent("agent-c", role="general")

    state.broadcast("agent-a", "ping", "checkin")
    for agent_id in ["agent-b", "agent-c"]:
        msgs = state.fetch_messages(agent_id, undelivered_only=False)
        assert len(msgs) == 1
        assert msgs[0].content == "checkin"


def test_close_is_idempotent(tmp_path: Path) -> None:
    state = SharedState(tmp_path / "agents.db")

    state.close()
    state.close()


def test_retire_stale_approval_requests_preserves_active_and_retires_retried(
    tmp_path: Path,
) -> None:
    state = SharedState(tmp_path / "approval-recovery.db")
    state.tasks.create_task(
        "approval recovery",
        task_id="approval-recovery",
        max_attempts=2,
    )
    lease = state.tasks.claim_task("worker-a", task_id="approval-recovery")
    assert lease is not None
    state.tasks.start_task("approval-recovery", lease.token)
    request_id = state.send_message(
        "worker-a",
        "lead",
        "approval_request",
        {
            "task_id": "approval-recovery",
            "attempt": lease.task.attempt,
            "agent_id": "worker-a",
            "tool_name": "write_file",
            "arguments_sha256": "b" * 64,
            "arguments_preview": "{}",
        },
    )

    assert state.retire_stale_approval_requests() == []
    retried = state.tasks.fail_task(
        "approval-recovery",
        lease.token,
        "retry",
        retryable=True,
    )
    assert retried.state == "queued"
    lease2 = state.tasks.claim_task("worker-b", task_id="approval-recovery")
    assert lease2 is not None
    state.tasks.start_task("approval-recovery", lease2.token)

    assert state.retire_stale_approval_requests() == [request_id]
    request = state.fetch_messages(
        "lead",
        undelivered_only=False,
        message_type="approval_request",
    )[0]
    assert request.delivered is True
    state.close()
