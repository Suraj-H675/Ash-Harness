from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace

import pytest

from ash.providers.base import CanonicalToolCall, ProviderABC, StreamChunk
from ash.providers.readiness import ProviderConnection, ProviderVerification


def _provider_command_config() -> SimpleNamespace:
    config = SimpleNamespace(
        model="openrouter/test-model",
        fallback_models=[],
    )
    config.model_copy = lambda *, update: SimpleNamespace(
        **{
            **config.__dict__,
            **update,
            "model_copy": config.model_copy,
        }
    )
    return config


def _provider_verification(*, selected: bool = True) -> ProviderVerification:
    return ProviderVerification(
        connection=ProviderConnection(
            provider="openrouter",
            model_name="test-model",
            base_url="https://openrouter.ai/api/v1",
            catalog_endpoint="https://openrouter.ai/api/v1/models",
            catalog_format="openai",
            auth_mode="bearer",
            api_key="secret-value",
        ),
        models=("test-model",) if selected else ("other-model",),
        selected_model_available=selected,
    )


class _ScriptedProbeProvider(ProviderABC):
    model_name = "test-model"

    def __init__(
        self,
        *,
        chunks: list[StreamChunk] | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.chunks = list(chunks or [])
        self.error = error
        self.delay = delay
        self.max_tokens: int | None = None
        self.closed = False
        self.messages = None
        self.tools = None

    def configure_max_tokens(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.messages = messages
        self.tools = tools
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _install_probe_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: ProviderABC,
) -> None:
    class Registry:
        def build(self, _config):
            return provider

    monkeypatch.setattr(
        "ash.providers.registry.get_provider_registry",
        lambda: Registry(),
    )


def test_provider_catalog_is_secret_free_and_includes_local_and_gateway_routes() -> None:
    from ash.commands.providers import provider_catalog_payload, render_provider_catalog

    payload = provider_catalog_payload()
    provider_ids = {item["id"] for item in payload["providers"]}

    assert {
        "google",
        "nvidia",
        "openrouter",
        "lmstudio",
        "vllm",
        "vertex",
        "bedrock",
        "azure",
    } <= provider_ids
    assert all("API_KEY" not in json.dumps(item) or item["key_env"] for item in payload["providers"])
    google = next(item for item in payload["providers"] if item["id"] == "google")
    openai = next(item for item in payload["providers"] if item["id"] == "openai")
    vertex = next(item for item in payload["providers"] if item["id"] == "vertex")
    bedrock = next(item for item in payload["providers"] if item["id"] == "bedrock")
    azure = next(item for item in payload["providers"] if item["id"] == "azure")
    assert openai["auth"] == "api-key-or-chatgpt"
    assert vertex["auth"] == "google-adc"
    assert bedrock["auth"] == "aws-sigv4"
    assert azure["auth"] == "azure-key-or-entra"
    assert google["key_env"] == "GOOGLE_API_KEY"
    assert google["key_envs"] == ["GOOGLE_API_KEY", "GEMINI_API_KEY"]
    rendered = render_provider_catalog()
    assert "Ash provider catalog" in rendered
    assert "openrouter" in rendered
    assert "lmstudio" in rendered
    assert "GOOGLE_API_KEY" in rendered
    assert "GEMINI_API_KEY" in rendered
    assert "API key /" in rendered
    assert "Entra" in rendered


def test_provider_test_rendering_never_includes_credentials() -> None:
    from ash.commands.providers import render_provider_test
    from ash.providers.readiness import ProviderConnection, ProviderVerification

    verification = ProviderVerification(
        connection=ProviderConnection(
            provider="openrouter",
            model_name="test-model",
            base_url="https://openrouter.ai/api/v1",
            catalog_endpoint="https://openrouter.ai/api/v1/models",
            catalog_format="openai",
            auth_mode="bearer",
            api_key="secret-value",
        ),
        models=("test-model",),
        selected_model_available=True,
        completion_attempted=True,
        completion_verified=True,
    )

    rendered = render_provider_test(verification, json_output=True)

    assert "secret-value" not in rendered
    assert json.loads(rendered)["ok"] is True


@pytest.mark.asyncio
async def test_provider_catalog_verification_uses_chatgpt_plan_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import providers
    from ash.config import AshConfig

    config = AshConfig(
        model="openai/gpt-plan",
        openai_auth_mode="chatgpt",
        fallback_models=["anthropic/claude-sonnet-4-6"],
    )
    verification = ProviderVerification(
        connection=ProviderConnection(
            provider="openai",
            model_name="gpt-plan",
            base_url="https://chatgpt.com/backend-api/codex",
            catalog_endpoint="https://chatgpt.com/backend-api/codex/models",
            catalog_format="openai",
            auth_mode="chatgpt",
        ),
        models=("gpt-plan",),
        selected_model_available=True,
    )
    seen = []

    async def verify_chatgpt(probe_config, *, timeout):
        seen.append((probe_config, timeout))
        return verification

    def fail_api_key_probe(*args, **kwargs):
        raise AssertionError("API-key catalog path must not run for ChatGPT-plan auth")

    monkeypatch.setattr(
        providers,
        "verify_chatgpt_plan_connection",
        verify_chatgpt,
    )
    monkeypatch.setattr(providers, "verify_provider_connection", fail_api_key_probe)

    result = await providers.verify_provider_catalog(config, timeout=2.5)

    assert result is verification
    assert len(seen) == 1
    probe_config, timeout = seen[0]
    assert probe_config.fallback_models == []
    assert probe_config.model == "openai/gpt-plan"
    assert timeout == 2.5


@pytest.mark.asyncio
async def test_provider_catalog_verification_uses_api_key_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import providers
    from ash.config import AshConfig

    config = AshConfig(
        model="openai/gpt-api",
        openai_auth_mode="api_key",
        fallback_models=["anthropic/claude-sonnet-4-6"],
    )
    verification = ProviderVerification(
        connection=ProviderConnection(
            provider="openai",
            model_name="gpt-api",
            base_url="https://api.openai.com/v1",
            catalog_endpoint="https://api.openai.com/v1/models",
            catalog_format="openai",
            auth_mode="bearer",
            api_key="secret",
        ),
        models=("gpt-api",),
        selected_model_available=True,
    )
    seen = []

    def verify_api_key(probe_config, *, timeout):
        seen.append((probe_config, timeout))
        return verification

    async def fail_chatgpt(*args, **kwargs):
        raise AssertionError("ChatGPT-plan catalog path must not run for API-key auth")

    monkeypatch.setattr(providers, "verify_provider_connection", verify_api_key)
    monkeypatch.setattr(
        providers,
        "verify_chatgpt_plan_connection",
        fail_chatgpt,
    )

    result = await providers.verify_provider_catalog(config, timeout=3.0)

    assert result is verification
    assert len(seen) == 1
    probe_config, timeout = seen[0]
    assert probe_config.fallback_models == []
    assert probe_config.model == "openai/gpt-api"
    assert timeout == 3.0


@pytest.mark.asyncio
async def test_bedrock_catalog_verification_uses_native_aws_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import providers
    from ash.config import AshConfig

    config = AshConfig(
        model="bedrock/us.anthropic.model",
        bedrock_region="us-west-2",
        bedrock_profile="engineering",
    )
    connection = ProviderConnection(
        provider="bedrock",
        model_name="us.anthropic.model",
        base_url="https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1",
        catalog_endpoint="",
        catalog_format="openai",
        auth_mode="aws_sigv4",
    )
    seen: list[tuple[str, str]] = []

    monkeypatch.setattr(
        providers,
        "resolve_provider_connection",
        lambda _config: connection,
    )
    monkeypatch.setattr(
        "ash.providers.bedrock.discover_bedrock_models",
        lambda *, region, profile: (
            seen.append((region, profile))
            or ("us.anthropic.model", "global.amazon.model")
        ),
    )

    result = await providers.verify_provider_catalog(config, timeout=2.0)

    assert result.connection is connection
    assert result.models == ("us.anthropic.model", "global.amazon.model")
    assert result.selected_model_available is True
    assert seen == [("us-west-2", "engineering")]


@pytest.mark.asyncio
async def test_vertex_catalog_verification_reports_manual_discovery_boundary() -> None:
    from ash.commands import providers
    from ash.config import AshConfig

    config = AshConfig(
        model="vertex/google/gemini-test",
        vertex_project="project-123",
        vertex_location="us-central1",
    )

    with pytest.raises(
        providers.ProviderVerificationError,
        match="does not expose a trustworthy.*live model catalog",
    ):
        await providers.verify_provider_catalog(config)


def test_vertex_provider_test_runs_completion_without_fake_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import providers
    from ash.config import AshConfig

    provider = _ScriptedProbeProvider(
        chunks=[
            StreamChunk(content="OK"),
            StreamChunk(is_done=True, stop_reason="stop"),
        ]
    )
    _install_probe_provider(monkeypatch, provider)
    config = AshConfig(
        model="vertex/google/gemini-test",
        vertex_project="project-123",
        vertex_location="global",
    )

    result = providers.test_provider(config, timeout=1.0)

    assert result.connection.provider == "vertex"
    assert result.selected_model_available is False
    assert result.catalog_authoritative is False
    assert result.models == ("google/gemini-test",)
    assert result.completion_attempted is True
    assert result.completion_verified is True
    assert result.ready_to_use is True


def test_bedrock_provider_test_uses_completion_as_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import providers
    from ash.config import AshConfig

    provider = _ScriptedProbeProvider(
        chunks=[
            StreamChunk(content="OK"),
            StreamChunk(is_done=True, stop_reason="stop"),
        ]
    )
    _install_probe_provider(monkeypatch, provider)
    config = AshConfig(
        model="bedrock/us.anthropic.model",
        bedrock_region="us-west-2",
    )

    result = providers.test_provider(config, timeout=1.0)

    assert result.connection.provider == "bedrock"
    assert result.selected_model_available is False
    assert result.catalog_authoritative is False
    assert result.models == ("us.anthropic.model",)
    assert result.completion_attempted is True
    assert result.completion_verified is True
    assert result.ready_to_use is True


def test_azure_provider_test_uses_completion_as_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import providers
    from ash.config import AshConfig

    provider = _ScriptedProbeProvider(
        chunks=[
            StreamChunk(content="OK"),
            StreamChunk(is_done=True, stop_reason="stop"),
        ]
    )
    _install_probe_provider(monkeypatch, provider)
    config = AshConfig(
        model="azure/deployment-a",
        azure_base_url="https://resource.openai.azure.com/openai/v1",
        azure_auth_mode="entra",
    )

    result = providers.test_provider(config, timeout=1.0)

    assert result.connection.provider == "azure"
    assert result.selected_model_available is False
    assert result.catalog_authoritative is False
    assert result.models == ("deployment-a",)
    assert result.completion_attempted is True
    assert result.completion_verified is True
    assert result.ready_to_use is True


def test_provider_test_does_not_report_ready_when_completion_probe_fails(
    monkeypatch,
) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    provider = _ScriptedProbeProvider(
        error=RuntimeError("completion endpoint rejected request")
    )
    _install_probe_provider(monkeypatch, provider)
    config = _provider_command_config()

    result = providers.test_provider(config, timeout=1.0)
    payload = providers.provider_test_payload(result)

    assert result.completion_attempted is True
    assert result.completion_verified is False
    assert "completion endpoint rejected request" in (result.completion_error or "")
    assert payload["ok"] is False
    assert provider.max_tokens == 8
    assert provider.closed is True


def test_provider_test_reports_ready_after_real_completion_probe(monkeypatch) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    provider = _ScriptedProbeProvider(
        chunks=[
            StreamChunk(content="OK"),
            StreamChunk(is_done=True, stop_reason="stop"),
        ]
    )
    _install_probe_provider(monkeypatch, provider)

    result = providers.test_provider(_provider_command_config(), timeout=1.0)

    assert result.completion_attempted is True
    assert result.completion_verified is True
    assert result.completion_error is None
    assert result.ready_to_use is True
    assert providers.provider_test_payload(result)["ok"] is True
    assert provider.max_tokens == 8
    assert provider.closed is True
    assert provider.tools is None
    assert provider.messages == [{"role": "user", "content": "Reply with exactly OK."}]


def test_provider_test_skips_completion_when_selected_model_is_missing(
    monkeypatch,
) -> None:
    from ash.commands import providers

    verification = _provider_verification(selected=False)
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    def fail_registry():
        raise AssertionError("completion provider must not be built")

    monkeypatch.setattr(
        "ash.providers.registry.get_provider_registry",
        fail_registry,
    )

    result = providers.test_provider(_provider_command_config(), timeout=1.0)

    assert result.selected_model_available is False
    assert result.completion_attempted is False
    assert result.completion_verified is False
    assert result.ready_to_use is False


@pytest.mark.parametrize(
    ("chunks", "error_fragment"),
    [
        ([StreamChunk(content="OK"), StreamChunk(is_done=True, stop_reason="length")], "length"),
        ([StreamChunk(is_done=True, stop_reason="content_filter")], "content_filter"),
        ([StreamChunk(content="partial")], "before a terminal completion"),
        (
            [
                StreamChunk(content="OK", is_done=True, stop_reason="stop"),
                StreamChunk(content="late output"),
            ],
            "output after its terminal completion",
        ),
        (
            [
                StreamChunk(
                    native_tool_calls=[
                        CanonicalToolCall(
                            call_id="unexpected-call",
                            name="read_file",
                            arguments={"file_path": "x"},
                        )
                    ]
                )
            ],
            "unexpected tool call",
        ),
    ],
)
def test_provider_test_rejects_nonfinal_or_tool_completion_streams(
    monkeypatch,
    chunks: list[StreamChunk],
    error_fragment: str,
) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    provider = _ScriptedProbeProvider(chunks=chunks)
    _install_probe_provider(monkeypatch, provider)

    result = providers.test_provider(_provider_command_config(), timeout=1.0)

    assert result.completion_attempted is True
    assert result.completion_verified is False
    assert error_fragment in (result.completion_error or "")
    assert result.ready_to_use is False


def test_provider_completion_probe_obeys_timeout_and_closes(monkeypatch) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )
    provider = _ScriptedProbeProvider(delay=0.1)
    _install_probe_provider(monkeypatch, provider)

    result = providers.test_provider(_provider_command_config(), timeout=0.01)

    assert result.completion_attempted is True
    assert result.completion_verified is False
    assert result.completion_error == "provider completion probe timed out"
    assert provider.closed is True


