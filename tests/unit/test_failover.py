import pytest

from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities, infer_capabilities
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


class NativeFakeProvider(FakeProvider):
    _ash_declared_capabilities = ProviderCapabilities(native_tools=True)


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


@pytest.mark.asyncio
async def test_failover_uses_backup_after_empty_primary_eof() -> None:
    class EmptyProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            if False:  # pragma: no cover - keep this an async generator
                yield StreamChunk()

    provider = FailoverProvider([EmptyProvider("empty"), FakeProvider("backup")])

    chunks = [chunk async for chunk in provider.stream_chat([])]

    assert [chunk.content for chunk in chunks] == ["backup"]
    assert provider.failures == [
        "empty: provider 'empty' ended before a terminal chunk"
    ]


@pytest.mark.asyncio
async def test_failover_uses_backup_after_metadata_only_terminal_error() -> None:
    class LimitedProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(is_done=True, stop_reason="rate_limit")

    provider = FailoverProvider([LimitedProvider("limited"), FakeProvider("backup")])

    chunks = [chunk async for chunk in provider.stream_chat([])]

    assert [chunk.content for chunk in chunks] == ["backup"]
    assert provider.failures == [
        "limited: provider reported an unsuccessful terminal outcome: rate_limit"
    ]


def test_capabilities_are_conservative_for_local_models() -> None:
    local = infer_capabilities("ollama", "unknown")
    assert local.local is True
    assert local.native_tools is False
    sonnet = infer_capabilities("anthropic", "claude-sonnet-4-6")
    assert sonnet.context_window == 1_000_000


def test_failover_rejects_mixed_native_and_fallback_protocols() -> None:
    with pytest.raises(ValueError, match="must agree on native tool support"):
        FailoverProvider([NativeFakeProvider("native"), FakeProvider("fallback")])
