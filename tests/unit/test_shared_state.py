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
            await state.update_status_async(agent_id, "working", current_task=f"task-{i}")

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