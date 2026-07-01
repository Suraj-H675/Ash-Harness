import pytest
import asyncio
import subprocess

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


@pytest.mark.asyncio
async def test_coder_agent_edits_isolated_worktree_and_returns_branch(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    (repository / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=repository,
        check=True,
    )

    class CodingProvider(FakeProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            assert tools is not None
            if self.calls == 1:
                yield StreamChunk(
                    native_tool_calls=[
                        {
                            "id": "write-1",
                            "name": "write_file",
                            "arguments": {
                                "file_path": "file.txt",
                                "content": "worker\n",
                                "overwrite": True,
                            },
                        }
                    ],
                    is_done=True,
                )
            else:
                yield StreamChunk(content="implemented and verified", is_done=True)

    state = SharedState(tmp_path / "state" / "agents.db")
    tool = SpawnAgentTool(SafetyGuard(repository), state, CodingProvider)

    result = await tool.run(
        role="coder",
        task="update file",
        agent_id="coder-1",
    )

    assert result.success is True
    assert "implemented and verified" in result.output
    assert "branch=ash-agent/coder-1" in result.output
    assert (repository / "file.txt").read_text(encoding="utf-8") == "base\n"
    assert (
        subprocess.run(
            ["git", "show", "ash-agent/coder-1:file.txt"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == "worker\n"
    )
    report_messages = state.fetch_messages("lead", undelivered_only=False)
    report = report_messages[-1].content
    assert report["artifacts"]["branch"] == "ash-agent/coder-1"
    await tool.aclose()


@pytest.mark.asyncio
async def test_shared_coder_requires_explicit_isolation_choice(tmp_path) -> None:
    class InspectingProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            assert tools is not None
            tool_names = {item["function"]["name"] for item in tools}
            assert "write_file" in tool_names
            assert "spawn_agent" not in tool_names
            yield StreamChunk(content="inspected tools", is_done=True)

    state = SharedState(tmp_path / "state" / "agents.db")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, InspectingProvider)

    result = await tool.run(
        role="coder",
        task="inspect",
        isolation="shared",
    )

    assert result.success is True
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_agent_consumes_and_acknowledges_steering(tmp_path) -> None:
    class SteeringProvider(FakeProvider):
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await asyncio.sleep(0.25)
                yield StreamChunk(content="initial", is_done=True)
            else:
                assert any(
                    message["role"] == "user" and message["content"] == "focus tests"
                    for message in messages
                )
                yield StreamChunk(content="redirected", is_done=True)

    provider = SteeringProvider()
    state = SharedState(tmp_path / "state" / "agents.db")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, lambda: provider)
    started = await tool.run(
        role="reviewer",
        task="inspect",
        agent_id="steered-worker",
        background=True,
    )
    assert started.success is True
    await provider.started.wait()
    message_id = state.send_to_agent(
        "lead",
        "steered-worker",
        "steer",
        "focus tests",
    )

    for _ in range(30):
        status = state.get_status("steered-worker")
        if status is not None and status.status == "completed":
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("steered worker did not complete")

    message = next(
        item
        for item in state.fetch_messages("steered-worker", undelivered_only=False)
        if item.message_id == message_id
    )
    assert message.delivered is True
    report = state.fetch_messages("lead", undelivered_only=False)[-1]
    assert report.content["summary"] == "redirected"
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_agent_honors_persisted_stop_message(tmp_path) -> None:
    class WaitingProvider(FakeProvider):
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.started.set()
            await asyncio.sleep(10)
            yield StreamChunk(content="late", is_done=True)

    provider = WaitingProvider()
    state = SharedState(tmp_path / "state" / "agents.db")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, lambda: provider)
    await tool.run(
        role="reviewer",
        task="wait",
        agent_id="stopped-worker",
        background=True,
    )
    await provider.started.wait()
    message_id = state.send_message(
        "lead",
        "stopped-worker",
        "stop",
        {},
    )

    for _ in range(30):
        status = state.get_status("stopped-worker")
        if status is not None and status.status == "failed":
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("persisted stop was not consumed")

    message = next(
        item
        for item in state.fetch_messages("stopped-worker", undelivered_only=False)
        if item.message_id == message_id
    )
    assert message.delivered is True
    assert "persisted message" in state.get_status("stopped-worker").current_task
    await tool.aclose()
