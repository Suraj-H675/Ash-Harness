from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ash.agents.shared_state import SharedState
from ash.agents.tasks import AgentTaskCreate, AgentTaskError
from ash.config import AshConfig
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.grants import PermissionRule
from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy
from ash.tools.agent import SpawnAgentTool
from ash.tools.delegate import DelegateAgentsTool


class RecordingProvider(ProviderABC):
    model_name = "graph-provider"
    calls: list[str] = []
    active = 0
    max_active = 0

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        task = str(messages[-1]["content"])
        type(self).calls.append(task)
        type(self).active += 1
        type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            await asyncio.sleep(0.05)
            yield StreamChunk(content=f"completed: {task}", is_done=True)
        finally:
            type(self).active -= 1

    def count_tokens(self, text: str) -> int:
        return len(text.split())


class CostlyProvider(ProviderABC):
    provider_family = "cost-provider"
    model_name = "cost-provider"

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="completed expensive task", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


class FailFirstTaskProvider(ProviderABC):
    model_name = "retry-provider"
    calls = 0

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        type(self).calls += 1
        if type(self).calls == 1:
            raise RuntimeError("transient task failure")
        yield StreamChunk(content="recovered", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


class CancellableProvider(ProviderABC):
    model_name = "cancellable-provider"
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        type(self).started.set()
        try:
            await asyncio.Event().wait()
        finally:
            type(self).cancelled.set()
        yield  # pragma: no cover

    def count_tokens(self, text: str) -> int:
        return len(text.split())


class WorktreeHandoffProvider(ProviderABC):
    model_name = "handoff-provider"
    _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

    def __init__(self) -> None:
        self.calls = 0
        self.task = ""

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.calls += 1
        if not self.task:
            self.task = str(messages[-1]["content"])
        if self.task == "produce change":
            if self.calls == 1:
                yield StreamChunk(
                    native_tool_calls=[
                        {
                            "id": "write-producer",
                            "name": "write_file",
                            "arguments": {
                                "file_path": "file.txt",
                                "content": "producer\n",
                                "overwrite": True,
                            },
                        }
                    ],
                    is_done=True,
                )
            else:
                yield StreamChunk(content="producer complete", is_done=True)
            return
        assert any(
            "Dependency handoff follows" in str(message["content"])
            and "producer complete" in str(message["content"])
            for message in messages
            if message["role"] == "system"
        )
        if self.calls == 1:
            yield StreamChunk(
                native_tool_calls=[
                    {
                        "id": "read-consumer",
                        "name": "read_file",
                        "arguments": {"file_path": "file.txt"},
                    }
                ],
                is_done=True,
            )
        elif self.calls == 2:
            assert "producer" in str(messages[-1]["content"])
            yield StreamChunk(
                native_tool_calls=[
                    {
                        "id": "write-consumer",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "file.txt",
                            "content": "consumer\n",
                            "overwrite": True,
                        },
                    }
                ],
                is_done=True,
            )
        else:
            yield StreamChunk(content="consumer complete", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


@pytest.fixture(autouse=True)
def reset_provider() -> None:
    RecordingProvider.calls = []
    RecordingProvider.active = 0
    RecordingProvider.max_active = 0
    FailFirstTaskProvider.calls = 0
    CancellableProvider.started = asyncio.Event()
    CancellableProvider.cancelled = asyncio.Event()


def _tools(tmp_path: Path, *, max_concurrency: int = 2):
    config = AshConfig(
        model_pricing_usd_per_million={
            "cost-provider": {"input": 1.0, "output": 2.0},
        },
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        max_concurrent_agents=max_concurrency,
        agent_token_budget=100,
        agent_time_budget_seconds=10,
        memory_backend="off",
    )
    db_path = config.db_directory / "agents.db"
    spawn = SpawnAgentTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        RecordingProvider,
        config=config,
    )
    delegate = DelegateAgentsTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        spawn,
        config,
    )
    return config, spawn, delegate


