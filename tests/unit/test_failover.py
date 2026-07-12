import pytest

from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import infer_capabilities
from ash.providers.failover import FailoverProvider


class FakeProvider(ProviderABC):
    def __init__(
        self,
        name: str,
        *,
        error: Exception | None = None,
        error_after_output: bool = False,
    ) -> None:
        self._name = name
        self.error = error
        self.error_after_output = error_after_output

    @property
    def model_name(self) -> str:
        return self._name

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        if self.error_after_output:
            yield StreamChunk(content="partial")
            raise self.error or RuntimeError("disconnected")
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


@pytest.mark.asyncio
async def test_failover_never_replays_after_output() -> None:
    provider = FailoverProvider(
        [
            FakeProvider(
                "primary",
                error=ConnectionError("disconnected"),
                error_after_output=True,
            ),
            FakeProvider("backup"),
        ]
    )
    chunks = []
    with pytest.raises(ConnectionError, match="disconnected"):
        async for chunk in provider.stream_chat([]):
            chunks.append(chunk)
    assert [chunk.content for chunk in chunks] == ["partial"]
    assert provider.model_name == "primary"


@pytest.mark.asyncio
async def test_failover_failure_diagnostics_are_request_scoped() -> None:
    primary = FakeProvider("primary", error=RuntimeError("first"))
    backup = FakeProvider("backup", error=RuntimeError("second"))
    provider = FailoverProvider([primary, backup])
    with pytest.raises(RuntimeError, match="first.*second"):
        _ = [chunk async for chunk in provider.stream_chat([])]

    primary.error = RuntimeError("new-first")
    backup.error = None
    chunks = [chunk async for chunk in provider.stream_chat([])]
    assert chunks[0].content == "backup"
    assert provider.failures == ["primary: new-first"]


def test_capabilities_are_conservative_for_local_models() -> None:
    local = infer_capabilities("ollama", "unknown")
    assert local.local is True
    assert local.native_tools is False
    sonnet = infer_capabilities("anthropic", "claude-sonnet-4-6")
    assert sonnet.context_window == 1_000_000
