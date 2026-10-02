import asyncio
import json
import sys
from pathlib import Path

import pytest

from ash.agents.shared_state import SharedState
from ash.agents.tasks import AgentTaskError
from ash.core.goals import GoalState
from ash.sdk import AshClient, AshResult
from ash.config import AshConfig
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.failover import FailoverProvider
from ash.sandbox import SandboxBackendUnavailable
from ash.safety.grants import PermissionRule, RuleEffect


class SDKProvider(ProviderABC):
    @property
    def model_name(self) -> str:
        return "sdk-model"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="<response>sdk ")
        yield StreamChunk(
            content="response</response>",
            is_done=True,
            prompt_tokens=100,
            completion_tokens=10,
            cache_read_tokens=80,
            cache_write_tokens=5,
        )


class SerialProvider(SDKProvider):
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.02)
            yield StreamChunk(content="<response>done</response>", is_done=True)
        finally:
            self.active -= 1


class VisionSDKProvider(SDKProvider):
    _ash_declared_capabilities = ProviderCapabilities(vision=True)

    def __init__(self) -> None:
        self.messages = []

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.messages = list(messages)
        yield StreamChunk(content="<response>image ok</response>", is_done=True)


class SteeringSDKProvider(SDKProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.received_messages = []

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.calls += 1
        self.received_messages.append(list(messages))
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
            yield StreamChunk(content="initial", is_done=True)
        else:
            yield StreamChunk(content="redirected", is_done=True)


class GoalStreamingSDKProvider(SDKProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        del messages, temperature, tools
        self.calls += 1
        outputs = {
            1: '<call_tool name="read_file"><arg name="file_path">goal.txt</arg></call_tool>',
            2: "<response>First Goal step complete.</response>",
            3: (
                '<call_tool name="update_goal">'
                '<arg name="action">complete</arg>'
                '<arg name="evidence">goal.txt was read successfully</arg>'
                "</call_tool>"
            ),
            4: "<response>Goal complete.</response>",
        }
        yield StreamChunk(
            content=outputs[self.calls],
            is_done=True,
            prompt_tokens=10,
            completion_tokens=2,
        )


@pytest.mark.asyncio
async def test_async_sdk_rejects_unisolated_auto_approve(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "ash.sandbox.manager.has_docker",
        lambda _image, *, workspace_root=None: False,
    )
    monkeypatch.setattr(
        "ash.sandbox.manager.has_bwrap", lambda _workspace=None: False
    )
    monkeypatch.setattr(
        "ash.sandbox.manager.has_sandbox_exec", lambda _workspace=None: False
    )
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        safety_tier="auto_approve",
    )

    with pytest.raises(
        SandboxBackendUnavailable,
        match="aggregate CPU and memory containment",
    ):
        await AshClient.create(config=config, provider=SDKProvider())


@pytest.mark.asyncio
async def test_async_sdk_create_cancellation_closes_allocated_runtime(
    tmp_path, monkeypatch
) -> None:
    started = asyncio.Event()

    class FakeLoop:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    loop = FakeLoop()
    runtime = type("Runtime", (), {"loop": loop})()
    monkeypatch.setattr("ash.sdk.build_runtime", lambda *args, **kwargs: runtime)

    async def blocked_start(self, session_id=None):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AshClient, "start", blocked_start)
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    task = asyncio.create_task(
        AshClient.create(config=config, provider=SDKProvider())
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.closed is True


@pytest.mark.asyncio
async def test_async_sdk_create_preserves_start_failure_when_cleanup_fails(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeLoop:
        async def aclose(self) -> None:
            raise RuntimeError("SDK runtime cleanup failed")

    runtime = type("Runtime", (), {"loop": FakeLoop()})()
    monkeypatch.setattr("ash.sdk.build_runtime", lambda *args, **kwargs: runtime)

    async def fail_start(self, session_id=None):
        del session_id
        raise RuntimeError("SDK startup failed")

    monkeypatch.setattr(AshClient, "start", fail_start)
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )

    with pytest.raises(RuntimeError, match="SDK startup failed") as failure:
        await AshClient.create(config=config, provider=SDKProvider())

    assert any(
        "SDK runtime cleanup failed" in note for note in failure.value.__notes__
    )


@pytest.mark.asyncio
async def test_async_sdk_owns_runtime_and_sessions(tmp_path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    client = await AshClient.create(config=config, provider=SDKProvider())
    try:
        assert client.loop.repo_map is not None
        assert "find_symbol" in client.loop.tools
        assert "find_references" in client.loop.tools
        assert client.loop.tools["find_symbol"].repo_map is client.loop.repo_map
        assert client.loop.tools["find_references"].repo_map is client.loop.repo_map
        result = await client.prompt("hello")
        assert result.response == "sdk response"
        assert result.session_id
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 10
        assert result.cache_read_tokens == 80
        assert result.cache_write_tokens == 5
        assert result.usage_source == "provider"
        assert result.usage["has_estimates"] is False
        assert result.usage["cache_hit_rate"] == 0.8
        assert result.model == "ollama/sdk-model"
        assert client.sessions()[0].session_id == result.session_id
        assert client.sessions()[0].model == "ollama/sdk-model"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_async_sdk_exposes_durable_goal_lifecycle(tmp_path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )

    async with await AshClient.create(config=config, provider=SDKProvider()) as client:
        result = await client.run_goal("Verify the SDK Goal lifecycle")
        goal = client.current_goal()

        assert result.response == "sdk response"
        assert goal is not None
        assert goal.objective == "Verify the SDK Goal lifecycle"
        assert goal.state is GoalState.ACTIVE

        paused = await client.pause_goal()
        assert paused is not None
        assert paused.state is GoalState.PAUSED
        assert client.current_goal() == paused

        resumed_result = await client.resume_goal()
        resumed = client.current_goal()
        assert resumed_result.response == "sdk response"
        assert resumed is not None
        assert resumed.state is GoalState.ACTIVE

        cleared = await client.clear_goal()
        assert cleared.state is GoalState.CLEARED
        assert client.current_goal() is None


@pytest.mark.asyncio
async def test_sdk_goal_stream_has_one_terminal_event_and_aggregate_usage(
    tmp_path,
) -> None:
    (tmp_path / "goal.txt").write_text("evidence\n", encoding="utf-8")
    provider = GoalStreamingSDKProvider()
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )

    async with await AshClient.create(config=config, provider=provider) as client:
        client.loop.create_goal("Read goal.txt and verify it")
        events = [event async for event in client.stream_prompt("Start the Goal")]

    event_types = [event.type for event in events]
    assert event_types.count("goal.step.completed") == 2
    assert event_types.count("turn.completed") == 1
    assert event_types[-1] == "turn.completed"
    terminal = events[-1]
    usage = terminal.data["usage"]
    assert usage["prompt_tokens"] == 40
    assert usage["completion_tokens"] == 8
    assert provider.calls == 4


@pytest.mark.asyncio
async def test_async_sdk_runtime_is_terminal_after_close(tmp_path: Path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )
    client = await AshClient.create(config=config, provider=SDKProvider())
    active = client.loop.current_session
    assert active is not None
    session_id = active.session_id
    before = [item.session_id for item in client.sessions(limit=100)]

    await client.close()

    assert [item.session_id for item in client.sessions(limit=100)] == before
    with pytest.raises(RuntimeError, match="Ash client runtime is closed"):
        await client.start()
    with pytest.raises(RuntimeError, match="Ash client runtime is closed"):
        await client.prompt("after close")
    with pytest.raises(RuntimeError, match="Ash client runtime is closed"):
        await client.new_session()
    with pytest.raises(RuntimeError, match="Ash client runtime is closed"):
        await client.fork(session_id, branch_name="after-close")

    assert [item.session_id for item in client.sessions(limit=100)] == before


@pytest.mark.asyncio
async def test_async_sdk_accepts_image_only_turn_without_persisting_raw_data(
    tmp_path,
) -> None:
    provider = VisionSDKProvider()
    config = AshConfig(
        model="custom/vision-sdk",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )
    metadata = {
        "content_blocks": [
            {"type": "image", "media_type": "image/png", "data": "YWJj"}
        ],
        "images": [
            {"source": "sdk-inline", "media_type": "image/png", "sha256": "digest"}
        ],
    }

    async with await AshClient.create(config=config, provider=provider) as client:
        result = await client.prompt("", user_metadata=metadata)
        loaded = client.loop.session_store.load_session(result.session_id)

    assert result.response == "image ok"
    user = next(item for item in provider.messages if item["role"] == "user")
    assert user["content"] == [
        {"type": "image", "media_type": "image/png", "data": "YWJj"}
    ]
    assert "content_blocks" not in loaded.messages[0].metadata
    assert loaded.messages[0].metadata["images"][0]["source"] == "sdk-inline"


@pytest.mark.asyncio
async def test_async_sdk_still_rejects_truly_empty_turn(tmp_path) -> None:
    config = AshConfig(
        model="custom/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )
    async with await AshClient.create(config=config, provider=SDKProvider()) as client:
        with pytest.raises(ValueError, match="prompt cannot be empty"):
            await client.prompt("", user_metadata={"source": "metadata-only"})
        with pytest.raises(ValueError, match="prompt cannot be empty"):
            await client.prompt(
                "",
                user_metadata={
                    "content_blocks": [{"type": "text", "text": ""}]
                },
            )


@pytest.mark.asyncio
async def test_async_sdk_reports_actual_failover_model_identity(tmp_path) -> None:
    class FailingPrimary(SDKProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            raise RuntimeError("primary unavailable")
            yield  # pragma: no cover

    primary = FailingPrimary()
    primary.provider_family = "primary"
    class BackupProvider(SDKProvider):
        @property
        def model_name(self) -> str:
            return "backup-model"

    backup = BackupProvider()
    backup.provider_family = "backup"
    provider = FailoverProvider([primary, backup])
    config = AshConfig(
        model="primary/sdk-model",
        fallback_models=["backup/backup-model"],
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
        model_pricing_usd_per_million={
            "backup/backup-model": {"input": 2.0, "output": 10.0}
        },
    )

    async with await AshClient.create(config=config, provider=provider) as client:
        result = await client.prompt("hello")
        events = client.events(result.session_id, limit=100)

    assert result.model == "backup/backup-model"
    assert result.cost_usd == pytest.approx(0.0003)
    assert result.cost_known is True
    completion = next(item.event for item in events if item.event.type == "turn.completed")
    assert completion.data["model"] == "backup-model"
    assert completion.data["model_id"] == "backup/backup-model"


def test_sdk_usage_distinguishes_unknown_pricing_from_zero_cost() -> None:
    result = AshResult(
        response="done",
        session_id="session",
        model="custom/unpriced",
        context_tokens=10,
        prompt_tokens=8,
        completion_tokens=2,
        cost_usd=0.0,
        usage_source="provider",
        cost_known=False,
    )

    assert result.usage["cost_usd"] == 0.0
    assert result.usage["cost_known"] is False
    assert result.usage["cost_is_estimated"] is False



@pytest.mark.asyncio
async def test_sdk_exposes_typed_durable_agent_tasks_and_artifacts(tmp_path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    client = await AshClient.create(config=config, provider=SDKProvider())
    state = SharedState(config.db_directory / "agents.db")
    workspace = str(tmp_path.resolve())
    try:
        state.tasks.create_task(
            "sdk task",
            task_id="sdk-task",
            metadata={"workspace": workspace},
        )
        lease = state.tasks.claim_task("sdk-worker", task_id="sdk-task")
        assert lease is not None
        state.tasks.complete_task("sdk-task", lease.token, {"summary": "done"})
        state.tasks.add_artifact("sdk-task", kind="report", uri="artifact://sdk")
        state.tasks.create_task(
            "cancel through sdk",
            task_id="sdk-cancel",
            metadata={"graph_id": "sdk-graph", "workspace": workspace},
        )

        tasks = client.agent_tasks(state="succeeded", owner_agent_id="sdk-worker")
        artifacts = client.agent_artifacts("sdk-task")
        events = client.agent_task_events(
            task_id="sdk-task", event_type="agent.task.succeeded"
        )

        assert [task.task_id for task in tasks] == ["sdk-task"]
        assert tasks[0].result == {"summary": "done"}
        assert [artifact.uri for artifact in artifacts] == ["artifact://sdk"]
        assert [event.event["type"] for event in events] == ["agent.task.succeeded"]
        assert [task.task_id for task in client.agent_tasks(graph_id="sdk-graph")] == [
            "sdk-cancel"
        ]
        assert client.cancel_agent_graph("sdk-graph") == ["sdk-cancel"]
        assert state.tasks.get_task("sdk-cancel").state == "cancelled"
    finally:
        state.close()
        await client.close()


@pytest.mark.asyncio
async def test_sdk_agent_state_is_scoped_to_client_workspace(tmp_path) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    database = tmp_path / "shared-db"
    first_config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=first_workspace,
        db_directory=database,
        memory_backend="off",
    )
    second_config = first_config.model_copy(
        update={"workspace_root": second_workspace}
    )
    first = await AshClient.create(config=first_config, provider=SDKProvider())
    second = await AshClient.create(config=second_config, provider=SDKProvider())
    state = SharedState(database / "agents.db")
    try:
        first_task = state.tasks.create_task(
            "first workspace task",
            task_id="task-a",
            metadata={
                "graph_id": "graph-a",
                "workspace": str(first_workspace.resolve()),
            },
        )
        second_task = state.tasks.create_task(
            "second workspace task",
            task_id="task-b",
            metadata={
                "graph_id": "graph-b",
                "workspace": str(second_workspace.resolve()),
            },
        )
        state.tasks.add_artifact(
            second_task.task_id,
            kind="report",
            uri="artifact://second-workspace",
        )

        assert [task.task_id for task in first.agent_tasks()] == [first_task.task_id]
        assert first.agent_artifacts(second_task.task_id) == []
        assert first.agent_task_events(task_id=second_task.task_id) == []
        with pytest.raises(AgentTaskError, match="unknown task graph"):
            first.cancel_agent_graph("graph-b")
        assert state.tasks.get_task(second_task.task_id).state == "queued"

        assert [task.task_id for task in second.agent_tasks()] == [second_task.task_id]
        assert [artifact.uri for artifact in second.agent_artifacts(second_task.task_id)] == [
            "artifact://second-workspace"
        ]
        assert second.agent_task_events(task_id=second_task.task_id)
        assert second.cancel_agent_graph("graph-b") == [second_task.task_id]
        assert state.tasks.get_task(second_task.task_id).state == "cancelled"
    finally:
        state.close()
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_sdk_freezes_relative_database_path_across_cwd_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    workspace = tmp_path / "workspace"
    first_cwd.mkdir()
    second_cwd.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(first_cwd)
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=workspace,
        db_directory=Path("relative-db"),
        memory_backend="off",
        repo_map_enabled=False,
    )
    assert config.db_directory == first_cwd / "relative-db"
    client = await AshClient.create(
        config=config,
        provider=SDKProvider(),
        run_maintenance=False,
    )
    state = SharedState(config.db_directory / "agents.db")
    try:
        state.tasks.create_task(
            "stable relative database",
            task_id="stable-db-task",
            metadata={"workspace": str(workspace.resolve())},
        )
        assert [task.task_id for task in client.agent_tasks()] == ["stable-db-task"]

        monkeypatch.chdir(second_cwd)

        assert [task.task_id for task in client.agent_tasks()] == ["stable-db-task"]
        assert client.config.db_directory == first_cwd / "relative-db"
    finally:
        state.close()
        await client.close()


@pytest.mark.asyncio
async def test_sdk_freezes_workspace_target_and_rejects_inode_replacement(
    tmp_path: Path,
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    workspace_link = tmp_path / "workspace"
    first_workspace.mkdir()
    second_workspace.mkdir()
    try:
        workspace_link.symlink_to(first_workspace, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=workspace_link,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )
    assert config.workspace_root == first_workspace.resolve()
    client = await AshClient.create(
        config=config,
        provider=SDKProvider(),
        run_maintenance=False,
    )
    moved = tmp_path / "first-original"
    try:
        first_session = client.loop.current_session
        assert first_session is not None
        workspace_link.unlink()
        workspace_link.symlink_to(second_workspace, target_is_directory=True)

        await client.new_session()

        assert client.loop.current_session is not None
        assert client.loop.current_session.project_path == str(first_workspace.resolve())
        assert client.loop.safety_guard.project_root == first_workspace.resolve()

        first_workspace.rename(moved)
        first_workspace.mkdir()
        with pytest.raises(RuntimeError, match="workspace root changed"):
            await client.new_session()
    finally:
        if first_workspace.exists() and moved.exists():
            first_workspace.rmdir()
            moved.rename(first_workspace)
        await client.close()


@pytest.mark.asyncio
async def test_sdk_delegates_durable_graph_with_injected_agent_provider(
    tmp_path,
) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        safety_tier="auto_approve",
        allow_unsafe_auto_approve=True,
    )
    client = await AshClient.create(
        config=config,
        provider=SDKProvider(),
        agent_provider_factory=SDKProvider,
    )
    try:
        result = await client.delegate_agents(
            "sdk graph",
            [
                {
                    "key": "inspect",
                    "role": "reviewer",
                    "task": "inspect through sdk",
                    "isolation": "shared",
                }
            ],
        )

        assert result.success is True
        assert result.tasks[0]["state"] == "succeeded"
        task = client.agent_tasks(state="succeeded")[0]
        assert task.metadata["graph_id"] == result.graph_id
        assert task.result["summary"] == "sdk response"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sdk_delegation_respects_dry_run_without_creating_tasks(tmp_path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        safety_tier="dry_run",
    )
    client = await AshClient.create(
        config=config,
        provider=SDKProvider(),
        agent_provider_factory=SDKProvider,
    )
    try:
        with pytest.raises(RuntimeError, match="dry-run mode forbids side effects"):
            await client.delegate_agents(
                "blocked sdk graph",
                [
                    {
                        "key": "inspect",
                        "role": "reviewer",
                        "task": "must not run through sdk",
                        "isolation": "shared",
                    }
                ],
            )

        assert client.agent_tasks() == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sdk_delegation_respects_explicit_deny_without_creating_tasks(
    tmp_path,
) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        safety_tier="auto_approve",
        allow_unsafe_auto_approve=True,
    )
    client = await AshClient.create(
        config=config,
        provider=SDKProvider(),
        agent_provider_factory=SDKProvider,
    )
    client.loop.permission_policy.add_session_rule(
        PermissionRule.create(RuleEffect.DENY, "delegate_agents")
    )
    try:
        with pytest.raises(RuntimeError, match="matched deny rule"):
            await client.delegate_agents(
                "denied sdk graph",
                [
                    {
                        "key": "inspect",
                        "role": "reviewer",
                        "task": "must not run through sdk",
                        "isolation": "shared",
                    }
                ],
            )

        assert client.agent_tasks() == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_async_sdk_forks_activates_and_exposes_session_tree(tmp_path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    async with await AshClient.create(config=config, provider=SDKProvider()) as client:
        result = await client.prompt("hello")
        forked_id = await client.fork(
            result.session_id,
            branch_name="alternate",
            branch_summary="try another implementation",
        )
        tree = client.session_tree()
        followup = await client.prompt("continue here")

    assert forked_id != result.session_id
    assert followup.session_id == forked_id
    assert [node.session_id for node in tree] == [result.session_id, forked_id]
    assert tree[0].children == (forked_id,)
    assert tree[1].parent_session_id == result.session_id
    assert tree[1].branch_name == "alternate"


@pytest.mark.asyncio
async def test_async_sdk_applies_trusted_project_extensions(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    skill = workspace / ".ash" / "skills" / "project-review"
    skill.mkdir(parents=True)
    (workspace / "ASH.md").write_text(
        "Always include PROJECT_RUNTIME_INSTRUCTION.", encoding="utf-8"
    )
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: project-review\n"
        "description: Review this project\n"
        "---\n"
        "Review the project carefully.\n",
        encoding="utf-8",
    )
    hooks = workspace / ".ash" / "hooks.json"
    hooks.write_text(
        json.dumps(
            {
                "pre_tool": [{"matcher": "write_file", "command": ["true"]}],
                "session_start": [
                    {
                        "command": [
                            sys.executable,
                            "-c",
                            "import os; print(os.getcwd() + '|' + "
                            "os.environ['ASH_PROJECT_ROOT'])",
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
        allowed_web_domains=["docs.example.com"],
    )

    async with await AshClient.create(
        config=config,
        provider=SDKProvider(),
        workspace_trusted=True,
    ) as client:
        listed = await client.loop.tools["list_skills"].run()
        web_tool = client.loop.tools["web_fetch"]

        assert "PROJECT_RUNTIME_INSTRUCTION" in client.loop.system_prompt
        assert f"{workspace}|{workspace}" in client.loop.system_prompt
        assert "project-review: Review this project" in listed.output
        assert client.loop.hooks is not None
        assert len(client.loop.hooks._pre_tool) == 1
        assert web_tool._allowed_domains == ("docs.example.com",)


@pytest.mark.asyncio
async def test_async_sdk_excludes_untrusted_project_extensions(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    skill = workspace / ".ash" / "skills" / "project-review"
    skill.mkdir(parents=True)
    (workspace / "ASH.md").write_text(
        "Always include UNTRUSTED_PROJECT_INSTRUCTION.", encoding="utf-8"
    )
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: project-review\n"
        "description: Review this project\n"
        "---\n"
        "Review the project carefully.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )

    async with await AshClient.create(
        config=config,
        provider=SDKProvider(),
        workspace_trusted=False,
    ) as client:
        listed = await client.loop.tools["list_skills"].run()

        assert "UNTRUSTED_PROJECT_INSTRUCTION" not in client.loop.system_prompt
        assert "project-review" not in listed.output


@pytest.mark.asyncio
async def test_async_sdk_delivers_full_command_hook_lifecycle(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    hooks = home / ".ash" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    output = tmp_path / "events.jsonl"
    command = [
        sys.executable,
        "-c",
        "import json,sys; p=json.load(sys.stdin); "
        "open(sys.argv[1],'a').write(json.dumps(p)+'\\n'); "
        "print(json.dumps({'additional_context':'SESSION CONTEXT'}) "
        "if p['event']=='session_start' else '')",
        str(output),
    ]
    hooks.write_text(
        json.dumps(
            {
                event: [{"command": command}]
                for event in (
                    "session_start",
                    "turn_start",
                    "pre_model",
                    "post_model",
                    "turn_end",
                    "session_end",
                )
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )

    client = await AshClient.create(config=config, provider=SDKProvider())
    result = await client.prompt(
        "hello OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz"
    )
    new_session_id = await client.new_session()
    assert client.loop.system_prompt.count("SESSION CONTEXT") == 1
    await client.close()

    events = [json.loads(line) for line in output.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "session_start",
        "turn_start",
        "pre_model",
        "post_model",
        "turn_end",
        "session_end",
        "session_start",
        "session_end",
    ]
    assert all(event["schema_version"] == 1 for event in events)
    assert all(event["session_id"] == result.session_id for event in events[:6])
    assert all(event["session_id"] == new_session_id for event in events[6:])
    assert events[0]["source"] == "new"
    assert "sk-proj" not in events[1]["input"]
    assert events[4]["status"] == "completed"
    assert events[5]["reason"] == "switch"
    assert events[7]["reason"] == "shutdown"


@pytest.mark.asyncio
async def test_async_sdk_can_disable_repository_map(tmp_path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )
    async with await AshClient.create(config=config, provider=SDKProvider()) as client:
        assert client.loop.repo_map is None


@pytest.mark.asyncio
async def test_async_sdk_streams_real_turn_events(tmp_path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    async with await AshClient.create(config=config, provider=SDKProvider()) as client:
        events = [event async for event in client.stream_prompt("hello")]

    assert events[0].type == "turn.started"
    assert all(event.schema_version == 1 for event in events)
    assert len({event.event_id for event in events}) == len(events)
    assert all(event.timestamp for event in events)
    assert all(event.source == {"type": "runtime", "id": "ash"} for event in events)
    assert any(event.type == "context.usage" for event in events)
    assert (
        "".join(
            event.data["text"] for event in events if event.type == "assistant.delta"
        )
        == "sdk response"
    )
    assert events[-1].type == "turn.completed"
    assert events[-1].session_id == events[-1].data["session_id"]
    assert events[-1].data["response"] == "sdk response"
    assert events[-1].data["model"] == "sdk-model"
    assert events[-1].data["model_id"] == "ollama/sdk-model"
    assert events[-1].data["usage"]["cache_read_tokens"] == 80
    replay = client.loop.session_store.list_runtime_events(events[-1].session_id or "")
    replay_types = [item.event["type"] for item in replay]
    assert replay_types[0] == "turn.started"
    assert "assistant.delta" in replay_types
    assert replay_types[-1] == "turn.completed"
    assert [item.sequence for item in replay] == sorted(
        item.sequence for item in replay
    )


@pytest.mark.asyncio
async def test_async_sdk_cancelled_event_terminates_stream(tmp_path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    async with await AshClient.create(config=config, provider=SDKProvider()) as client:

        async def cancelled_prompt(text, *, user_metadata=None):
            client.loop.ui.emit_event({"type": "turn.cancelled"})
            raise RuntimeError("cancelled after terminal event")

        client._prompt_unlocked = cancelled_prompt  # type: ignore[method-assign]
        events = [event async for event in client.stream_prompt("cancel")]

    assert [event.type for event in events] == ["turn.cancelled"]


@pytest.mark.asyncio
async def test_async_sdk_stream_fails_closed_when_consumer_falls_behind(
    tmp_path,
    monkeypatch,
) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    monkeypatch.setattr("ash.sdk.SDK_STREAM_EVENT_QUEUE_MAX", 2, raising=False)
    async with await AshClient.create(config=config, provider=SDKProvider()) as client:

        async def bursty_prompt(text, *, user_metadata=None):
            del text, user_metadata
            for index in range(10):
                client.loop.ui.emit_event(
                    {"type": "assistant.delta", "text": str(index)}
                )
            client.loop.ui.emit_event(
                {"type": "turn.completed", "response": "unbounded"}
            )
            return object()

        client._prompt_unlocked = bursty_prompt  # type: ignore[method-assign]
        events = [event async for event in client.stream_prompt("burst")]

    assert events[-1].type == "turn.error"
    assert "fell behind" in events[-1].data["error"]
    assert sum(event.type == "assistant.delta" for event in events) <= 2


@pytest.mark.asyncio
async def test_async_sdk_stream_fails_closed_for_oversized_event(
    tmp_path,
    monkeypatch,
) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    monkeypatch.setattr("ash.sdk.SDK_STREAM_EVENT_QUEUE_MAX", 100)
    monkeypatch.setattr("ash.sdk.SDK_STREAM_EVENT_QUEUE_MAX_BYTES", 256)
    async with await AshClient.create(config=config, provider=SDKProvider()) as client:

        async def oversized_prompt(text, *, user_metadata=None):
            del text, user_metadata
            client.loop.ui.emit_event(
                {"type": "tool.completed", "output": "x" * 2048}
            )
            client.loop.ui.emit_event(
                {"type": "turn.completed", "response": "unbounded"}
            )
            return object()

        client._prompt_unlocked = oversized_prompt  # type: ignore[method-assign]
        events = [event async for event in client.stream_prompt("oversized")]

    assert [event.type for event in events] == ["turn.error"]
    assert events[0].data["category"] == "backpressure"
    assert "fell behind" in events[0].data["error"]


@pytest.mark.asyncio
async def test_async_sdk_rejects_cross_workspace_session_resume(tmp_path) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    database = tmp_path / "db"
    first_config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=first_workspace,
        db_directory=database,
        memory_backend="off",
    )
    async with await AshClient.create(
        config=first_config, provider=SDKProvider()
    ) as first_client:
        first_result = await first_client.prompt("private first-workspace turn")
        session_id = first_result.session_id

    second_config = first_config.model_copy(update={"workspace_root": second_workspace})
    with pytest.raises(ValueError, match="different workspace"):
        await AshClient.create(
            config=second_config,
            provider=SDKProvider(),
            session_id=session_id,
        )

    async with await AshClient.create(
        config=second_config, provider=SDKProvider()
    ) as second_client:
        with pytest.raises(ValueError, match="different workspace"):
            second_client.events(session_id)
        with pytest.raises(ValueError, match="different workspace"):
            second_client.session_tree(session_id)
        before = second_client.loop.session_store.list_sessions(
            project_path=str(first_workspace), limit=100
        )
        with pytest.raises(ValueError, match="different workspace"):
            await second_client.fork(session_id, branch_name="must-not-exist")
        after = second_client.loop.session_store.list_sessions(
            project_path=str(first_workspace), limit=100
        )

    assert [item.session_id for item in after] == [item.session_id for item in before]


@pytest.mark.asyncio
async def test_async_sdk_serializes_prompts_on_one_session(tmp_path) -> None:
    provider = SerialProvider()
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    async with await AshClient.create(config=config, provider=provider) as client:
        first, second = await asyncio.gather(
            client.prompt("first"), client.prompt("second")
        )

    assert first.response == second.response == "done"
    assert provider.maximum_active == 1
    assert first.usage_source == "estimated"
    assert first.estimated_prompt_tokens == first.prompt_tokens > 0
    assert first.estimated_completion_tokens == first.completion_tokens > 0
    assert first.usage["has_estimates"] is True


@pytest.mark.asyncio
async def test_async_sdk_steers_running_turn_without_waiting_for_prompt_lock(
    tmp_path,
) -> None:
    provider = SteeringSDKProvider()
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    async with await AshClient.create(config=config, provider=provider) as client:
        with pytest.raises(RuntimeError, match="no turn"):
            await client.steer("too early")

        prompt = asyncio.create_task(client.prompt("start"))
        await provider.started.wait()
        assert await client.steer("redirect now") == 1
        provider.release.set()
        result = await prompt

    assert result.response == "redirected"
    assert provider.calls == 2
    assert any(
        message["role"] == "user" and message["content"] == "redirect now"
        for message in provider.received_messages[1]
    )


@pytest.mark.asyncio
async def test_sdk_mcp_interactions_require_explicit_headless_callbacks(tmp_path) -> None:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
        mcp_sampling_enabled=True,
        mcp_elicitation_enabled=True,
    )

    without_callbacks = await AshClient.create(
        config=config,
        provider=SDKProvider(),
        workspace_trusted=False,
        run_maintenance=False,
    )
    try:
        interactions = without_callbacks.loop._mcp_interactions
        assert interactions is not None
        assert interactions.supports_sampling is False
        assert interactions.supports_elicitation is False
    finally:
        await without_callbacks.close()

    async def review(_server: str, _stage: str, _payload: dict) -> bool:
        return True

    async def elicit(_server: str, _message: str, _schema: dict) -> dict:
        return {"action": "decline"}

    with_callbacks = await AshClient.create(
        config=config,
        provider=SDKProvider(),
        agent_provider_factory=SDKProvider,
        workspace_trusted=False,
        mcp_sampling_review=review,
        mcp_elicitation_callback=elicit,
        run_maintenance=False,
    )
    try:
        interactions = with_callbacks.loop._mcp_interactions
        assert interactions is not None
        assert interactions.supports_sampling is True
        assert interactions.supports_elicitation is True
    finally:
        await with_callbacks.close()