@pytest.mark.asyncio
async def test_provider_completion_probe_preserves_failure_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import providers

    verification = _provider_verification()

    class FailingCloseProvider(_ScriptedProbeProvider):
        async def aclose(self) -> None:
            raise RuntimeError("provider cleanup failed")

    provider = FailingCloseProvider(
        error=RuntimeError("completion endpoint rejected request")
    )
    _install_probe_provider(monkeypatch, provider)

    verified, error = await providers._probe_provider_completion(
        _provider_command_config(),
        verification,
        timeout=1.0,
    )

    assert verified is False
    assert error is not None
    assert "completion endpoint rejected request" in error
    assert "cleanup failed" in error


@pytest.mark.asyncio
async def test_provider_completion_probe_settles_close_before_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import providers

    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_finished = asyncio.Event()

    class BlockingCloseProvider(_ScriptedProbeProvider):
        async def aclose(self) -> None:
            close_started.set()
            await release_close.wait()
            self.closed = True
            close_finished.set()

    provider = BlockingCloseProvider(
        chunks=[
            StreamChunk(content="OK"),
            StreamChunk(is_done=True, stop_reason="stop"),
        ]
    )
    _install_probe_provider(monkeypatch, provider)
    task = asyncio.create_task(
        providers._probe_provider_completion(
            _provider_command_config(),
            _provider_verification(),
            timeout=1.0,
        )
    )
    await asyncio.wait_for(close_started.wait(), timeout=1)

    try:
        task.cancel()
        await asyncio.sleep(0)

        assert task.done() is False
        assert close_finished.is_set() is False

        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert close_finished.is_set() is True
        assert provider.closed is True
    finally:
        release_close.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_provider_completion_error_redacts_connection_secret(monkeypatch) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    provider = _ScriptedProbeProvider(
        error=RuntimeError("upstream echoed secret-value")
    )
    _install_probe_provider(monkeypatch, provider)

    result = providers.test_provider(_provider_command_config(), timeout=1.0)
    rendered = providers.render_provider_test(result, json_output=True)

    assert "secret-value" not in (result.completion_error or "")
    assert "secret-value" not in rendered
    assert "[REDACTED]" in rendered


