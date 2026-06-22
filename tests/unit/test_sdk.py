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
        yield StreamChunk(content="<response>sdk response</response>", is_done=True)


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