@pytest.mark.asyncio
async def test_delegate_agents_runs_provider_in_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            requests.append((self.path, self.headers.get("Authorization", "")))
            body = (
                'data: {"id":"delegate-child","choices":[{"delta":{"content":"delegated evidence"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"delegate-child","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    parent_factory_calls = 0

    def parent_factory() -> ProviderABC:
        nonlocal parent_factory_calls
        parent_factory_calls += 1
        raise AssertionError("delegated subprocess must not call parent factory")

    monkeypatch.setenv("OPENAI_API_KEY", "delegate-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/delegate-child",
        agent_execution_mode="subprocess",
        max_concurrent_agents=2,
        agent_token_budget=100,
        agent_time_budget_seconds=10,
        memory_backend="off",
    )
    db_path = config.db_directory / "agents.db"
    spawn = SpawnAgentTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        parent_factory,
        config=config,
        provider_config_backed=True,
    )
    delegate = DelegateAgentsTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        spawn,
        config,
    )
    try:
        result = await delegate.run(
            goal="inspect implementation",
            tasks=[
                {
                    "key": "review",
                    "role": "reviewer",
                    "task": "inspect tests",
                    "isolation": "shared",
                }
            ],
        )

        assert result.success is True, result.error
        assert parent_factory_calls == 0
        assert requests == [
            ("/v1/chat/completions", "Bearer delegate-secret")
        ]
        tasks = spawn._shared_state.tasks.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].state == "succeeded"
        assert tasks[0].owner_agent_id is not None
        assert tasks[0].owner_agent_id.endswith("-review-a1")
        assert tasks[0].result is not None
        assert tasks[0].result["summary"] == "delegated evidence"
    finally:
        await spawn.aclose()
        delegate._shared_state.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_delegate_subprocess_retry_survives_failed_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            nonlocal requests
            requests += 1
            if requests == 1:
                body = b'{"error":{"message":"first attempt failed"}}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = (
                'data: {"id":"retry-child","choices":[{"delta":{"content":"retry succeeded"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"retry-child","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def forbidden_parent_factory() -> ProviderABC:
        raise AssertionError("delegated subprocess must not call parent factory")

    monkeypatch.setenv("OPENAI_API_KEY", "retry-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/retry-child",
        agent_execution_mode="subprocess",
        provider_max_attempts=1,
        provider_retry_base_delay=0,
        provider_retry_max_delay=0,
        max_concurrent_agents=1,
        agent_token_budget=100,
        agent_time_budget_seconds=10,
        memory_backend="off",
    )
    db_path = config.db_directory / "agents.db"
    spawn = SpawnAgentTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        forbidden_parent_factory,
        config=config,
        provider_config_backed=True,
    )
    delegate = DelegateAgentsTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        spawn,
        config,
    )
    try:
        result = await delegate.run(
            goal="retry subprocess",
            tasks=[
                {
                    "key": "retry",
                    "role": "reviewer",
                    "task": "retry task",
                    "isolation": "shared",
                    "max_attempts": 2,
                }
            ],
        )

        assert result.success is True, result.error
        assert requests == 2
        task = spawn._shared_state.tasks.list_tasks()[0]
        assert task.state == "succeeded"
        assert task.attempt == 2
        assert task.owner_agent_id is not None
        assert task.owner_agent_id.endswith("-a2")
        assert task.result is not None
        assert task.result["summary"] == "retry succeeded"
        assert "agent.task.retrying" in {
            event.event["type"] for event in spawn._shared_state.tasks.list_events()
        }
    finally:
        await spawn.aclose()
        delegate._shared_state.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_delegate_agents_runs_dependency_dag_and_aggregates_results(
    tmp_path: Path,
) -> None:
    config, spawn, delegate = _tools(tmp_path)
    emitted: list[dict] = []
    delegate.set_event_sink(emitted.append)
    result = await delegate.run(
        goal="ship change",
        tasks=[
            {
                "key": "review",
                "role": "reviewer",
                "task": "review implementation",
                "depends_on": ["implement"],
                "isolation": "shared",
            },
            {
                "key": "implement",
                "role": "reviewer",
                "task": "implement change",
                "isolation": "shared",
            },
        ],
    )

    assert result.success is True
    payload = json.loads(result.output)
    assert [task["state"] for task in payload["tasks"]] == [
        "succeeded",
        "succeeded",
    ]
    assert RecordingProvider.calls == ["implement change", "review implementation"]
    assert [event["type"] for event in emitted] == [
        "agent.graph.created",
        "agent.graph.completed",
    ]
    state = SharedState(config.db_directory / "agents.db")
    try:
        tasks = {task.metadata["task_key"]: task for task in state.tasks.list_tasks()}
        assert tasks["review"].dependencies == (tasks["implement"].task_id,)
        assert tasks["review"].result["summary"] == "completed: review implementation"
    finally:
        state.close()
        await delegate.aclose()
        await spawn.aclose()


