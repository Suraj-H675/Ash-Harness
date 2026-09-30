import asyncio
from datetime import datetime, timezone
import math

import pytest

from ash.sdk import AshEvent, AshEventRecord, AshResult
from ash.core.session import SessionLineage
from ash.server.jsonrpc import JSONRPCServer


class FakeLoop:
    current_session = None
    project_root = "/tmp/project"
    _last_context_tokens = 0
    last_turn_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_hit_rate": 0.0,
        "cost_usd": 0.0,
    }

    class Policy:
        class Mode:
            value = "interactive"

        mode = Mode()

    permission_policy = Policy()


class FakeConfig:
    model = "fake/model"


class FakeClient:
    def __init__(self) -> None:
        self.loop = FakeLoop()
        self.config = FakeConfig()
        self._started = True

    async def prompt(self, text: str) -> AshResult:
        return AshResult(text.upper(), "session-1", "fake/model", 3)

    def sessions(self, query="", limit=20):
        return []

    def events(
        self,
        session_id=None,
        *,
        after_sequence=0,
        turn_id=None,
        limit=1000,
    ):
        return [
            AshEventRecord(
                after_sequence + 1,
                AshEvent("turn.started", {"session_id": session_id}),
            )
        ]

    async def close(self):
        return None

    async def fork(
        self,
        session_id=None,
        *,
        message_count=None,
        branch_name="",
        branch_summary="",
    ):
        return "session-fork"

    def session_tree(self, session_id=None):
        return [
            SessionLineage(
                session_id=session_id or "session-1",
                root_session_id=session_id or "session-1",
                created_at=datetime.now(timezone.utc),
            )
        ]


@pytest.mark.asyncio
async def test_jsonrpc_initialize_advertises_versioned_contracts() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]

    response = await server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )

    assert response["result"]["protocol_version"] == 1
    assert response["result"]["capabilities"]["event_schema_version"] == 1
    assert response["result"]["capabilities"]["event_replay"] is True
    assert response["result"]["capabilities"]["session_tree"] is True


@pytest.mark.asyncio
async def test_jsonrpc_bounds_in_flight_requests_without_blocking_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.server.jsonrpc as jsonrpc_module

    monkeypatch.setattr(jsonrpc_module, "MAX_PENDING_JSONRPC_REQUESTS", 2)
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]
    started = 0
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(_params):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await release.wait()
        return {"done": True}

    server._methods["test/block"] = blocked
    first = asyncio.create_task(
        server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "test/block"}
        )
    )
    second = asyncio.create_task(
        server.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "test/block"}
        )
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)

    busy = await server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "test/block"}
    )
    assert busy == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32001, "message": "Server is busy"},
    }
    assert len(server._request_tasks) == 2

    cancelled = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "$/cancelRequest",
            "params": {"id": 1},
        }
    )
    assert cancelled == {"jsonrpc": "2.0", "id": 4, "result": True}

    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32800, "message": "Request cancelled"},
    }
    assert second_result == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"done": True},
    }
    assert server._request_tasks == set()


@pytest.mark.asyncio
async def test_jsonrpc_rejects_duplicate_in_flight_request_id_without_losing_cancel_handle() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(_params):
        started.set()
        await release.wait()
        return {"done": True}

    server._methods["test/block"] = blocked
    first = asyncio.create_task(
        server.handle_request(
            {"jsonrpc": "2.0", "id": "same", "method": "test/block"}
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    duplicate = await server.handle_request(
        {"jsonrpc": "2.0", "id": "same", "method": "status"}
    )
    assert duplicate == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {
            "code": -32600,
            "message": "Request id is already in flight",
        },
    }
    assert server._pending.get("same") is not None

    cancelled = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "cancel",
            "method": "$/cancelRequest",
            "params": {"id": "same"},
        }
    )
    assert cancelled == {"jsonrpc": "2.0", "id": "cancel", "result": True}
    result = await first
    assert result == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {"code": -32800, "message": "Request cancelled"},
    }
    assert "same" not in server._pending


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", ["x" * 513, 2**53, -(2**53), float("inf")])
async def test_jsonrpc_rejects_unsafe_request_ids_before_admission(request_id) -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]

    response = await server.handle_request(
        {"jsonrpc": "2.0", "id": request_id, "method": "status"}
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert server._request_tasks == set()
    assert server._pending == {}


@pytest.mark.asyncio
async def test_jsonrpc_event_replay_returns_next_cursor() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]

    response = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "event/list",
            "params": {"session_id": "session-1", "after_sequence": 8},
        }
    )

    assert response["result"]["events"][0]["sequence"] == 9
    assert response["result"]["next_sequence"] == 9


