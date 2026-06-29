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
        assert result.usage["cache_hit_rate"] == 0.8
        assert client.sessions()[0].session_id == result.session_id
    finally:
        await client.close()


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
    assert any(event.type == "context.usage" for event in events)
    assert (
        "".join(
            event.data["text"] for event in events if event.type == "assistant.delta"
        )
        == "sdk response"
    )
    assert events[-1].type == "turn.completed"
    assert events[-1].data["response"] == "sdk response"
    assert events[-1].data["usage"]["cache_read_tokens"] == 80


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
