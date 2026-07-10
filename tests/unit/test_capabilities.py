import pytest

from ash.sdk import AshClient
from config import AshConfig
from providers.base import ProviderABC, StreamChunk
from providers.capabilities import CapabilityRegistry, ProviderCapabilities
from providers.registry import ProviderRegistry, get_provider_registry


class DeclaredProvider(ProviderABC):
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="<response>registered provider</response>", is_done=True)


def test_capability_registry_supports_dynamic_provider_declarations() -> None:
    capabilities = CapabilityRegistry()
    providers = ProviderRegistry(capabilities)
    providers.register(
        "example",
        lambda config, model: DeclaredProvider(model),
        capabilities=lambda model: ProviderCapabilities(
            native_tools=model != "legacy",
            vision=True,
            context_window=32_000,
            max_output_tokens=4_000,
        ),
    )

    provider = providers.build(AshConfig(model="example/current"))

    assert provider.provider_family == "example"
    assert provider.capabilities == ProviderCapabilities(
        native_tools=True,
        vision=True,
        context_window=32_000,
        max_output_tokens=4_000,
    )
    assert capabilities.families() == ("example",)


def test_provider_unregister_removes_owned_capability_declaration() -> None:
    capabilities = CapabilityRegistry()
    providers = ProviderRegistry(capabilities)
    providers.register(
        "example",
        lambda config, model: DeclaredProvider(model),
        capabilities=lambda model: ProviderCapabilities(local=True),
    )

    assert providers.unregister("example") is True
    assert capabilities.families() == ()
    assert providers.unregister("example") is False


def test_capability_registry_rejects_duplicate_or_invalid_resolvers() -> None:
    registry = CapabilityRegistry()
    registry.register("example", lambda model: ProviderCapabilities())

    with pytest.raises(ValueError, match="already registered"):
        registry.register("example", lambda model: ProviderCapabilities())
    with pytest.raises(ValueError, match="cannot be empty"):
        registry.register("", lambda model: ProviderCapabilities())
    with pytest.raises(TypeError, match="must return"):
        registry.register("invalid", lambda model: object())  # type: ignore[arg-type]
        registry.resolve("invalid", "model")


def test_unknown_capability_family_uses_stable_defaults() -> None:
    assert CapabilityRegistry().resolve("missing", "model") == ProviderCapabilities()


@pytest.mark.asyncio
async def test_registered_provider_runs_through_shared_sdk_runtime(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    registry = get_provider_registry()
    registry.register(
        "runtime-example",
        lambda config, model: DeclaredProvider(model),
        capabilities=lambda model: ProviderCapabilities(
            native_tools=False,
            local=True,
            context_window=16_000,
        ),
    )
    config = AshConfig(
        model="runtime-example/model",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )
    try:
        async with await AshClient.create(
            config=config, workspace_trusted=False
        ) as client:
            result = await client.prompt("use the registered provider")

            assert result.response == "registered provider"
            assert client.loop.provider.capabilities.local is True
            assert client.loop.provider.capabilities.context_window == 16_000
    finally:
        registry.unregister("runtime-example")