@pytest.mark.asyncio
async def test_jsonrpc_forks_and_lists_session_tree() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]

    forked = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/fork",
            "params": {
                "session_id": "session-1",
                "message_count": 2,
                "branch_name": "alternate",
            },
        }
    )
    tree = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/tree",
            "params": {"session_id": "session-1"},
        }
    )

    assert forked["result"] == {"session_id": "session-fork"}
    assert tree["result"][0]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_jsonrpc_turn_validation_and_unknown_method() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]
    response = await server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "turn/run", "params": {"input": "hi"}}
    )
    assert response["result"]["response"] == "HI"
    assert response["result"]["usage"]["prompt_tokens"] == 0
    invalid = await server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "turn/run", "params": {}}
    )
    assert invalid["error"]["code"] == -32602
    missing = await server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "missing"}
    )
    assert missing["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_jsonrpc_explicit_null_id_is_a_request_not_a_notification() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]

    notification = await server.handle_request(
        {"jsonrpc": "2.0", "method": "status"}
    )
    explicit_null = await server.handle_request(
        {"jsonrpc": "2.0", "id": None, "method": "status"}
    )
    explicit_null_error = await server.handle_request(
        {"jsonrpc": "2.0", "id": None, "method": "missing"}
    )
    numeric_zero = await server.handle_request(
        {"jsonrpc": "2.0", "id": 0, "method": "status"}
    )
    string_id = await server.handle_request(
        {"jsonrpc": "2.0", "id": "status", "method": "status"}
    )

    assert notification is None
    assert explicit_null is not None
    assert explicit_null["id"] is None
    assert "result" in explicit_null
    assert explicit_null_error == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32601, "message": "Method not found: missing"},
    }
    assert numeric_zero is not None and numeric_zero["id"] == 0
    assert string_id is not None and string_id["id"] == "status"
    assert server._pending == {}


@pytest.mark.asyncio
async def test_jsonrpc_close_owns_explicit_null_id_request() -> None:
    client = FakeClient()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(text: str) -> AshResult:
        del text
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client.prompt = slow
    server = JSONRPCServer(client)  # type: ignore[arg-type]
    pending = asyncio.create_task(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": None,
                "method": "turn/run",
                "params": {"input": "wait"},
            }
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert server._pending == {}
    assert len(server._request_tasks) == 1

    await server.close(close_client=False)
    response = await asyncio.wait_for(pending, timeout=1)

    assert cancelled.is_set()
    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32800, "message": "Request cancelled"},
    }
    assert not server._request_tasks


@pytest.mark.asyncio
async def test_jsonrpc_redacts_secrets_from_internal_errors() -> None:
    client = FakeClient()
    secret = "sk-proj-abcdefghijklmnop"

    async def fail(_text: str) -> AshResult:
        raise RuntimeError(f"provider failed api_key={secret}")

    client.prompt = fail
    server = JSONRPCServer(client)  # type: ignore[arg-type]

    response = await server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "turn/run", "params": {"input": "hi"}}
    )

    assert response["error"]["code"] == -32603
    assert secret not in str(response)
    assert "[REDACTED]" in response["error"]["data"]["detail"]


@pytest.mark.asyncio
async def test_jsonrpc_rejects_malformed_collection_parameters() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]

    invalid_requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/list",
            "params": {"query": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/list",
            "params": {"limit": 1.5},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/list",
            "params": {"limit": 101},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "event/list",
            "params": {"after_sequence": float("inf")},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "event/list",
            "params": {"turn_id": []},
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "event/list",
            "params": {"limit": 10_001},
        },
    ]

    responses = [await server.handle_request(request) for request in invalid_requests]

    assert [response["error"]["code"] for response in responses] == [-32602] * 6


@pytest.mark.asyncio
async def test_jsonrpc_rejects_oversized_turn_and_fork_metadata() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]

    oversized_turn = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn/run",
            "params": {"input": "x" * 1_000_001},
        }
    )
    oversized_branch = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/fork",
            "params": {"branch_name": "x" * 129},
        }
    )

    assert oversized_turn["error"]["code"] == -32602
    assert oversized_branch["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_jsonrpc_rejects_unhashable_ids_and_cancel_targets() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]

    invalid_request_id = await server.handle_request(
        {"jsonrpc": "2.0", "id": [], "method": "status"}
    )
    invalid_cancel_target = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "$/cancelRequest",
            "params": {"id": {}},
        }
    )

    assert invalid_request_id == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert invalid_cancel_target == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "cancel request id is invalid"},
    }

    for identifier in (math.inf, -math.inf, math.nan):
        response = await server.handle_request(
            {"jsonrpc": "2.0", "id": identifier, "method": "status"}
        )
        assert response == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }


