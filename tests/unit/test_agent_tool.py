import asyncio
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ash.agents.shared_state import SharedState
from ash.config import AshConfig
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.grants import PermissionRule, build_exact_scope_matchers
from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy
from ash.tools.agent import SpawnAgentTool
from ash.tools.base import ToolResult


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
async def test_worker_fences_recovery_before_first_provider_request(tmp_path) -> None:
    state = SharedState(tmp_path / "agents.db")

    class FenceObservingProvider(FakeProvider):
        @staticmethod
        def _assert_fenced() -> None:
            tasks = state.tasks.list_tasks()
            assert len(tasks) == 1
            events = state.tasks.list_events(task_id=tasks[0].task_id)
            assert any(
                event.event["type"] == "agent.task.recovery_fenced"
                for event in events
            )

        async def detect_capabilities(self) -> ProviderCapabilities:
            self._assert_fenced()
            return ProviderCapabilities(native_tools=True)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self._assert_fenced()
            yield StreamChunk(content="fence observed", is_done=True)

    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        FenceObservingProvider,
        config=AshConfig(workspace_root=tmp_path, memory_backend="off"),
    )
    try:
        result = await tool.run(
            role="reviewer",
            task="inspect fence ordering",
            agent_id="fence-observer",
            isolation="shared",
        )

        assert result.success is True
        assert result.output == "fence observed"
    finally:
        await tool.aclose()