@pytest.mark.asyncio
async def test_delegate_agents_do_not_bypass_parent_permission_policy(tmp_path: Path) -> None:
    class WritingProvider(ProviderABC):
        model_name = "delegate-writer"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            assert tools is not None
            if self.calls == 1:
                yield StreamChunk(
                    native_tool_calls=[
                        {
                            "id": "delegate-write",
                            "name": "write_file",
                            "arguments": {
                                "file_path": "delegated.txt",
                                "content": "blocked\n",
                                "overwrite": True,
                            },
                        }
                    ],
                    is_done=True,
                )
            else:
                assert any(
                    message.get("role") == "tool"
                    and "matched deny rule" in str(message.get("content"))
                    for message in messages
                )
                yield StreamChunk(content="delegated write denied", is_done=True)

        def count_tokens(self, text: str) -> int:
            return len(text.split())

    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "delegate-policy-db",
        safety_tier="auto_approve",
        max_concurrent_agents=1,
        agent_token_budget=100,
        agent_time_budget_seconds=10,
        memory_backend="off",
    )
    db_path = config.db_directory / "agents.db"
    spawn = SpawnAgentTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        WritingProvider,
        config=config,
    )
    parent_policy = PermissionPolicy(
        "auto_approve",
        managed_rules=[PermissionRule.create("deny", "write_file")],
    )
    spawn.set_permission_policy_provider(lambda: parent_policy)
    delegate = DelegateAgentsTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        spawn,
        config,
    )

    result = await delegate.run(
        goal="attempt managed write",
        tasks=[
            {
                "key": "write",
                "role": "coder",
                "task": "write delegated file",
                "isolation": "shared",
            }
        ],
    )

    assert result.success is True
    payload = json.loads(result.output)
    assert payload["tasks"][0]["state"] == "succeeded"
    assert payload["tasks"][0]["result"]["summary"] == "delegated write denied"
    assert not (tmp_path / "delegated.txt").exists()
    await delegate.aclose()
    await spawn.aclose()


@pytest.mark.asyncio
async def test_delegate_worker_never_uses_foreground_approval_broker(tmp_path: Path) -> None:
    class WritingProvider(ProviderABC):
        model_name = "queued-writer"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            assert tools is not None
            if self.calls == 1:
                yield StreamChunk(
                    native_tool_calls=[
                        {
                            "id": "queued-write",
                            "name": "write_file",
                            "arguments": {
                                "file_path": "queued.txt",
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
                yield StreamChunk(content="queued denied", is_done=True)

        def count_tokens(self, text: str) -> int:
            return len(text.split())

    broker_calls: list[tuple[str, str]] = []

    async def broker(agent_id: str, tool_name: str, arguments: dict) -> bool:
        del arguments
        broker_calls.append((agent_id, tool_name))
        return True

    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "queued-policy-db",
        safety_tier="interactive",
        max_concurrent_agents=1,
        agent_token_budget=100,
        agent_time_budget_seconds=10,
        memory_backend="off",
    )
    db_path = config.db_directory / "agents.db"
    spawn = SpawnAgentTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        WritingProvider,
        config=config,
    )
    spawn.set_foreground_approval_broker(broker)
    delegate = DelegateAgentsTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        spawn,
        config,
    )

    delegation = asyncio.create_task(
        delegate.run(
            goal="attempt queued write",
            tasks=[
                {
                    "key": "write",
                    "role": "coder",
                    "task": "write queued file",
                    "isolation": "shared",
                }
            ],
        )
    )
    request = None
    resolver = SharedState(db_path)
    try:
        for _ in range(150):
            requests = resolver.fetch_messages(
                "lead",
                undelivered_only=True,
                limit=100,
                message_type="approval_request",
            )
            request = next(
                (message for message in requests if message.content.get("tool_name") == "write_file"),
                None,
            )
            if request is not None:
                break
            await asyncio.sleep(0.01)
        assert request is not None
        assert broker_calls == []
        resolver.resolve_approval_request(
            request.message_id,
            approved=False,
            feedback="queued worker denied",
        )
    finally:
        resolver.close()

    result = await asyncio.wait_for(delegation, timeout=3.0)
    assert result.success is True
    payload = json.loads(result.output)
    assert payload["tasks"][0]["result"]["summary"] == "queued denied"
    assert broker_calls == []
    assert not (tmp_path / "queued.txt").exists()
    await delegate.aclose()
    await spawn.aclose()


@pytest.mark.asyncio
async def test_delegate_graph_enforces_shared_cost_ceiling(tmp_path: Path):
    config = AshConfig(
        model_pricing_usd_per_million={
            "cost-provider": {"input": 1.0, "output": 2.0},
        },
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        max_concurrent_agents=1,
        agent_token_budget=100,
        agent_time_budget_seconds=10,
        memory_backend="off",
    )
    db_path = config.db_directory / "agents.db"
    provider = CostlyProvider()
    spawn = SpawnAgentTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        lambda: provider,
        config=config,
    )
    delegate = DelegateAgentsTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        spawn,
        config,
    )

    result = await delegate.run(
        goal="ship change",
        graph_cost_budget_usd=0.0001,
        tasks=[
            {
                "key": "implement",
                "role": "reviewer",
                "task": "implement change",
                "isolation": "shared",
            }
        ],
    )

    assert result.success is False
    payload = json.loads(result.output)
    assert payload["graph_cost_budget_usd"] == pytest.approx(0.0001)
    state = SharedState(db_path)
    try:
        task = state.tasks.get_task(payload["tasks"][0]["task_id"])
        budget = state.tasks.get_graph_budget(payload["graph_id"])
        assert task is not None and task.state == "failed"
        assert task.error is not None and task.error.startswith("graph cost budget")
        assert budget.used_cost_usd == pytest.approx(0.000181)
    finally:
        state.close()
        await delegate.aclose()
        await spawn.aclose()


