import pytest
import asyncio
import subprocess

from ash.agents.shared_state import SharedState
from ash.config import AshConfig
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.grants import PermissionRule
from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy
from ash.tools.agent import SpawnAgentTool


class FakeProvider(ProviderABC):
    model_name = "fake"
    _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

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
async def test_background_subagent_never_uses_foreground_approval_broker(tmp_path) -> None:
    class WritingProvider(FakeProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            assert tools is not None
            if self.calls == 1:
                yield StreamChunk(
                    native_tool_calls=[
                        {
                            "id": "background-write",
                            "name": "write_file",
                            "arguments": {
                                "file_path": "background.txt",
                                "content": "must stay denied\n",
                                "overwrite": True,
                            },
                        }
                    ],
                    is_done=True,
                )
            else:
                assert any(
                    message.get("role") == "tool"
                    and "Denied by user" in str(message.get("content"))
                    for message in messages
                )
                yield StreamChunk(content="background denied", is_done=True)

    broker_calls: list[tuple[str, str]] = []

    async def broker(agent_id: str, tool_name: str, arguments: dict) -> bool:
        del arguments
        broker_calls.append((agent_id, tool_name))
        return True

    state = SharedState(tmp_path / "background-policy" / "agents.db")
    config = AshConfig(workspace_root=tmp_path, safety_tier="interactive")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, WritingProvider, config=config)
    tool.set_foreground_approval_broker(broker)

    started = await tool.run(
        role="coder",
        task="attempt background write",
        agent_id="background-coder",
        isolation="shared",
        background=True,
    )
    assert started.success is True
    report = await tool._tasks["background-coder"]

    assert report.success is True
    assert report.summary == "background denied"
    assert broker_calls == []
    assert not (tmp_path / "background.txt").exists()
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
async def test_interactive_parent_does_not_auto_approve_subagent_write(tmp_path) -> None:
    class WritingProvider(FakeProvider):
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
                                "file_path": "worker.txt",
                                "content": "should-not-write\n",
                                "overwrite": True,
                            },
                        }
                    ],
                    is_done=True,
                )
            else:
                assert any(
                    message.get("role") == "tool"
                    and "Denied by user" in str(message.get("content"))
                    for message in messages
                )
                yield StreamChunk(content="write was denied", is_done=True)

    state = SharedState(tmp_path / "state" / "agents.db")
    config = AshConfig(workspace_root=tmp_path, safety_tier="interactive")
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        WritingProvider,
        config=config,
    )

    result = await tool.run(
        role="coder",
        task="write a file",
        agent_id="interactive-coder",
        isolation="shared",
    )

    assert result.success is True
    assert result.output == "write was denied"
    assert not (tmp_path / "worker.txt").exists()
    await tool.aclose()


@pytest.mark.asyncio
async def test_subagent_inherits_parent_session_allow_rule(tmp_path) -> None:
    class WritingProvider(FakeProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            assert tools is not None
            if self.calls == 1:
                yield StreamChunk(
                    native_tool_calls=[{
                        "id": "write-allow",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "allowed.txt",
                            "content": "allowed\n",
                            "overwrite": True,
                        },
                    }],
                    is_done=True,
                )
            else:
                yield StreamChunk(content="write completed", is_done=True)

    state = SharedState(tmp_path / "state-allow" / "agents.db")
    config = AshConfig(workspace_root=tmp_path, safety_tier="interactive")
    parent_policy = PermissionPolicy(
        "interactive",
        session_rules=[PermissionRule.create("allow", "write_file")],
    )
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, WritingProvider, config=config)
    tool.set_permission_policy_provider(lambda: parent_policy)

    result = await tool.run(
        role="coder",
        task="write allowed file",
        agent_id="allowed-coder",
        isolation="shared",
    )

    assert result.success is True
    assert (tmp_path / "allowed.txt").read_text(encoding="utf-8") == "allowed\n"
    await tool.aclose()


@pytest.mark.asyncio
async def test_subagent_inherits_managed_deny_over_auto_approve(tmp_path) -> None:
    class WritingProvider(FakeProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            assert tools is not None
            if self.calls == 1:
                yield StreamChunk(
                    native_tool_calls=[{
                        "id": "write-deny",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "denied.txt",
                            "content": "denied\n",
                            "overwrite": True,
                        },
                    }],
                    is_done=True,
                )
            else:
                assert any(
                    message.get("role") == "tool"
                    and "matched deny rule" in str(message.get("content"))
                    for message in messages
                )
                yield StreamChunk(content="managed deny honored", is_done=True)

    state = SharedState(tmp_path / "state-deny" / "agents.db")
    config = AshConfig(workspace_root=tmp_path, safety_tier="auto_approve")
    parent_policy = PermissionPolicy(
        "auto_approve",
        managed_rules=[PermissionRule.create("deny", "write_file")],
    )
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, WritingProvider, config=config)
    tool.set_permission_policy_provider(lambda: parent_policy)

    result = await tool.run(
        role="coder",
        task="attempt denied file",
        agent_id="denied-coder",
        isolation="shared",
    )

    assert result.success is True
    assert result.output == "managed deny honored"
    assert not (tmp_path / "denied.txt").exists()
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_subagent_uses_start_time_permission_snapshot(tmp_path) -> None:
    class PausedWritingProvider(FakeProvider):
        started = asyncio.Event()
        release = asyncio.Event()

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            assert tools is not None
            if self.calls == 1:
                type(self).started.set()
                await type(self).release.wait()
                yield StreamChunk(
                    native_tool_calls=[
                        {
                            "id": "write-snapshot",
                            "name": "write_file",
                            "arguments": {
                                "file_path": "snapshot.txt",
                                "content": "snapshot\n",
                                "overwrite": True,
                            },
                        }
                    ],
                    is_done=True,
                )
            else:
                yield StreamChunk(content="snapshot complete", is_done=True)

    PausedWritingProvider.started = asyncio.Event()
    PausedWritingProvider.release = asyncio.Event()
    state = SharedState(tmp_path / "state-snapshot" / "agents.db")
    config = AshConfig(workspace_root=tmp_path, safety_tier="interactive")
    parent_policy = PermissionPolicy(
        "interactive",
        session_rules=[PermissionRule.create("allow", "write_file")],
    )
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path), state, PausedWritingProvider, config=config
    )
    tool.set_permission_policy_provider(lambda: parent_policy)

    started = await tool.run(
        role="coder",
        task="write from snapshot",
        agent_id="snapshot-coder",
        isolation="shared",
        background=True,
    )
    assert started.success is True
    await PausedWritingProvider.started.wait()
    parent_policy.session_rules.clear()
    PausedWritingProvider.release.set()
    report = await tool._tasks["snapshot-coder"]

    assert report.success is True
    assert report.summary == "snapshot complete"
    assert (tmp_path / "snapshot.txt").read_text(encoding="utf-8") == "snapshot\n"
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

    background_task = tool._tasks["stopped-worker"]
    await asyncio.wait_for(
        asyncio.gather(background_task, return_exceptions=True),
        timeout=2.0,
    )

    message = next(
        item
        for item in state.fetch_messages("stopped-worker", undelivered_only=False)
        if item.message_id == message_id
    )
    assert message.delivered is True
    status = state.get_status("stopped-worker")
    assert status is not None
    assert status.current_task == "stopped by persisted message"
    durable_task = state.tasks.get_task(str(status.metadata["durable_task_id"]))
    assert durable_task is not None
    assert durable_task.state == "cancelled"
    assert durable_task.error == "stopped by persisted message"
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
