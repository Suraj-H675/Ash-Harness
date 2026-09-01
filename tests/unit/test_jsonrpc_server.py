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