@pytest.mark.asyncio
async def test_foreground_read_only_agent_can_run_provider_in_subprocess(
    tmp_path, monkeypatch
) -> None:
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            requests.append((self.path, self.headers.get("Authorization", "")))
            body = (
                'data: {"id":"child","choices":[{"delta":{"content":"child evidence"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"child","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
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
        raise AssertionError("config-backed subprocess must not call parent factory")

    monkeypatch.setenv("OPENAI_API_KEY", "child-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-cross")
    state = SharedState(tmp_path / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/child-model",
        agent_execution_mode="subprocess",
        memory_backend="off",
    )
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        parent_factory,
        config=config,
        provider_config_backed=True,
    )
    try:
        result = await tool.run(
            role="reviewer",
            task="inspect tests",
            agent_id="process-reviewer",
        )

        assert result.success is True, result.error
        assert result.output == "child evidence"
        assert parent_factory_calls == 0
        assert requests == [
            ("/v1/chat/completions", "Bearer child-secret")
        ]
        durable = state.tasks.list_tasks()
        assert len(durable) == 1
        assert durable[0].state == "succeeded"
        assert durable[0].owner_agent_id == "process-reviewer"
        assert durable[0].result is not None
        assert durable[0].result["summary"] == "child evidence"
    finally:
        await tool.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_foreground_coder_subprocess_uses_live_approval_and_syncs_rule(
    tmp_path, monkeypatch
) -> None:
    requests = 0

    def tool_response(response_id: str, call_id: str, arguments: dict) -> bytes:
        first = {
            "id": response_id,
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        terminal = {
            "id": response_id,
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }
        return (
            f"data: {json.dumps(first)}\n\ndata: {json.dumps(terminal)}\n\n"
        ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            nonlocal requests
            requests += 1
            if requests == 1:
                payload = tool_response(
                    "approval-child-1",
                    "write-first",
                    {
                        "file_path": "first.txt",
                        "content": "first\n",
                        "overwrite": True,
                    },
                )
            elif requests == 2:
                payload = tool_response(
                    "approval-child-2",
                    "write-second",
                    {
                        "file_path": "second.txt",
                        "content": "second\n",
                        "overwrite": True,
                    },
                )
            else:
                payload = (
                    'data: {"id":"approval-child-3","choices":[{"delta":'
                    '{"content":"writes completed"},"finish_reason":null}]}\n\n'
                    'data: {"id":"approval-child-3","choices":[{"delta":{},'
                    '"finish_reason":"stop"}]}\n\n'
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def forbidden_parent_factory() -> ProviderABC:
        raise AssertionError("foreground coder subprocess must rebuild provider in child")

    monkeypatch.setenv("OPENAI_API_KEY", "approval-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    state = SharedState(tmp_path / "state" / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/approval-child",
        agent_execution_mode="subprocess",
        safety_tier="interactive",
        memory_backend="off",
    )
    parent_policy = PermissionPolicy("interactive")
    broker_calls: list[tuple[str, str, dict]] = []

    async def broker(agent_id: str, tool_name: str, arguments: dict) -> bool:
        broker_calls.append((agent_id, tool_name, dict(arguments)))
        parent_policy.add_session_rule(PermissionRule.create("allow", "write_file"))
        return True

    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        forbidden_parent_factory,
        config=config,
        provider_config_backed=True,
    )
    tool.set_permission_policy_provider(lambda: parent_policy)
    tool.set_foreground_approval_broker(broker)
    try:
        result = await tool.run(
            role="coder",
            task="write two files",
            agent_id="approval-coder",
            isolation="shared",
        )

        assert result.success is True, result.error
        assert result.output == "writes completed"
        assert requests == 3
        assert len(broker_calls) == 1
        assert broker_calls[0][0:2] == ("approval-coder", "write_file")
        assert broker_calls[0][2]["file_path"] == "first.txt"
        assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first\n"
        assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second\n"
        assert state.fetch_messages(
            "lead",
            undelivered_only=False,
            message_type="approval_request",
        ) == []
        durable = state.tasks.list_tasks()
        assert len(durable) == 1
        assert durable[0].state == "succeeded"
        assert durable[0].owner_agent_id == "approval-coder"
    finally:
        await tool.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_foreground_coder_subprocess_exact_scope_does_not_cross_call(
    tmp_path, monkeypatch
) -> None:
    requests = 0

    def tool_response(response_id: str, call_id: str, arguments: dict) -> bytes:
        first = {
            "id": response_id,
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        terminal = {
            "id": response_id,
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }
        return (
            f"data: {json.dumps(first)}\n\ndata: {json.dumps(terminal)}\n\n"
        ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            nonlocal requests
            requests += 1
            if requests == 1:
                payload = tool_response(
                    "scope-child-1",
                    "write-scope-first",
                    {
                        "file_path": "scope-first.txt",
                        "content": "first\n",
                        "overwrite": True,
                    },
                )
            elif requests == 2:
                payload = tool_response(
                    "scope-child-2",
                    "write-scope-second",
                    {
                        "file_path": "scope-second.txt",
                        "content": "second\n",
                        "overwrite": True,
                    },
                )
            else:
                payload = (
                    'data: {"id":"scope-child-3","choices":[{"delta":'
                    '{"content":"scoped writes completed"},"finish_reason":null}]}\n\n'
                    'data: {"id":"scope-child-3","choices":[{"delta":{},'
                    '"finish_reason":"stop"}]}\n\n'
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def forbidden_parent_factory() -> ProviderABC:
        raise AssertionError("foreground coder subprocess must rebuild provider in child")

    monkeypatch.setenv("OPENAI_API_KEY", "scope-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    state = SharedState(tmp_path / "scope-state" / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "scope-db",
        model="openai/scope-child",
        agent_execution_mode="subprocess",
        safety_tier="interactive",
        memory_backend="off",
    )
    parent_policy = PermissionPolicy("interactive")
    broker_calls: list[dict] = []

    async def broker(agent_id: str, tool_name: str, arguments: dict) -> bool:
        del agent_id
        broker_calls.append(dict(arguments))
        parent_policy.add_session_rule(
            PermissionRule.create(
                "allow",
                tool_name,
                build_exact_scope_matchers(arguments),
            )
        )
        return True

    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        forbidden_parent_factory,
        config=config,
        provider_config_backed=True,
    )
    tool.set_permission_policy_provider(lambda: parent_policy)
    tool.set_foreground_approval_broker(broker)
    try:
        result = await tool.run(
            role="coder",
            task="write two differently scoped files",
            agent_id="scope-coder",
            isolation="shared",
        )

        assert result.success is True, result.error
        assert result.output == "scoped writes completed"
        assert requests == 3
        assert [call["file_path"] for call in broker_calls] == [
            "scope-first.txt",
            "scope-second.txt",
        ]
        assert (
            tmp_path / "scope-first.txt"
        ).read_text(encoding="utf-8") == "first\n"
        assert (
            tmp_path / "scope-second.txt"
        ).read_text(encoding="utf-8") == "second\n"
    finally:
        await tool.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_live_approval_capability_is_not_in_subprocess_environment(
    tmp_path, monkeypatch
) -> None:
    import ash.tools.agent as agent_module

    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 1

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["argv"] = args
        captured["env"] = dict(kwargs["env"])
        return FakeProcess()

    async def fake_communicate_process(process, *, input_data, **_kwargs):
        assert process is not None
        assert input_data is not None
        captured["spec"] = json.loads(input_data)
        return (b"", b"child failed before lease with sk-test-redacted")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(agent_module, "communicate_process", fake_communicate_process)
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    state = SharedState(tmp_path / "cap-state" / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "cap-db",
        model="openai/capability-child",
        agent_execution_mode="subprocess",
        safety_tier="interactive",
        memory_backend="off",
    )
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        lambda: FakeProvider(),
        config=config,
        provider_config_backed=True,
    )
    tool.set_foreground_approval_broker(
        lambda *_args: asyncio.sleep(0, result=True)
    )
    try:
        result = await tool.run(
            role="coder",
            task="inspect capability transport",
            agent_id="cap-coder",
            isolation="shared",
        )

        assert result.success is False
        assert "exited with status 1" in (result.error or "")
        assert "child failed before lease" in (result.error or "")
        assert "sk-test-redacted" not in (result.error or "")
        spec = captured["spec"]
        assert isinstance(spec, dict)
        channel = spec["approval_channel"]
        assert isinstance(channel, dict)
        token = channel["token"]
        assert isinstance(token, str)
        environment = captured["env"]
        assert isinstance(environment, dict)
        assert token not in environment
        assert token not in environment.values()
        assert all("APPROVAL" not in key for key in environment)
        provider_env = spec["provider_env"]
        assert isinstance(provider_env, dict)
        assert provider_env["OPENAI_API_KEY"] == "provider-secret"
        assert token not in provider_env
        assert token not in provider_env.values()
        assert all("APPROVAL" not in key for key in provider_env)
        durable_task = state.tasks.list_tasks()[0]
        assert durable_task.state == "cancelled"
    finally:
        await tool.aclose()


@pytest.mark.asyncio
async def test_live_approval_server_closes_when_subprocess_launch_fails(
    tmp_path, monkeypatch
) -> None:
    import ash.tools.agent as agent_module

    endpoints = []
    original_server = agent_module.ForegroundApprovalServer

    class TrackingApprovalServer(original_server):
        async def start(self):
            endpoint = await super().start()
            endpoints.append(endpoint)
            return endpoint

    async def fail_create_subprocess_exec(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(
        agent_module,
        "ForegroundApprovalServer",
        TrackingApprovalServer,
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fail_create_subprocess_exec,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "launch-failure-secret")
    state = SharedState(tmp_path / "launch-state" / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "launch-db",
        model="openai/launch-failure-child",
        agent_execution_mode="subprocess",
        safety_tier="interactive",
        memory_backend="off",
    )
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        lambda: FakeProvider(),
        config=config,
        provider_config_backed=True,
    )
    tool.set_foreground_approval_broker(
        lambda *_args: asyncio.sleep(0, result=True)
    )
    try:
        result = await tool.run(
            role="coder",
            task="exercise launch cleanup",
            agent_id="launch-failure-coder",
            isolation="shared",
        )

        assert result.success is False
        assert "spawn failed" in (result.error or "")
        assert len(endpoints) == 1
        endpoint = endpoints[0]
        with pytest.raises(OSError):
            await asyncio.open_connection(endpoint.host, endpoint.port)
    finally:
        await tool.aclose()


@pytest.mark.asyncio
async def test_foreground_subprocess_cancel_during_live_approval_fails_closed(
    tmp_path, monkeypatch
) -> None:
    def tool_response(arguments: dict) -> bytes:
        first = {
            "id": "cancel-approval-child",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "cancel-write",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        terminal = {
            "id": "cancel-approval-child",
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }
        return (
            f"data: {json.dumps(first)}\n\ndata: {json.dumps(terminal)}\n\n"
        ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            payload = tool_response(
                {
                    "file_path": "must-not-write.txt",
                    "content": "blocked\n",
                    "overwrite": True,
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def forbidden_parent_factory() -> ProviderABC:
        raise AssertionError("foreground subprocess must rebuild provider in child")

    monkeypatch.setenv("OPENAI_API_KEY", "cancel-approval-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    state = SharedState(tmp_path / "cancel-state" / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "cancel-db",
        model="openai/cancel-approval-child",
        agent_execution_mode="subprocess",
        safety_tier="interactive",
        memory_backend="off",
    )
    broker_started = asyncio.Event()
    broker_cancelled = asyncio.Event()

    async def broker(agent_id: str, tool_name: str, arguments: dict) -> bool:
        del agent_id, tool_name, arguments
        broker_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            broker_cancelled.set()
            raise

    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        forbidden_parent_factory,
        config=config,
        provider_config_backed=True,
    )
    tool.set_foreground_approval_broker(broker)
    run_task = asyncio.create_task(
        tool.run(
            role="coder",
            task="attempt one blocked write",
            agent_id="cancel-approval-coder",
            isolation="shared",
        )
    )
    try:
        await asyncio.wait_for(broker_started.wait(), timeout=5)
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=5)
        await asyncio.wait_for(broker_cancelled.wait(), timeout=2)

        durable = state.tasks.list_tasks()
        assert len(durable) == 1
        current = state.tasks.get_task(durable[0].task_id)
        assert current is not None
        assert current.state == "cancelled"
        assert not (tmp_path / "must-not-write.txt").exists()
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
        await tool.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_background_agent_runs_provider_in_subprocess(tmp_path, monkeypatch) -> None:
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            requests.append((self.path, self.headers.get("Authorization", "")))
            body = (
                'data: {"id":"background-child","choices":[{"delta":{"content":"background evidence"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"background-child","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
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
        raise AssertionError("background subprocess must not call parent factory")

    monkeypatch.setenv("OPENAI_API_KEY", "background-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    state = SharedState(tmp_path / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/background-model",
        agent_execution_mode="subprocess",
        memory_backend="off",
    )
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        parent_factory,
        config=config,
        provider_config_backed=True,
    )
    try:
        started = await tool.run(
            role="reviewer",
            task="inspect tests",
            agent_id="background-process-reviewer",
            background=True,
        )
        assert started.success is True
        assert "Started subagent background-process-reviewer" in started.output

        durable = state.tasks.list_tasks()[0]
        terminal = await asyncio.wait_for(
            tool.wait_for_tasks([durable.task_id]),
            timeout=10,
        )
        assert terminal[0].state == "succeeded"
        assert terminal[0].owner_agent_id == "background-process-reviewer"
        assert terminal[0].result is not None
        assert terminal[0].result["summary"] == "background evidence"
        assert parent_factory_calls == 0
        assert requests == [
            ("/v1/chat/completions", "Bearer background-secret")
        ]
        monitor = tool._subprocess_tasks.get("background-process-reviewer")
        if monitor is not None:
            await asyncio.wait_for(asyncio.shield(monitor), timeout=10)
            await asyncio.sleep(0)
        assert "background-process-reviewer" not in tool._subprocess_tasks
    finally:
        await tool.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_background_coder_subprocess_keeps_durable_approval_path(
    tmp_path, monkeypatch
) -> None:
    requests = 0

    def tool_response(response_id: str, call_id: str, arguments: dict) -> bytes:
        first = {
            "id": response_id,
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        terminal = {
            "id": response_id,
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }
        return (
            f"data: {json.dumps(first)}\n\ndata: {json.dumps(terminal)}\n\n"
        ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            nonlocal requests
            requests += 1
            if requests == 1:
                payload = tool_response(
                    "durable-child-1",
                    "write-durable",
                    {
                        "file_path": "durable-child.txt",
                        "content": "durable\n",
                        "overwrite": True,
                    },
                )
            else:
                payload = (
                    'data: {"id":"durable-child-2","choices":[{"delta":'
                    '{"content":"durable write completed"},"finish_reason":null}]}\n\n'
                    'data: {"id":"durable-child-2","choices":[{"delta":{},'
                    '"finish_reason":"stop"}]}\n\n'
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def forbidden_parent_factory() -> ProviderABC:
        raise AssertionError("background coder subprocess must rebuild provider in child")

    monkeypatch.setenv("OPENAI_API_KEY", "durable-approval-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    state = SharedState(tmp_path / "state" / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/durable-child",
        agent_execution_mode="subprocess",
        safety_tier="interactive",
        memory_backend="off",
    )
    live_broker_calls = 0

    async def forbidden_live_broker(
        agent_id: str, tool_name: str, arguments: dict
    ) -> bool:
        nonlocal live_broker_calls
        del agent_id, tool_name, arguments
        live_broker_calls += 1
        return True

    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        forbidden_parent_factory,
        config=config,
        provider_config_backed=True,
    )
    tool.set_foreground_approval_broker(forbidden_live_broker)
    try:
        started = await tool.run(
            role="coder",
            task="write with durable approval",
            agent_id="durable-coder",
            isolation="shared",
            background=True,
        )
        assert started.success is True

        request = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            approvals = state.fetch_messages(
                "lead",
                undelivered_only=True,
                limit=100,
                message_type="approval_request",
            )
            request = next(
                (
                    message
                    for message in approvals
                    if message.sender_id == "durable-coder"
                ),
                None,
            )
            if request is not None:
                break
            durable = state.tasks.list_tasks()[0]
            current = state.tasks.get_task(durable.task_id)
            if current is not None and current.state in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                break
            await asyncio.sleep(0.02)
        durable = state.tasks.list_tasks()[0]
        current = state.tasks.get_task(durable.task_id)
        assert request is not None, (
            "background durable approval request did not arrive; "
            f"task_state={current.state if current is not None else 'missing'} "
            f"task_error={current.error if current is not None else None!r}"
        )
        assert live_broker_calls == 0

        resolved = state.resolve_approval_request(
            request.message_id,
            approved=True,
        )
        assert resolved["approved"] is True

        terminal = await asyncio.wait_for(
            tool.wait_for_tasks([durable.task_id]),
            timeout=10,
        )
        assert terminal[0].state == "succeeded"
        assert requests == 2
        assert live_broker_calls == 0
        assert (
            tmp_path / "durable-child.txt"
        ).read_text(encoding="utf-8") == "durable\n"
    finally:
        await tool.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_subprocess_dispatcher_reserves_task_before_child_claim(
    tmp_path, monkeypatch
) -> None:
    state = SharedState(tmp_path / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        agent_execution_mode="subprocess",
        memory_backend="off",
    )
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        FakeProvider,
        config=config,
        provider_config_backed=True,
    )
    durable = state.tasks.create_task(
        "inspect tests",
        role="reviewer",
        task_id="dispatch-reservation",
        metadata={
            "agent_id": "reserved-reviewer",
            "dispatchable": True,
            "isolation": "shared",
            "workspace": str(tmp_path.resolve()),
        },
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_execute(**kwargs) -> ToolResult:
        nonlocal calls
        calls += 1
        assert kwargs["durable_task"].task_id == durable.task_id
        started.set()
        await release.wait()
        state.tasks.cancel_task(durable.task_id, reason="reservation probe complete")
        return ToolResult(success=False, output="", error="reservation probe complete")

    monkeypatch.setattr(tool, "_execute_subprocess_task", delayed_execute)
    try:
        tool.ensure_dispatcher()
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0.35)

        assert state.tasks.get_task(durable.task_id).state == "queued"
        assert calls == 1

        release.set()
        await asyncio.wait_for(tool.wait_for_tasks([durable.task_id]), timeout=2)
        assert state.tasks.get_task(durable.task_id).state == "cancelled"
    finally:
        release.set()
        await tool.aclose()


@pytest.mark.asyncio
async def test_dispatcher_does_not_replay_expired_recovery_fenced_task(
    tmp_path, monkeypatch
) -> None:
    clock = [time.time()]
    monkeypatch.setattr("ash.agents.tasks.time.time", lambda: clock[0])
    db_path = tmp_path / "agents.db"
    state = SharedState(db_path)
    durable = state.tasks.create_task(
        "do not replay",
        role="reviewer",
        task_id="ambiguous-restart",
        max_attempts=3,
        metadata={
            "agent_id": "ambiguous-worker",
            "dispatchable": True,
            "isolation": "shared",
            "workspace": str(tmp_path.resolve()),
        },
    )
    lease = state.tasks.claim_task(
        "ambiguous-worker-a1",
        task_id=durable.task_id,
        lease_seconds=1,
    )
    assert lease is not None
    state.tasks.start_task(durable.task_id, lease.token)
    state.tasks.mark_recovery_unsafe(durable.task_id, lease.token)
    state.close()
    clock[0] += 2

    provider_calls = 0

    def provider_factory() -> ProviderABC:
        nonlocal provider_calls
        provider_calls += 1
        return FakeProvider()

    restarted_state = SharedState(db_path)
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        restarted_state,
        provider_factory,
        config=AshConfig(workspace_root=tmp_path, memory_backend="off"),
    )
    try:
        terminal = await asyncio.wait_for(
            tool.wait_for_tasks([durable.task_id]),
            timeout=2,
        )

        assert terminal[0].state == "failed"
        assert (
            terminal[0].error
            == "worker lease expired after execution began; automatic retry suppressed"
        )
        assert provider_calls == 0
    finally:
        await tool.aclose()


@pytest.mark.asyncio
async def test_background_subprocess_stop_cancels_durable_task(
    tmp_path, monkeypatch
) -> None:
    request_started = threading.Event()
    release_response = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            request_started.set()
            release_response.wait(timeout=5)
            body = (
                'data: {"id":"slow-child","choices":[{"delta":{"content":"late"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"slow-child","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def forbidden_parent_factory() -> ProviderABC:
        raise AssertionError("background subprocess must not call parent factory")

    monkeypatch.setenv("OPENAI_API_KEY", "slow-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    state = SharedState(tmp_path / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        model="openai/slow-child",
        agent_execution_mode="subprocess",
        memory_backend="off",
    )
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        forbidden_parent_factory,
        config=config,
        provider_config_backed=True,
    )
    try:
        started = await tool.run(
            role="reviewer",
            task="inspect tests",
            agent_id="slow-process-reviewer",
            background=True,
        )
        assert started.success is True
        assert await asyncio.to_thread(request_started.wait, 5)

        durable = state.tasks.list_tasks()[0]
        active = state.tasks.get_task(durable.task_id)
        assert active is not None and active.state == "running"

        assert await tool.stop("slow-process-reviewer") is True

        stopped = state.tasks.get_task(durable.task_id)
        assert stopped is not None and stopped.state == "cancelled"
        assert stopped.error == "stopped by persisted message"
        stop_messages = [
            message
            for message in state.fetch_messages(
                "slow-process-reviewer",
                undelivered_only=False,
            )
            if message.message_type == "stop"
        ]
        assert len(stop_messages) == 1
        assert stop_messages[0].delivered is True
        assert "slow-process-reviewer" not in tool._subprocess_tasks
    finally:
        release_response.set()
        await tool.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_background_subprocess_stop_cleans_coder_worktree(
    tmp_path, monkeypatch
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

    request_started = threading.Event()
    release_response = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            request_started.set()
            release_response.wait(timeout=5)
            body = (
                'data: {"id":"coder-child","choices":[{"delta":{"content":"late"},'
                '"finish_reason":null}]}\n\n'
                'data: {"id":"coder-child","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def forbidden_parent_factory() -> ProviderABC:
        raise AssertionError("coder subprocess must not call parent factory")

    monkeypatch.setenv("OPENAI_API_KEY", "coder-secret")
    monkeypatch.setenv(
        "OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1"
    )
    state = SharedState(tmp_path / "state" / "agents.db")
    config = AshConfig(
        workspace_root=repository,
        db_directory=tmp_path / "state",
        model="openai/coder-child",
        agent_execution_mode="subprocess",
        memory_backend="off",
    )
    tool = SpawnAgentTool(
        SafetyGuard(repository),
        state,
        forbidden_parent_factory,
        config=config,
        provider_config_backed=True,
    )
    try:
        started = await tool.run(
            role="coder",
            task="inspect before editing",
            agent_id="coder-process",
            background=True,
        )
        assert started.success is True
        assert await asyncio.to_thread(request_started.wait, 15)

        before = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert before.count("worktree ") == 2

        assert await tool.stop("coder-process") is True

        after = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert after.count("worktree ") == 1
        branches = subprocess.run(
            ["git", "branch", "--list", "ash-agent/coder-process"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert branches.strip() == ""
    finally:
        release_response.set()
        await tool.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_subprocess_mode_preserves_opaque_python_provider_factory(tmp_path) -> None:
    calls = 0

    def provider_factory() -> ProviderABC:
        nonlocal calls
        calls += 1
        return FakeProvider()

    state = SharedState(tmp_path / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        agent_execution_mode="subprocess",
        memory_backend="off",
    )
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        provider_factory,
        config=config,
    )
    try:
        result = await tool.run(
            role="reviewer",
            task="inspect tests",
            agent_id="sdk-reviewer",
        )

        assert result.success is True
        assert result.output == "evidence: tests pass"
        assert calls == 1
        assert state.tasks.list_tasks()[0].owner_agent_id == "sdk-reviewer"
    finally:
        await tool.aclose()


@pytest.mark.asyncio
async def test_interactive_coder_subprocess_without_live_broker_falls_back_in_process(
    tmp_path,
) -> None:
    calls = 0

    class LocalProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            assert tools is not None
            yield StreamChunk(content="local fallback", is_done=True)

    def provider_factory() -> ProviderABC:
        nonlocal calls
        calls += 1
        return LocalProvider()

    state = SharedState(tmp_path / "agents.db")
    config = AshConfig(
        workspace_root=tmp_path,
        agent_execution_mode="subprocess",
        safety_tier="interactive",
        memory_backend="off",
    )
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        provider_factory,
        config=config,
        provider_config_backed=True,
    )
    try:
        result = await tool.run(
            role="coder",
            task="inspect tests",
            agent_id="interactive-local-coder",
            isolation="shared",
        )

        assert result.success is True
        assert result.output == "local fallback"
        assert calls == 1
    finally:
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

    database = tmp_path / "background-policy" / "agents.db"
    state = SharedState(database)
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

    request = None
    for _ in range(100):
        requests = state.fetch_messages(
            "lead",
            undelivered_only=True,
            limit=100,
            message_type="approval_request",
        )
        request = next(
            (message for message in requests if message.sender_id == "background-coder"),
            None,
        )
        if request is not None:
            break
        await asyncio.sleep(0.01)
    assert request is not None
    assert broker_calls == []

    resolver = SharedState(database)
    try:
        resolver.resolve_approval_request(
            request.message_id,
            approved=False,
            feedback="keep background writes denied",
        )
    finally:
        resolver.close()

    report = await asyncio.wait_for(tool._tasks["background-coder"], timeout=10.0)
    assert report.success is True
    assert report.summary == "background denied"
    assert broker_calls == []
    assert not (tmp_path / "background.txt").exists()
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_subagent_waits_for_durable_approval_and_resumes(tmp_path) -> None:
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
                            "id": "durable-write",
                            "name": "write_file",
                            "arguments": {
                                "file_path": "durable-approved.txt",
                                "content": "approved later\n",
                                "overwrite": True,
                            },
                        }
                    ],
                    is_done=True,
                )
            else:
                yield StreamChunk(content="background approved", is_done=True)

    database = tmp_path / "durable-approval" / "agents.db"
    state = SharedState(database)
    config = AshConfig(workspace_root=tmp_path, safety_tier="interactive")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, WritingProvider, config=config)

    started = await tool.run(
        role="coder",
        task="wait for approval then write",
        agent_id="approval-worker",
        isolation="shared",
        background=True,
    )
    assert started.success is True

    request = None
    for _ in range(100):
        request = next(
            (
                message
                for message in state.fetch_messages(
                    "lead", undelivered_only=True, limit=100
                )
                if message.message_type == "approval_request"
                and message.sender_id == "approval-worker"
            ),
            None,
        )
        if request is not None:
            break
        await asyncio.sleep(0.01)

    assert request is not None
    assert not (tmp_path / "durable-approved.txt").exists()
    payload = request.content
    resolver = SharedState(database)
    try:
        resolution = resolver.resolve_approval_request(
            request.message_id,
            approved=True,
        )
        assert resolution["task_id"] == payload["task_id"]
        assert resolution["attempt"] == payload["attempt"]
        assert resolution["arguments_sha256"] == payload["arguments_sha256"]
    finally:
        resolver.close()

    report = await asyncio.wait_for(tool._tasks["approval-worker"], timeout=10.0)
    assert report.success is True
    assert report.summary == "background approved"
    assert (tmp_path / "durable-approved.txt").read_text(encoding="utf-8") == (
        "approved later\n"
    )
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_approval_persists_redacted_bounded_argument_preview(
    tmp_path,
) -> None:
    fake_secret = "supersecretvalue12345"
    large_content = f"password={fake_secret}\n" + ("x" * 10_000)

    class WritingProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            assert tools is not None
            yield StreamChunk(
                native_tool_calls=[
                    {
                        "id": "preview-write",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "preview.txt",
                            "content": large_content,
                            "overwrite": True,
                        },
                    }
                ],
                is_done=True,
            )

    database = tmp_path / "preview-approval" / "agents.db"
    state = SharedState(database)
    config = AshConfig(workspace_root=tmp_path, safety_tier="interactive")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, WritingProvider, config=config)
    started = await tool.run(
        role="coder",
        task="request large secret-looking write",
        agent_id="preview-worker",
        isolation="shared",
        background=True,
    )
    assert started.success is True

    request = None
    for _ in range(100):
        requests = state.fetch_messages(
            "lead",
            undelivered_only=True,
            limit=100,
            message_type="approval_request",
        )
        request = next(
            (message for message in requests if message.sender_id == "preview-worker"),
            None,
        )
        if request is not None:
            break
        await asyncio.sleep(0.01)
    assert request is not None
    preview = request.content["arguments_preview"]
    assert fake_secret not in preview
    assert "[REDACTED]" in preview
    assert len(preview) <= 8192
    assert preview.endswith("... [approval arguments truncated]")
    assert len(request.content["arguments_sha256"]) == 64

    assert await tool.stop("preview-worker") is True
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_approval_response_mismatch_fails_closed(tmp_path) -> None:
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
                            "id": "mismatch-write",
                            "name": "write_file",
                            "arguments": {
                                "file_path": "mismatch.txt",
                                "content": "must not write\n",
                                "overwrite": True,
                            },
                        }
                    ],
                    is_done=True,
                )
            else:
                assert any(
                    "Approval response did not match the active request"
                    in str(message.get("content"))
                    for message in messages
                    if message.get("role") == "tool"
                )
                yield StreamChunk(content="mismatch denied", is_done=True)

    database = tmp_path / "mismatch-approval" / "agents.db"
    state = SharedState(database)
    config = AshConfig(workspace_root=tmp_path, safety_tier="interactive")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, WritingProvider, config=config)
    started = await tool.run(
        role="coder",
        task="reject mismatched approval",
        agent_id="mismatch-worker",
        isolation="shared",
        background=True,
    )
    assert started.success is True

    request = None
    for _ in range(100):
        requests = state.fetch_messages(
            "lead",
            undelivered_only=True,
            limit=100,
            message_type="approval_request",
        )
        request = next(
            (message for message in requests if message.sender_id == "mismatch-worker"),
            None,
        )
        if request is not None:
            break
        await asyncio.sleep(0.01)
    assert request is not None
    payload = request.content
    state.send_message(
        "lead",
        "mismatch-worker",
        "approval_response",
        {
            "request_message_id": request.message_id,
            "task_id": payload["task_id"],
            "attempt": payload["attempt"],
            "agent_id": payload["agent_id"],
            "tool_name": payload["tool_name"],
            "arguments_sha256": "0" * 64,
            "approved": True,
            "feedback": "",
        },
    )

    report = await asyncio.wait_for(tool._tasks["mismatch-worker"], timeout=10.0)
    assert report.success is True
    assert report.summary == "mismatch denied"
    assert not (tmp_path / "mismatch.txt").exists()
    await tool.aclose()


