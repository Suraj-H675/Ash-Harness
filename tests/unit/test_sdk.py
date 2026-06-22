import asyncio

import pytest

from ash.sdk import AshClient
from config import AshConfig
from providers.base import ProviderABC, StreamChunk


class SDKProvider(ProviderABC):
    @property
    def model_name(self) -> str:
        return "sdk-model"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="<response>sdk ")
        yield StreamChunk(content="response</response>", is_done=True)


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
        result = await client.prompt("hello")
        assert result.response == "sdk response"
        assert result.session_id
        assert client.sessions()[0].session_id == result.session_id
    finally:
        await client.close()


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
    assert any(event.type == "context.usage" for event in events)
    assert (
        "".join(
            event.data["text"] for event in events if event.type == "assistant.delta"
        )
        == "sdk response"
    )
    assert events[-1].type == "turn.completed"
    assert events[-1].data["response"] == "sdk response"


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
