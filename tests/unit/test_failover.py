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
    primary = FakeProvider("primary", error=RuntimeError("offline"))
    primary.provider_family = "primary-route"
    backup = FakeProvider("backup")
    backup.provider_family = "backup-route"
    provider = FailoverProvider([primary, backup])
    chunks = [chunk async for chunk in provider.stream_chat([])]
    assert chunks[0].content == "backup"
    assert provider.model_name == "backup"
    assert provider.provider_family == "backup-route"
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


@pytest.mark.asyncio
async def test_failover_revalidates_tool_protocol_after_dynamic_capability_probe() -> None:
    class DynamicProvider(FakeProvider):
        def __init__(self, name: str, *, native_tools: bool) -> None:
            super().__init__(name)
            self._native_tools = native_tools
            self._probed = False

        @property
        def capabilities(self):
            from ash.providers.capabilities import ProviderCapabilities

            return ProviderCapabilities(native_tools=self._native_tools if self._probed else False)

        async def detect_capabilities(self, *, refresh: bool = False):
            del refresh
            self._probed = True
            return self.capabilities

    primary = DynamicProvider("primary", native_tools=False)
    backup = DynamicProvider("backup", native_tools=True)
    provider = FailoverProvider([primary, backup])

    with pytest.raises(RuntimeError, match="must agree on native tool support"):
        await provider.detect_capabilities()


@pytest.mark.asyncio
async def test_failover_dynamic_capability_probe_accepts_matching_protocols() -> None:
    class DynamicProvider(FakeProvider):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.probes = 0

        async def detect_capabilities(self, *, refresh: bool = False):
            from ash.providers.capabilities import ProviderCapabilities

            self.probes += 1
            return ProviderCapabilities(native_tools=True)

        @property
        def capabilities(self):
            from ash.providers.capabilities import ProviderCapabilities

            return ProviderCapabilities(native_tools=self.probes > 0)

    primary = DynamicProvider("primary")
    backup = DynamicProvider("backup")
    provider = FailoverProvider([primary, backup])

    capabilities = await provider.detect_capabilities()

    assert primary.probes == 1
    assert backup.probes == 1
    assert capabilities.native_tools is True


@pytest.mark.asyncio
async def test_failover_probe_failure_still_enforces_protocol_compatibility() -> None:
    from ash.providers.capabilities import ProviderCapabilities

    class FailingDynamicProvider(FakeProvider):
        @property
        def capabilities(self):
            return ProviderCapabilities(native_tools=False)

        async def detect_capabilities(self, *, refresh: bool = False):
            del refresh
            raise RuntimeError("catalog unavailable")

    class StaticNativeProvider(FakeProvider):
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

    provider = FailoverProvider(
        [FailingDynamicProvider("dynamic"), StaticNativeProvider("native")]
    )

    with pytest.raises(RuntimeError, match="must agree on native tool support"):
        await provider.detect_capabilities()


def test_failover_rejects_known_static_protocol_mismatch_even_with_dynamic_child() -> None:
    from ash.providers.capabilities import ProviderCapabilities

    class StaticFallback(FakeProvider):
        _ash_declared_capabilities = ProviderCapabilities(native_tools=False)

    class StaticNative(FakeProvider):
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

    class DynamicUnknown(FakeProvider):
        async def detect_capabilities(self, *, refresh: bool = False):
            del refresh
            return self.capabilities

    with pytest.raises(ValueError, match="must agree on native tool support"):
        FailoverProvider(
            [StaticFallback("fallback"), StaticNative("native"), DynamicUnknown("dynamic")]
        )


def test_failover_capabilities_are_conservative_across_entire_chain() -> None:
    class CapProvider(FakeProvider):
        def __init__(self, name: str, caps: ProviderCapabilities) -> None:
            super().__init__(name)
            self._caps = caps

        @property
        def capabilities(self):
            return self._caps

    first = CapProvider(
        "first",
        ProviderCapabilities(
            native_tools=True, vision=True, reasoning=True, local=False,
            context_window=200_000, max_output_tokens=16_000,
        ),
    )
    second = CapProvider(
        "second",
        ProviderCapabilities(
            native_tools=True, vision=False, reasoning=False, local=False,
            context_window=64_000, max_output_tokens=4_000,
        ),
    )
    provider = FailoverProvider([first, second])

    assert provider.capabilities == ProviderCapabilities(
        native_tools=True, vision=False, reasoning=False, local=False,
        context_window=64_000, max_output_tokens=4_000,
    )


def test_failover_token_count_uses_conservative_maximum_across_chain() -> None:
    class TokenProvider(FakeProvider):
        def __init__(self, name: str, multiplier: int) -> None:
            super().__init__(name)
            self.multiplier = multiplier

        def count_tokens(self, text: str) -> int:
            return len(text) * self.multiplier

    provider = FailoverProvider([TokenProvider("a", 1), TokenProvider("b", 3)])
    provider.active_index = 0
    assert provider.count_tokens("abcd") == 12
    provider.active_index = 1
    assert provider.count_tokens("abcd") == 12


def test_failover_forwards_completion_ceiling_to_every_child() -> None:
    class LimitProvider(FakeProvider):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.limits = []

        def configure_max_tokens(self, max_tokens: int) -> None:
            super().configure_max_tokens(max_tokens)
            self.limits.append(max_tokens)

    first = LimitProvider("first")
    second = LimitProvider("second")
    provider = FailoverProvider([first, second])

    provider.configure_max_tokens(321)

    assert first.limits == [321]
    assert second.limits == [321]