@pytest.mark.asyncio
async def test_stopping_background_agent_retires_pending_approval(tmp_path) -> None:
    class WritingProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            assert tools is not None
            yield StreamChunk(
                native_tool_calls=[
                    {
                        "id": "stop-waiting-write",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "never-written.txt",
                            "content": "no\n",
                            "overwrite": True,
                        },
                    }
                ],
                is_done=True,
            )

    database = tmp_path / "stop-approval" / "agents.db"
    state = SharedState(database)
    config = AshConfig(workspace_root=tmp_path, safety_tier="interactive")
    tool = SpawnAgentTool(SafetyGuard(tmp_path), state, WritingProvider, config=config)
    started = await tool.run(
        role="coder",
        task="wait until stopped",
        agent_id="stopped-approval-worker",
        isolation="shared",
        background=True,
    )
    assert started.success is True

    request = None
    for _ in range(100):
        requests = state.fetch_messages(
            "lead",
            undelivered_only=True,
            limit=100,
            message_type="approval_request",
        )
        request = next(
            (message for message in requests if message.sender_id == "stopped-approval-worker"),
            None,
        )
        if request is not None:
            break
        await asyncio.sleep(0.01)
    assert request is not None

    assert await tool.stop("stopped-approval-worker") is True
    persisted = state.fetch_messages(
        "lead",
        undelivered_only=False,
        limit=100,
        message_type="approval_request",
    )
    stopped_request = next(
        message for message in persisted if message.message_id == request.message_id
    )
    assert stopped_request.delivered is True
    assert not (tmp_path / "never-written.txt").exists()
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