def test_provider_test_human_rendering_distinguishes_completion_failure() -> None:
    from ash.commands.providers import render_provider_test

    verification = ProviderVerification(
        **{
            **_provider_verification().__dict__,
            "completion_attempted": True,
            "completion_verified": False,
            "completion_error": "chat endpoint failed",
        }
    )

    rendered = render_provider_test(verification)

    assert "Catalog: 1 model(s); selected model available" in rendered
    assert "Completion: failed: chat endpoint failed" in rendered
    assert "model is available, but a completion could not be verified" in rendered


@pytest.mark.parametrize("json_output", [False, True])
def test_provider_test_error_redacts_credentials(json_output: bool) -> None:
    from ash.commands.providers import provider_test_error

    secret = "verylongprovidersecret"
    rendered = provider_test_error(
        RuntimeError(f"upstream failed api_key={secret}"),
        json_output=json_output,
    )

    assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_provider_test_error_sanitizes_human_terminal_controls() -> None:
    from ash.commands.providers import provider_test_error

    rendered = provider_test_error(
        RuntimeError("bad\nerror\x1b[2J\u202ehidden\u202c"),
        json_output=False,
    )

    assert "bad\\x0aerror\\x1b[2J\\u202ehidden\\u202c" in rendered
    assert "bad\nerror" not in rendered
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered


def test_main_lists_provider_catalog_without_loading_runtime_config(capsys) -> None:
    from ash.cli import main

    assert main(["providers", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(item["id"] == "openrouter" for item in payload["providers"])


@pytest.mark.parametrize(
    ("completion_verified", "expected_exit"),
    [(False, 1), (True, 0)],
)
def test_main_provider_test_exit_requires_verified_completion(
    monkeypatch,
    capsys,
    completion_verified: bool,
    expected_exit: int,
) -> None:
    import ash.cli as cli
    from ash.commands import providers

    verification = ProviderVerification(
        **{
            **_provider_verification().__dict__,
            "completion_attempted": True,
            "completion_verified": completion_verified,
            "completion_error": None if completion_verified else "chat failed",
        }
    )
    monkeypatch.setattr(
        cli,
        "_load_config_or_report",
        lambda **_kwargs: (_provider_command_config(), 0),
    )
    monkeypatch.setattr(
        providers,
        "test_provider",
        lambda _config, *, model=None, timeout=10.0: verification,
    )

    assert cli.main(["providers", "test", "--json"]) == expected_exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_model_available"] is True
    assert payload["completion_verified"] is completion_verified
    assert payload["ok"] is completion_verified


@pytest.mark.parametrize("timeout", [0.0, math.nan, math.inf, -math.inf])
def test_test_provider_rejects_invalid_timeout(timeout: float) -> None:
    from ash.commands.providers import test_provider

    with pytest.raises(ValueError, match="timeout must be positive and finite"):
        test_provider(SimpleNamespace(), timeout=timeout)