@pytest.mark.asyncio
async def test_delegate_graph_consolidates_evidence_and_conflicts(tmp_path: Path):
    config, spawn, delegate = _tools(tmp_path, max_concurrency=1)
    result = await delegate.run(
        goal="inspect repository",
        tasks=[
            {
                "key": "first",
                "role": "reviewer",
                "task": 'report `src/app.py` has 10 lines',
                "isolation": "shared",
            },
            {
                "key": "second",
                "role": "reviewer",
                "task": 'report `src/app.py` has 20 lines',
                "depends_on": ["first"],
                "isolation": "shared",
            },
        ],
    )

    assert result.success is True
    payload = json.loads(result.output)
    consolidation = payload["consolidation"]
    assert consolidation["status"] == "succeeded"
    assert consolidation["summary"] == "2 of 2 delegated tasks succeeded. 1 evidence conflict(es) detected."
    assert consolidation["conflicts"][0]["path"] == "src/app.py"
    assert {item["task_key"] for item in consolidation["conflicts"][0]["evidence"]} == {
        "first",
        "second",
    }
    state = SharedState(config.db_directory / "agents.db")
    try:
        artifacts = state.tasks.list_artifacts(payload["tasks"][0]["task_id"])
        assert len(artifacts) == 1
        assert artifacts[0].kind == "graph-consolidation"
        assert artifacts[0].uri == f"consolidation://{payload['graph_id']}"
        assert artifacts[0].metadata["conflict_count"] == 1
        assert artifacts[0].metadata["conflicts"] == consolidation["conflicts"]
    finally:
        state.close()

    await delegate.aclose()
    await spawn.aclose()


@pytest.mark.asyncio
async def test_delegate_agents_runs_independent_tasks_in_parallel(
    tmp_path: Path,
) -> None:
    _, spawn, delegate = _tools(tmp_path, max_concurrency=2)
    result = await delegate.run(
        goal="parallel inspection",
        tasks=[
            {
                "key": "one",
                "role": "reviewer",
                "task": "inspect one",
                "isolation": "shared",
            },
            {
                "key": "two",
                "role": "reviewer",
                "task": "inspect two",
                "isolation": "shared",
            },
        ],
    )

    assert result.success is True
    assert RecordingProvider.max_active == 2
    await delegate.aclose()
    await spawn.aclose()


