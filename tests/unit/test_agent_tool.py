import pytest
import asyncio
import subprocess

from ash.agents.shared_state import SharedState
from ash.config import AshConfig
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.tools.agent import SpawnAgentTool


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
    emitted: list[dict] = []
    tool.set_event_sink(emitted.append)
    result = await tool.run(role="reviewer", task="inspect tests", agent_id="worker")
    assert result.success is True
    assert result.output == "evidence: tests pass"
    assert state.get_status("worker").status == "completed"
    durable = state.tasks.list_tasks()
    assert len(durable) == 1
    assert durable[0].state == "succeeded"
    assert durable[0].owner_agent_id == "worker"
    assert durable[0].result["summary"] == "evidence: tests pass"
    assert [event["type"] for event in emitted] == [
        "agent.task.created",
        "agent.task.leased",
        "agent.task.running",
        "agent.task.succeeded",
    ]
    assert all(event["task_id"] == durable[0].task_id for event in emitted)
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
    assert state.tasks.list_tasks()[0].state == "cancelled"
    await tool.aclose()


@pytest.mark.asyncio
async def test_agent_capacity_is_enforced_across_durable_leases(tmp_path) -> None:
    class SlowProvider(FakeProvider):
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.started.set()
            await asyncio.sleep(10)
            yield StreamChunk(content="late", is_done=True)

    provider = SlowProvider()
    state = SharedState(tmp_path / "agents.db")
    config = AshConfig(workspace_root=tmp_path, max_concurrent_agents=1)
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, lambda: provider, config=config)
    first = await tool.run(
        role="reviewer", task="first", agent_id="first", background=True
    )
    assert first.success is True
    await provider.started.wait()

    second = await tool.run(
        role="reviewer", task="second", agent_id="second", background=True
    )

    assert second.success is False
    assert "concurrency limit" in (second.error or "")
    states = {task.description: task.state for task in state.tasks.list_tasks()}
    assert states == {"first": "running", "second": "cancelled"}
    await tool.aclose()
    reopened = SharedState(tmp_path / "agents.db")
    try:
        assert reopened.get_status("first").status == "failed"
        assert {
            task.description: task.state for task in reopened.tasks.list_tasks()
        } == {"first": "cancelled", "second": "cancelled"}
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_agent_token_budget_changes_report_and_task_to_failure(tmp_path) -> None:
    state = SharedState(tmp_path / "agents.db")
    config = AshConfig(workspace_root=tmp_path, agent_token_budget=1)
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, FakeProvider, config=config)

    result = await tool.run(role="reviewer", task="inspect tests", agent_id="budget")

    assert result.success is False
    assert "exceeded token budget" in (result.error or "")
    assert state.get_status("budget").status == "failed"
    durable = state.tasks.list_tasks()[0]
    assert durable.state == "failed"
    assert durable.used_tokens > durable.token_budget
    report = state.fetch_messages("lead", undelivered_only=False)[-1]
    assert report.content["success"] is False
    await tool.aclose()


@pytest.mark.asyncio
async def test_agent_time_budget_is_enforced_and_persisted(tmp_path) -> None:
    class SlowProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            await asyncio.sleep(10)
            yield StreamChunk(content="late", is_done=True)

    state = SharedState(tmp_path / "agents.db")
    config = AshConfig(workspace_root=tmp_path, agent_time_budget_seconds=1)
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, SlowProvider, config=config)

    result = await tool.run(role="reviewer", task="wait", agent_id="timed")

    assert result.success is False
    assert "time budget" in (result.error or "")
    assert state.get_status("timed").status == "failed"
    assert state.tasks.list_tasks()[0].state == "failed"
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


@pytest.mark.asyncio
async def test_completed_agent_can_resume_from_persisted_report(tmp_path) -> None:
    class ResumeProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(content="continued", is_done=True)

    state = SharedState(tmp_path / "state" / "agents.db")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, ResumeProvider)
    first = await tool.run(
        role="reviewer",
        task="review tests",
        agent_id="reviewer-1",
        isolation="shared",
    )
    assert first.success is True

    resumed = await tool.resume("reviewer-1")

    assert resumed.success is True
    assert "Continued from reviewer-1" in resumed.output
    for _ in range(20):
        if any(
            status.agent_id.startswith("reviewer-1-r-")
            for status in state.list_agents()
        ):
            break
        await asyncio.sleep(0.01)
    assert any(
        status.agent_id.startswith("reviewer-1-r-") for status in state.list_agents()
    )
    tasks = sorted(state.tasks.list_tasks(), key=lambda task: task.created_at)
    assert tasks[1].parent_task_id == tasks[0].task_id
    await tool.aclose()


@pytest.mark.asyncio
async def test_isolated_agent_requires_branch_apply_before_resume(tmp_path) -> None:
    state = SharedState(tmp_path / "state" / "agents.db")
    state.register_agent(
        "coder-1",
        role="coder",
        metadata={"task": "edit", "isolation": "worktree"},
    )
    state.update_status("coder-1", "completed", "done")
    state.send_message(
        "coder-1",
        "lead",
        "agent_report",
        {
            "agent_id": "coder-1",
            "task": "edit",
            "summary": "done",
            "artifacts": {"branch": "ash-agent/coder-1"},
        },
    )
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, FakeProvider)

    result = await tool.resume("coder-1")

    assert result.success is False
    assert "Apply isolated branch" in (result.error or "")
    await tool.aclose()
