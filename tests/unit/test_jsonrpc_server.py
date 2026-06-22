import asyncio

import pytest

from ash.sdk import AshResult
from server.jsonrpc import JSONRPCServer


class FakeLoop:
    current_session = None
    project_root = "/tmp/project"
    _last_context_tokens = 0

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

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_jsonrpc_turn_validation_and_unknown_method() -> None:
    server = JSONRPCServer(FakeClient())  # type: ignore[arg-type]
    response = await server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "turn/run", "params": {"input": "hi"}}
    )
    assert response["result"]["response"] == "HI"
    invalid = await server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "turn/run", "params": {}}
    )
    assert invalid["error"]["code"] == -32602
    missing = await server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "missing"}
    )
    assert missing["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_jsonrpc_cancellation() -> None:
    client = FakeClient()

    async def slow(text):
        await asyncio.sleep(10)

    client.prompt = slow
    server = JSONRPCServer(client)  # type: ignore[arg-type]
    pending = asyncio.create_task(
        server.handle_request(
            {"jsonrpc": "2.0", "id": "slow", "method": "turn/run", "params": {"input": "wait"}}
        )
    )
    await asyncio.sleep(0)
    assert server.cancel("slow") is True
    response = await pending
    assert response["error"]["code"] == -32800