@pytest.mark.asyncio
async def test_background_graph_and_runtime_start_resume_persisted_work(
    tmp_path: Path,
) -> None:
    config, spawn, delegate = _tools(tmp_path)
    submitted = await delegate.run(
        goal="background",
        background=True,
        tasks=[
            {
                "key": "queued",
                "role": "reviewer",
                "task": "background task",
                "isolation": "shared",
            }
        ],
    )
    task_id = json.loads(submitted.output)["tasks"][0]["task_id"]
    terminal = await spawn.wait_for_tasks([task_id])
    assert terminal[0].state == "succeeded"
    await delegate.aclose()
    await spawn.aclose()

    db_path = config.db_directory / "agents.db"
    state = SharedState(db_path)
    resumed = state.tasks.create_tasks(
        [
            AgentTaskCreate(
                "resumed task",
                role="reviewer",
                task_id="resume-on-start",
                metadata={
                    "agent_id": "resume-worker",
                    "dispatchable": True,
                    "isolation": "shared",
                    "workspace": str(tmp_path.resolve()),
                },
            )
        ]
    )[0]
    state.close()
    restarted = SpawnAgentTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        RecordingProvider,
        config=config,
    )
    await restarted.start()
    terminal = await restarted.wait_for_tasks([resumed.task_id])

    assert terminal[0].state == "succeeded"
    assert terminal[0].owner_agent_id == "resume-worker-a1"
    await restarted.aclose()


@pytest.mark.asyncio
async def test_delegate_agents_retries_with_new_lease_and_agent_attempt(
    tmp_path: Path,
) -> None:
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        provider_max_attempts=1,
        provider_retry_base_delay=0,
        provider_retry_max_delay=0,
        agent_token_budget=100,
        agent_time_budget_seconds=10,
        memory_backend="off",
    )
    db_path = config.db_directory / "agents.db"
    spawn = SpawnAgentTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        FailFirstTaskProvider,
        config=config,
    )
    delegate = DelegateAgentsTool(
        SafetyGuard(tmp_path), SharedState(db_path), spawn, config
    )

    result = await delegate.run(
        goal="retry",
        tasks=[
            {
                "key": "retry",
                "role": "reviewer",
                "task": "retry task",
                "isolation": "shared",
                "max_attempts": 2,
            }
        ],
    )

    assert result.success is True
    state = SharedState(db_path)
    try:
        task = state.tasks.list_tasks()[0]
        assert task.state == "succeeded"
        assert task.attempt == 2
        assert task.owner_agent_id.endswith("-a2")
        assert "agent.task.retrying" in {
            event.event["type"] for event in state.tasks.list_events()
        }
    finally:
        state.close()
        await delegate.aclose()
        await spawn.aclose()


@pytest.mark.asyncio
async def test_external_graph_cancellation_stops_active_provider_turn(
    tmp_path: Path,
) -> None:
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        agent_token_budget=100,
        agent_time_budget_seconds=10,
        memory_backend="off",
    )
    db_path = config.db_directory / "agents.db"
    spawn = SpawnAgentTool(
        SafetyGuard(tmp_path),
        SharedState(db_path),
        CancellableProvider,
        config=config,
    )
    delegate = DelegateAgentsTool(
        SafetyGuard(tmp_path), SharedState(db_path), spawn, config
    )
    submitted = await delegate.run(
        goal="cancel",
        background=True,
        tasks=[
            {
                "key": "slow",
                "role": "reviewer",
                "task": "wait forever",
                "isolation": "shared",
            }
        ],
    )
    graph_id = json.loads(submitted.output)["graph_id"]
    await asyncio.wait_for(CancellableProvider.started.wait(), timeout=2)

    state = SharedState(db_path)
    try:
        state.tasks.cancel_graph(graph_id, reason="test cancellation")
    finally:
        state.close()
    await asyncio.wait_for(CancellableProvider.cancelled.wait(), timeout=2)

    terminal = await spawn.wait_for_tasks(
        [json.loads(submitted.output)["tasks"][0]["task_id"]]
    )
    assert terminal[0].state == "cancelled"
    await delegate.aclose()
    await spawn.aclose()


