import pytest
import asyncio

from agents.shared_state import SharedState
from providers.base import ProviderABC, StreamChunk
from safety.guard import SafetyGuard
from tools.agent import SpawnAgentTool


class FakeProvider(ProviderABC):
    model_name = "fake"

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        assert messages[-1]["content"] == "inspect tests"
        yield StreamChunk(content="evidence: tests pass", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


@pytest.mark.asyncio
async def test_spawn_agent_uses_provider_and_persists_report(tmp_path) -> None:
    state = SharedState(tmp_path / "agents.db")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, FakeProvider)
    result = await tool.run(role="reviewer", task="inspect tests", agent_id="worker")
    assert result.success is True
    assert result.output == "evidence: tests pass"
    assert state.get_status("worker").status == "completed"
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_agent_can_be_stopped(tmp_path) -> None:
    class SlowProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            await asyncio.sleep(10)
            yield StreamChunk(content="late", is_done=True)

    state = SharedState(tmp_path / "agents.db")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, SlowProvider)
    result = await tool.run(
        role="reviewer",
        task="inspect tests",
        agent_id="slow-worker",
        background=True,
    )
    assert result.success is True
    await asyncio.sleep(0)
    assert state.get_status("slow-worker").status == "working"
    assert await tool.stop("slow-worker") is True
    assert state.get_status("slow-worker").status == "failed"
    await tool.aclose()
