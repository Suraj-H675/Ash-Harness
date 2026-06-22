import pytest

from providers.base import ProviderABC, StreamChunk
from providers.capabilities import infer_capabilities
from providers.failover import FailoverProvider


class FakeProvider(ProviderABC):
    def __init__(self, name: str, *, error: Exception | None = None) -> None:
        self._name = name
        self.error = error

    @property
    def model_name(self) -> str:
        return self._name

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        if self.error:
            raise self.error
        yield StreamChunk(content=self._name, is_done=True)


@pytest.mark.asyncio
async def test_failover_switches_only_before_output() -> None:
    provider = FailoverProvider(
        [FakeProvider("primary", error=RuntimeError("offline")), FakeProvider("backup")]
    )
    chunks = [chunk async for chunk in provider.stream_chat([])]
    assert chunks[0].content == "backup"
    assert provider.model_name == "backup"
    assert provider.failures == ["primary: offline"]


def test_capabilities_are_conservative_for_local_models() -> None:
    local = infer_capabilities("ollama", "unknown")
    assert local.local is True
    assert local.native_tools is False
    sonnet = infer_capabilities("anthropic", "claude-sonnet-4-6")
    assert sonnet.context_window == 1_000_000