@pytest.mark.asyncio
async def test_jsonrpc_cancellation() -> None:
    client = FakeClient()

    async def slow(text):
        await asyncio.sleep(10)

    client.prompt = slow
    server = JSONRPCServer(client)  # type: ignore[arg-type]
    pending = asyncio.create_task(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": "slow",
                "method": "turn/run",
                "params": {"input": "wait"},
            }
        )
    )
    await asyncio.sleep(0)
    assert server.cancel("slow") is True
    response = await pending
    assert response["error"]["code"] == -32800


@pytest.mark.asyncio
async def test_jsonrpc_bounds_and_cancels_notification_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    started = asyncio.Event()
    cancelled = 0

    async def slow(_text: str) -> AshResult:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    client.prompt = slow
    monkeypatch.setattr("ash.server.jsonrpc.MAX_PENDING_JSONRPC_NOTIFICATIONS", 4)
    server = JSONRPCServer(client)  # type: ignore[arg-type]

    for index in range(10):
        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "method": "turn/run",
                "params": {"input": f"notification {index}"},
            }
        )
        assert response is None

    await started.wait()
    await asyncio.sleep(0)
    assert len(server._notification_tasks) == 4
    assert server._pending == {}
    notification_tasks = tuple(server._notification_tasks)

    await server.close(close_client=False)

    assert server._notification_tasks == set()
    assert all(task.done() for task in notification_tasks)
    assert cancelled == 1


@pytest.mark.asyncio
async def test_jsonrpc_close_rejects_request_arriving_after_shutdown_snapshot() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    prompt_started = asyncio.Event()
    allow_prompt = asyncio.Event()

    class ClosingClient(FakeClient):
        async def prompt(self, text: str) -> AshResult:
            prompt_started.set()
            await allow_prompt.wait()
            return AshResult(text.upper(), "session-1", "fake/model", 3)

        async def close(self) -> None:
            close_started.set()
            await allow_close.wait()

    server = JSONRPCServer(ClosingClient())  # type: ignore[arg-type]
    closing = asyncio.create_task(server.close(close_client=True))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    late = asyncio.create_task(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": "late",
                "method": "turn/run",
                "params": {"input": "must not start"},
            }
        )
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert prompt_started.is_set() is False
        assert await late == {
            "jsonrpc": "2.0",
            "id": "late",
            "error": {"code": -32000, "message": "Server is closing"},
        }
    finally:
        allow_prompt.set()
        allow_close.set()
        await asyncio.gather(late, closing, return_exceptions=True)


@pytest.mark.asyncio
async def test_jsonrpc_close_retries_failed_client_cleanup_once() -> None:
    class FlakyCloseClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("JSON-RPC client cleanup failed once")

    client = FlakyCloseClient()
    server = JSONRPCServer(client)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="JSON-RPC client cleanup failed once"):
        await server.close(close_client=True)

    assert client.close_calls == 1
    rejected = await server.handle_request(
        {"jsonrpc": "2.0", "id": "late", "method": "status"}
    )
    assert rejected == {
        "jsonrpc": "2.0",
        "id": "late",
        "error": {"code": -32000, "message": "Server is closing"},
    }

    await server.close(close_client=True)
    assert client.close_calls == 2

    await server.close(close_client=True)
    assert client.close_calls == 2


@pytest.mark.asyncio
async def test_jsonrpc_close_settles_client_cleanup_before_propagating_cancellation() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    close_finished = asyncio.Event()

    class BlockingCloseClient(FakeClient):
        async def close(self) -> None:
            close_started.set()
            await allow_close.wait()
            close_finished.set()

    server = JSONRPCServer(BlockingCloseClient())  # type: ignore[arg-type]
    closing = asyncio.create_task(server.close(close_client=True))
    await asyncio.wait_for(close_started.wait(), timeout=1)

    closing.cancel()
    await asyncio.sleep(0)

    assert closing.done() is False
    assert close_finished.is_set() is False

    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=1)

    assert close_finished.is_set() is True
    assert server._client_closed is True