@pytest.mark.asyncio
async def test_dependent_worktree_receives_verified_commit_and_result_context(
    tmp_path: Path,
) -> None:
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
    config = AshConfig(
        workspace_root=repository,
        db_directory=tmp_path / "db",
        agent_token_budget=100,
        agent_time_budget_seconds=10,
        memory_backend="off",
        safety_tier="auto_edit",
    )
    db_path = config.db_directory / "agents.db"
    spawn = SpawnAgentTool(
        SafetyGuard(repository),
        SharedState(db_path),
        WorktreeHandoffProvider,
        config=config,
    )
    delegate = DelegateAgentsTool(
        SafetyGuard(repository), SharedState(db_path), spawn, config
    )

    result = await delegate.run(
        goal="handoff",
        tasks=[
            {
                "key": "produce",
                "role": "coder",
                "task": "produce change",
                "isolation": "worktree",
            },
            {
                "key": "consume",
                "role": "coder",
                "task": "consume change",
                "depends_on": ["produce"],
                "isolation": "worktree",
            },
        ],
    )

    assert result.success is True
    assert (repository / "file.txt").read_text(encoding="utf-8") == "base\n"
    state = SharedState(db_path)
    try:
        tasks = {task.metadata["task_key"]: task for task in state.tasks.list_tasks()}
        producer_artifact = state.tasks.list_artifacts(tasks["produce"].task_id)[0]
        consumer_artifact = state.tasks.list_artifacts(tasks["consume"].task_id)[0]
        producer_commit = producer_artifact.metadata["commit"]
        consumer_commit = consumer_artifact.metadata["commit"]
        assert (
            producer_commit in consumer_artifact.metadata["accepted_dependency_commits"]
        )
        assert (
            subprocess.run(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    producer_commit,
                    consumer_commit,
                ],
                cwd=repository,
                check=False,
            ).returncode
            == 0
        )
        content = subprocess.run(
            ["git", "show", f"{consumer_artifact.uri}:file.txt"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert content == "consumer\n"
    finally:
        state.close()
        await delegate.aclose()
        await spawn.aclose()


@pytest.mark.asyncio
async def test_delegate_agents_rejects_invalid_graph_without_partial_tasks(
    tmp_path: Path,
) -> None:
    config, spawn, delegate = _tools(tmp_path)
    result = await delegate.run(
        goal="invalid",
        tasks=[
            {
                "key": "one",
                "role": "reviewer",
                "task": "one",
                "depends_on": ["missing"],
            }
        ],
    )

    assert result.success is False
    assert "unknown dependencies" in (result.error or "")
    state = SharedState(config.db_directory / "agents.db")
    try:
        assert state.tasks.list_tasks() == []
    finally:
        state.close()
        await delegate.aclose()
        await spawn.aclose()


@pytest.mark.asyncio
async def test_waiting_graph_surfaces_dispatcher_failure_instead_of_hanging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, spawn, delegate = _tools(tmp_path)
    state = SharedState(config.db_directory / "agents.db")
    task = state.tasks.create_task(
        "never dispatched",
        task_id="dispatcher-failure",
        metadata={
            "agent_id": "failure-worker",
            "dispatchable": True,
            "isolation": "shared",
            "workspace": str(tmp_path.resolve()),
        },
    )
    state.close()

    def fail_ready_query(*, limit: int = 100):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(spawn._shared_state.tasks, "list_ready_tasks", fail_ready_query)

    with pytest.raises(AgentTaskError, match="dispatcher failed"):
        await asyncio.wait_for(spawn.wait_for_tasks([task.task_id]), timeout=2)
    await delegate.aclose()
    await spawn.aclose()


@pytest.mark.asyncio
async def test_dependency_handoff_redacts_predecessor_summary(tmp_path: Path) -> None:
    config, spawn, delegate = _tools(tmp_path)
    state = SharedState(config.db_directory / "agents.db")
    predecessor = state.tasks.create_task("predecessor", task_id="predecessor")
    lease = state.tasks.claim_task("worker", task_id=predecessor.task_id)
    assert lease is not None
    state.tasks.complete_task(
        predecessor.task_id,
        lease.token,
        {"summary": "secret sk-abcdefghijklmnop"},
    )
    child = state.tasks.create_task(
        "child", task_id="child", dependencies=[predecessor.task_id]
    )
    state.close()

    context, artifacts = spawn._dependency_handoff(child)

    assert "sk-abcdefghijklmnop" not in context
    assert "[REDACTED]" in context
    assert artifacts == []
    await delegate.aclose()
    await spawn.aclose()
