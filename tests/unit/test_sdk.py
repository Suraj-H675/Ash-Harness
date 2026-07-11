import asyncio
import json
import sys

import pytest

from agents.shared_state import SharedState
from ash.sdk import AshClient
from config import AshConfig
from providers.base import ProviderABC, StreamChunk
from sandbox import SandboxBackendUnavailable


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


@pytest.mark.asyncio
async def test_async_sdk_rejects_unisolated_auto_approve(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sandbox.manager.has_docker", lambda _image: False)
    monkeypatch.setattr("sandbox.manager.has_bwrap", lambda: False)
    monkeypatch.setattr("sandbox.manager.has_sandbox_exec", lambda: False)
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        safety_tier="auto_approve",
    )

    with pytest.raises(SandboxBackendUnavailable, match="does not isolate"):
        await AshClient.create(config=config, provider=SDKProvider())


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
        assert client.sessions()[0].session_id == result.session_id
    finally:
        await client.close()


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
    try:
        state.tasks.create_task("sdk task", task_id="sdk-task")
        lease = state.tasks.claim_task("sdk-worker", task_id="sdk-task")
        assert lease is not None
        state.tasks.complete_task("sdk-task", lease.token, {"summary": "done"})
        state.tasks.add_artifact("sdk-task", kind="report", uri="artifact://sdk")

        tasks = client.agent_tasks(state="succeeded", owner_agent_id="sdk-worker")
        artifacts = client.agent_artifacts("sdk-task")

        assert [task.task_id for task in tasks] == ["sdk-task"]
        assert tasks[0].result == {"summary": "done"}
        assert [artifact.uri for artifact in artifacts] == ["artifact://sdk"]
    finally:
        state.close()
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
                "pre_tool": [
                    {"matcher": "write_file", "command": ["true"]}
                ],
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
    assert events[-1].data["usage"]["cache_read_tokens"] == 80
    replay = client.loop.session_store.list_runtime_events(
        events[-1].session_id or ""
    )
    replay_types = [item.event["type"] for item in replay]
    assert replay_types[0] == "turn.started"
    assert "assistant.delta" in replay_types
    assert replay_types[-1] == "turn.completed"
    assert [item.sequence for item in replay] == sorted(
        item.sequence for item in replay
    )


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
