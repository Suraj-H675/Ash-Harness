"""Tests for cli/setup.py — provider flows and credential saving."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from .provider_test_helpers import patch_catalog_client


# ---------------------------------------------------------------------------
# Helpers to patch stdin / getpass
# ---------------------------------------------------------------------------


def _fake_input(values: list[str]) -> MagicMock:
    """Return a mock input() that cycles through a list of values."""
    it = iter(values)
    m = MagicMock()
    m.side_effect = lambda _: next(it)
    return m


class _FakeGetpass:
    """Fake getpass.getpass that returns a configured value."""

    def __init__(self, password: str) -> None:
        self._password = password

    def __call__(self, _: str = "") -> str:
        return self._password


# ---------------------------------------------------------------------------
# Provider flow tests
# ---------------------------------------------------------------------------


class TestHasProviderConfigured:
    """Tests for _has_provider_configured."""

    def test_true_when_api_key_set_for_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """provider/model + matching API key = configured."""
        mock_config = MagicMock(model="anthropic/claude-3-5-sonnet")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("HOME", "/tmp")

        with patch("ash.commands.setup.load_config", return_value={}):
            from ash.commands.setup import _has_provider_configured

            assert _has_provider_configured(mock_config) is True

    def test_true_when_api_key_in_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """API key in env vars + provider/model format = configured."""
        mock_config = MagicMock(model="anthropic/claude-3-5-sonnet")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("HOME", "/tmp")

        with patch("ash.commands.setup.load_config", return_value={}):
            from ash.commands.setup import _has_provider_configured

            assert _has_provider_configured(mock_config) is True

    def test_true_when_custom_providers_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom provider in config.custom_providers = configured."""
        mock_config = MagicMock(model="my-minimax/MiniMax-M2.7")
        mock_config.custom_providers = {
            "my-minimax": {"base_url": "https://api.minimax.io/v1"}
        }
        for key in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "GROQ_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HOME", "/tmp")

        with patch("ash.commands.setup.load_config", return_value={}):
            from ash.commands.setup import _has_provider_configured

            assert _has_provider_configured(mock_config) is True

    def test_false_when_completely_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No model, no API keys, no custom providers = not configured."""
        mock_config = MagicMock(model="")
        for key in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "GROQ_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HOME", "/tmp")

        with patch("ash.commands.setup.load_config", return_value={}):
            from ash.commands.setup import _has_provider_configured

            assert _has_provider_configured(mock_config) is False


class TestGetCurrentModel:
    """Tests for _get_current_model."""

    def test_extracts_model_name_from_provider_slash_model(self) -> None:
        """'anthropic/claude-3-5-sonnet' → 'claude-3-5-sonnet'."""
        from ash.commands.setup import _get_current_model

        mock_config = MagicMock(model="anthropic/claude-3-5-sonnet")
        assert _get_current_model(mock_config) == "claude-3-5-sonnet"

    def test_returns_raw_value_if_no_slash(self) -> None:
        """Model without slash is returned as-is."""
        from ash.commands.setup import _get_current_model

        mock_config = MagicMock(model="llama3")
        assert _get_current_model(mock_config) == "llama3"

    def test_empty_model(self) -> None:
        """Empty model returns empty string."""
        from ash.commands.setup import _get_current_model

        mock_config = MagicMock(model="")
        assert _get_current_model(mock_config) == ""

    def test_provider_specific_current_model_does_not_cross_providers(self) -> None:
        from ash.commands.setup import _get_current_model_for_provider

        config = MagicMock(model="anthropic/claude-example")
        assert _get_current_model_for_provider(config, "anthropic") == "claude-example"
        assert _get_current_model_for_provider(config, "openai") == ""


class TestAnthropicFlow:
    """Tests for _flow_anthropic — verifies correct env values are saved."""

    def test_saves_anthropic_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_flow_anthropic should save the given API key and ASH_MODEL."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # Clean env so get_env_value returns None (no existing key)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # _prompt_api_key calls getpass once, _prompt_model_list calls input once
        monkeypatch.setattr(
            "ash.commands.setup.getpass.getpass", _FakeGetpass("sk-ant-test123")
        )
        monkeypatch.setattr(
            "builtins.input", _fake_input(["", "1"])
        )  # default base URL, model selection

        from ash.commands.setup import ModelProbe, _flow_anthropic

        with (
            patch(
                "ash.commands.setup._probe_anthropic_models_detailed",
                return_value=ModelProbe(models=("claude-test",)),
            ),
            patch("ash.commands.setup.save_env_values") as mock_save,
        ):
            _flow_anthropic("")
            calls = mock_save.call_args.args[0]
            assert "ANTHROPIC_API_KEY" in calls
            assert calls["ANTHROPIC_API_KEY"] == "sk-ant-test123"
            assert "ASH_MODEL" in calls
            assert calls["ASH_MODEL"].startswith("anthropic/")

    def test_cancelled_model_selection_does_not_save_partial_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import ModelProbe, SetupCancelled, _flow_anthropic

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(
            "ash.commands.setup.getpass.getpass", _FakeGetpass("sk-new")
        )
        monkeypatch.setattr("builtins.input", _fake_input(["", "c"]))

        with (
            patch(
                "ash.commands.setup._probe_anthropic_models_detailed",
                return_value=ModelProbe(models=("model",)),
            ),
            patch("ash.commands.setup.save_env_values") as save,
            pytest.raises(SetupCancelled),
        ):
            _flow_anthropic("")
        save.assert_not_called()


class TestGroqFlow:
    """Tests for _flow_groq — verifies correct env values are saved."""

    def test_saves_groq_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_flow_groq should save the given API key and ASH_MODEL."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setattr(
            "ash.commands.setup.getpass.getpass", _FakeGetpass("gsk_groq_test")
        )
        monkeypatch.setattr(
            "builtins.input", _fake_input(["", "1"])
        )  # default endpoint, then select model index 1

        from ash.commands.setup import ModelProbe, _flow_groq

        with (
            patch(
                "ash.commands.setup._probe_models_detailed",
                return_value=ModelProbe(models=("groq-test",)),
            ),
            patch("ash.commands.setup.save_env_values") as mock_save,
        ):
            _flow_groq("")
            calls = mock_save.call_args.args[0]
            assert "GROQ_API_KEY" in calls
            assert calls["GROQ_API_KEY"] == "gsk_groq_test"
            assert "ASH_MODEL" in calls
            assert calls["ASH_MODEL"].startswith("groq/")


class TestEnterpriseCloudFlows:
    def test_vertex_setup_saves_scope_without_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands.setup import SetupOutcome, _flow_vertex

        class TokenProvider:
            async def __call__(self) -> str:
                return "short-lived-token"

        monkeypatch.setattr(
            "ash.commands.setup.importlib.util.find_spec",
            lambda name: object() if name == "google.auth" else None,
        )
        monkeypatch.setattr(
            "ash.providers.vertex.GoogleAdcTokenProvider",
            TokenProvider,
        )
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(["project-123", "us-central1", "google/gemini-test"]),
        )

        with patch("ash.commands.setup.save_env_values") as save:
            result = _flow_vertex(
                "",
                SimpleNamespace(vertex_project="", vertex_location=""),
            )

        assert result == SetupOutcome.SUCCESS
        assert save.call_args.args[0] == {
            "ASH_VERTEX_PROJECT": "project-123",
            "ASH_VERTEX_LOCATION": "us-central1",
            "ASH_MODEL": "vertex/google/gemini-test",
        }
        assert "short-lived-token" not in json.dumps(save.call_args.args[0])

    def test_bedrock_setup_saves_region_profile_and_discovered_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands.setup import SetupOutcome, _flow_bedrock

        class Provider:
            def __init__(self, model, *, region, profile):
                assert model == "us.anthropic.model"
                assert region == "us-west-2"
                assert profile == "engineering"

            async def aclose(self) -> None:
                return None

        monkeypatch.setattr(
            "builtins.input",
            _fake_input(["us-west-2", "engineering", "1"]),
        )
        monkeypatch.setattr(
            "ash.commands.setup._probe_bedrock_models_detailed",
            lambda region, profile: (
                __import__("ash.commands.setup", fromlist=["ModelProbe"]).ModelProbe(
                    models=("us.anthropic.model",)
                )
            ),
        )
        monkeypatch.setattr(
            "ash.providers.bedrock.BedrockProvider",
            Provider,
        )

        with patch("ash.commands.setup.save_env_values") as save:
            result = _flow_bedrock(
                "",
                SimpleNamespace(bedrock_region="", bedrock_profile=""),
            )

        assert result == SetupOutcome.SUCCESS
        assert save.call_args.args[0] == {
            "ASH_BEDROCK_REGION": "us-west-2",
            "ASH_BEDROCK_PROFILE": "engineering",
            "ASH_MODEL": "bedrock/us.anthropic.model",
        }

    def test_azure_entra_setup_verifies_identity_and_scrubs_saved_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands.setup import SetupOutcome, _flow_azure

        events: list[str] = []

        class Provider:
            def __init__(self, model, *, base_url, auth_mode):
                assert model == "credential-probe"
                assert base_url == "https://resource.openai.azure.com/openai/v1"
                assert auth_mode == "entra"

            async def verify_credentials(self) -> None:
                events.append("verified")

            async def aclose(self) -> None:
                events.append("closed")

        monkeypatch.setattr("ash.providers.azure.AzureProvider", Provider)
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(
                [
                    "https://resource.openai.azure.com",
                    "entra",
                    "deployment-a",
                ]
            ),
        )

        with patch("ash.commands.setup.save_env_values") as save:
            result = _flow_azure(
                "",
                SimpleNamespace(azure_base_url="", azure_auth_mode="entra"),
            )

        assert result == SetupOutcome.SUCCESS
        assert events == ["verified", "closed"]
        assert save.call_args.args[0] == {
            "ASH_AZURE_BASE_URL": "https://resource.openai.azure.com/openai/v1",
            "ASH_AZURE_AUTH_MODE": "entra",
            "ASH_MODEL": "azure/deployment-a",
            "AZURE_OPENAI_API_KEY": "",
        }

    def test_azure_api_key_setup_persists_selected_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands.setup import SetupOutcome, _flow_azure

        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(
            "ash.commands.setup.getpass.getpass",
            _FakeGetpass("azure-key"),
        )
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(
                [
                    "https://resource.openai.azure.com",
                    "api_key",
                    "deployment-a",
                ]
            ),
        )

        with patch("ash.commands.setup.save_env_values") as save:
            result = _flow_azure(
                "",
                SimpleNamespace(azure_base_url="", azure_auth_mode="entra"),
            )

        assert result == SetupOutcome.SUCCESS
        assert save.call_args.args[0] == {
            "ASH_AZURE_BASE_URL": "https://resource.openai.azure.com/openai/v1",
            "ASH_AZURE_AUTH_MODE": "api_key",
            "ASH_MODEL": "azure/deployment-a",
            "AZURE_OPENAI_API_KEY": "azure-key",
        }

    def test_azure_entra_setup_fails_closed_on_credential_cleanup_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands.setup import SetupOutcome, _flow_azure

        class Provider:
            def __init__(self, model, *, base_url, auth_mode):
                assert model == "credential-probe"
                assert base_url == "https://resource.openai.azure.com/openai/v1"
                assert auth_mode == "entra"

            async def verify_credentials(self) -> None:
                return None

            async def aclose(self) -> None:
                raise RuntimeError("close failed")

        monkeypatch.setattr("ash.providers.azure.AzureProvider", Provider)
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(
                [
                    "https://resource.openai.azure.com",
                    "entra",
                ]
            ),
        )

        with patch("ash.commands.setup.save_env_values") as save:
            result = _flow_azure(
                "",
                SimpleNamespace(azure_base_url="", azure_auth_mode="entra"),
            )

        assert result == SetupOutcome.ERROR
        save.assert_not_called()


class TestOpenAIFlow:
    def test_custom_base_url_is_used_for_discovery_and_saved_atomically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import ModelProbe, _flow_openai

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)
        monkeypatch.setattr(
            "ash.commands.setup.getpass.getpass", _FakeGetpass("sk-openai")
        )
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(["2", "https://gateway.example/v1/", "1"]),
        )

        with (
            patch(
                "ash.commands.setup._probe_models_detailed",
                return_value=ModelProbe(models=("gateway-model",)),
            ) as probe,
            patch("ash.commands.setup.save_env_values") as save,
        ):
            _flow_openai("", SimpleNamespace(openai_auth_mode="api_key"))

        probe.assert_called_once_with("https://gateway.example/v1", "sk-openai")
        assert save.call_args.args[0] == {
            "OPENAI_API_KEY": "sk-openai",
            "OPENAI_API_BASE": "https://gateway.example/v1",
            "ASH_OPENAI_AUTH_MODE": "api_key",
            "ASH_MODEL": "openai/gateway-model",
        }

    def test_chatgpt_plan_flow_saves_auth_mode_without_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import SetupOutcome, _flow_openai
        from ash.providers.openai_chatgpt_auth import ChatGPTAccount

        account = ChatGPTAccount(
            client_id="oaiapp_setup-client",
            subject="subject",
            email="user@example.com",
            issuer="https://auth.openai.com",
            ext_agent_host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
            id_token="id-token",
            access_token="access-token",
            refresh_token="refresh-token",
            token_type="Bearer",
            scopes=("chatgpt.tokens.use.direct",),
            expires_at=9_999_999_999.0,
            earliest_refresh_at=0.0,
            saved_at=1.0,
        )

        class Store:
            def active(self):
                return account

        class Manager:
            async def list_models(self):
                return (("gpt-plan", "GPT Plan"),)

        monkeypatch.setattr("builtins.input", _fake_input(["1", "1"]))
        monkeypatch.setattr(
            "ash.providers.openai_chatgpt_auth.ChatGPTCredentialStore",
            Store,
        )
        monkeypatch.setattr(
            "ash.providers.openai_chatgpt_auth.ChatGPTAuthManager",
            Manager,
        )
        with patch("ash.commands.setup.save_env_values") as save:
            result = _flow_openai(
                "",
                SimpleNamespace(openai_auth_mode="chatgpt"),
            )

        assert result == SetupOutcome.SUCCESS
        assert save.call_args.args[0] == {
            "ASH_OPENAI_AUTH_MODE": "chatgpt",
            "ASH_MODEL": "openai/gpt-plan",
        }

    def test_chatgpt_setup_reauthorizes_saved_registration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import SetupOutcome, _flow_openai
        from ash.providers.openai_chatgpt_auth import ChatGPTAccount

        signed_out = ChatGPTAccount(
            client_id="oaiapp_saved-client",
            subject="subject",
            email="user@example.com",
            issuer="https://auth.openai.com",
            ext_agent_host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
            id_token="",
            access_token="",
            refresh_token="",
            token_type="Bearer",
            scopes=(),
            expires_at=0.0,
            earliest_refresh_at=0.0,
            saved_at=1.0,
        )
        enabled = ChatGPTAccount(
            **{
                **signed_out.__dict__,
                "id_token": "id-token",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "scopes": ("chatgpt.tokens.use.direct",),
                "expires_at": 9_999_999_999.0,
            }
        )
        login_client_ids: list[str | None] = []

        class Store:
            def active(self):
                return signed_out

        class Manager:
            async def login(self, *, client_id=None):
                login_client_ids.append(client_id)
                return enabled

            async def list_models(self):
                return (("gpt-plan", "GPT Plan"),)

        monkeypatch.setattr("builtins.input", _fake_input(["1", "1"]))
        monkeypatch.setattr(
            "ash.providers.openai_chatgpt_auth.ChatGPTCredentialStore",
            Store,
        )
        monkeypatch.setattr(
            "ash.providers.openai_chatgpt_auth.ChatGPTAuthManager",
            Manager,
        )
        with patch("ash.commands.setup.save_env_values"):
            result = _flow_openai(
                "",
                SimpleNamespace(openai_auth_mode="chatgpt"),
            )

        assert result == SetupOutcome.SUCCESS
        assert login_client_ids == ["oaiapp_saved-client"]


@pytest.mark.parametrize(
    ("provider_id", "key_env", "base_url", "model", "catalog_format"),
    [
        (
            "google",
            "GOOGLE_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-test",
            "openai",
        ),
        (
            "nvidia",
            "NVIDIA_API_KEY",
            "https://integrate.api.nvidia.com/v1",
            "nvidia/test-model",
            "openai",
        ),
        (
            "together",
            "TOGETHER_API_KEY",
            "https://api.together.xyz/v1",
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "together",
        ),
    ],
)
def test_openai_compatible_builtin_provider_onboarding(
    provider_id: str,
    key_env: str,
    base_url: str,
    model: str,
    catalog_format: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import ModelProbe, _flow_openai_compatible_builtin
    from ash.provider_catalog import get_provider_descriptor
    from ash.providers.readiness import GOOGLE_API_CLIENT_HEADER

    descriptor = get_provider_descriptor(provider_id)
    assert descriptor is not None
    monkeypatch.delenv(key_env, raising=False)
    monkeypatch.setattr(
        "ash.commands.setup.getpass.getpass",
        _FakeGetpass("provider-secret"),
    )
    monkeypatch.setattr("builtins.input", _fake_input(["", "1"]))

    with (
        patch(
            "ash.commands.setup._probe_models_detailed",
            return_value=ModelProbe(models=(model,)),
        ) as probe,
        patch("ash.commands.setup.save_env_values") as save,
    ):
        _flow_openai_compatible_builtin(descriptor, "")

    expected_headers = (
        {"x-goog-api-client": GOOGLE_API_CLIENT_HEADER}
        if provider_id == "google"
        else None
    )
    probe.assert_called_once_with(
        base_url,
        "provider-secret",
        catalog_format=catalog_format,
        extra_headers=expected_headers,
    )
    assert save.call_args.args[0] == {
        "ASH_MODEL": f"{provider_id}/{model}",
        key_env: "provider-secret",
    }


@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [
        ("lmstudio", "lms server start"),
        ("vllm", "vllm serve <model>"),
    ],
)
def test_local_runtime_setup_guidance_is_runtime_specific(
    provider_id: str,
    expected: str,
) -> None:
    from ash.commands.setup import _local_runtime_guidance

    assert expected in _local_runtime_guidance(provider_id)


def test_google_onboarding_uses_documented_key_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import ModelProbe, _flow_openai_compatible_builtin
    from ash.provider_catalog import get_provider_descriptor
    from ash.providers.readiness import GOOGLE_API_CLIENT_HEADER

    descriptor = get_provider_descriptor("google")
    assert descriptor is not None
    monkeypatch.setenv("GOOGLE_API_KEY", "preferred-google-key")
    monkeypatch.setenv("GEMINI_API_KEY", "fallback-gemini-key")
    monkeypatch.setattr("builtins.input", _fake_input(["n", "", "1"]))

    with (
        patch(
            "ash.commands.setup._probe_models_detailed",
            return_value=ModelProbe(models=("gemini-test",)),
        ) as probe,
        patch("ash.commands.setup.save_env_values") as save,
    ):
        _flow_openai_compatible_builtin(descriptor, "")

    probe.assert_called_once_with(
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "preferred-google-key",
        catalog_format="openai",
        extra_headers={"x-goog-api-client": GOOGLE_API_CLIENT_HEADER},
    )
    assert save.call_args.args[0] == {
        "ASH_MODEL": "google/gemini-test",
        "GOOGLE_API_KEY": "preferred-google-key",
    }


def test_google_setup_status_accepts_gemini_api_key_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import _provider_status
    from ash.provider_catalog import get_provider_descriptor

    descriptor = get_provider_descriptor("google")
    assert descriptor is not None
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fallback-gemini-key")

    assert _provider_status(object(), descriptor) == "key detected"


def test_enterprise_cloud_setup_status_uses_scope_not_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import _provider_status
    from ash.provider_catalog import get_provider_descriptor

    vertex = get_provider_descriptor("vertex")
    bedrock = get_provider_descriptor("bedrock")
    azure = get_provider_descriptor("azure")
    assert vertex is not None
    assert bedrock is not None
    assert azure is not None
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)

    assert (
        _provider_status(
            SimpleNamespace(
                vertex_project="project-123",
                vertex_location="us-central1",
            ),
            vertex,
        )
        == "ADC scope configured"
    )
    assert (
        _provider_status(
            SimpleNamespace(bedrock_region="us-west-2"),
            bedrock,
        )
        == "AWS scope configured"
    )
    assert _provider_status(SimpleNamespace(), vertex) == "needs project/location"
    assert _provider_status(SimpleNamespace(), bedrock) == "needs AWS region"
    assert (
        _provider_status(
            SimpleNamespace(
                azure_base_url="https://resource.openai.azure.com/openai/v1",
                azure_auth_mode="entra",
            ),
            azure,
        )
        == "Entra scope configured"
    )
    assert _provider_status(SimpleNamespace(), azure) == "needs Azure endpoint"


@pytest.mark.parametrize(
    ("provider_id", "key_env", "base_env", "model"),
    [
        ("google", "GOOGLE_API_KEY", "GOOGLE_API_BASE", "gemini-test"),
        ("nvidia", "NVIDIA_API_KEY", "NVIDIA_API_BASE", "nvidia/test-model"),
    ],
)
def test_openai_compatible_builtin_provider_saves_base_override(
    provider_id: str,
    key_env: str,
    base_env: str,
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import ModelProbe, _flow_openai_compatible_builtin
    from ash.provider_catalog import get_provider_descriptor
    from ash.providers.readiness import GOOGLE_API_CLIENT_HEADER

    descriptor = get_provider_descriptor(provider_id)
    assert descriptor is not None
    monkeypatch.delenv(key_env, raising=False)
    monkeypatch.setattr(
        "ash.commands.setup.getpass.getpass",
        _FakeGetpass("provider-secret"),
    )
    monkeypatch.setattr(
        "builtins.input",
        _fake_input(["https://gateway.example/v1/", "1"]),
    )

    with (
        patch(
            "ash.commands.setup._probe_models_detailed",
            return_value=ModelProbe(models=(model,)),
        ) as probe,
        patch("ash.commands.setup.save_env_values") as save,
    ):
        _flow_openai_compatible_builtin(descriptor, "")

    expected_headers = (
        {"x-goog-api-client": GOOGLE_API_CLIENT_HEADER}
        if provider_id == "google"
        else None
    )
    probe.assert_called_once_with(
        "https://gateway.example/v1",
        "provider-secret",
        catalog_format="openai",
        extra_headers=expected_headers,
    )
    assert save.call_args.args[0] == {
        "ASH_MODEL": f"{provider_id}/{model}",
        key_env: "provider-secret",
        base_env: "https://gateway.example/v1",
    }


class TestDiscoveryRecovery:
    def test_probe_can_retry_then_verify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ash.commands.setup import ModelProbe, _discover_models

        monkeypatch.setattr("builtins.input", _fake_input(["r"]))
        probe = MagicMock(
            side_effect=[
                ModelProbe(error="HTTP 503"),
                ModelProbe(models=("model-a",)),
            ]
        )

        assert _discover_models("Provider", probe) == (["model-a"], True)
        assert probe.call_count == 2

    def test_probe_can_continue_explicitly_without_verification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import ModelProbe, _discover_models

        monkeypatch.setattr("builtins.input", _fake_input(["s"]))

        assert _discover_models(
            "Provider",
            lambda: ModelProbe(error="offline"),
            fallback=["manual-model"],
        ) == (["manual-model"], False)


class TestOpenaiCompatibleFlow:
    """Tests for _flow_openai_compatible — verifies TOML save."""

    def test_preserves_existing_configuration_when_adding_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adding a custom endpoint must not erase unrelated user settings."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(
                [
                    "my-minimax",
                    "https://api.minimax.io/v1",
                    "MiniMax-M2.7",
                ]
            ),
        )
        monkeypatch.setattr("ash.commands.setup.getpass.getpass", _FakeGetpass(""))

        from ash.commands import config as cli_config
        from ash.commands.setup import ModelProbe, _flow_openai_compatible

        cli_config.save_config(
            {
                "theme": "light",
                "sandbox_backend": "native",
                "fallback_models": ["ollama/local"],
            }
        )

        with patch(
            "ash.commands.setup._probe_models_detailed",
            return_value=ModelProbe(models=("MiniMax-M2.7",)),
        ):
            _flow_openai_compatible()

        saved = cli_config.load_config(strict=True)
        assert saved["theme"] == "light"
        assert saved["sandbox_backend"] == "native"
        assert saved["fallback_models"] == ["ollama/local"]
        assert saved["custom_providers"]["my-minimax"]["base_url"] == (
            "https://api.minimax.io/v1"
        )

    def test_saves_custom_provider_to_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom endpoint metadata is saved without embedding its API key."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # provider name, base URL, model name
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(
                [
                    "my-minimax",
                    "https://api.minimax.io/v1",
                    "MiniMax-M2.7",
                ]
            ),
        )
        monkeypatch.setattr(
            "ash.commands.setup.getpass.getpass", _FakeGetpass("sk-cp-test")
        )

        from ash.commands.setup import ModelProbe

        with patch(
            "ash.commands.setup._probe_models_detailed",
            return_value=ModelProbe(models=("MiniMax-M2.7",)),
        ):
            from ash.commands.config import load_config
            from ash.commands.setup import _flow_openai_compatible

            _flow_openai_compatible()
            saved = load_config(strict=True)
            assert "custom_providers" in saved
            assert "my-minimax" in saved["custom_providers"]
            cp = saved["custom_providers"]["my-minimax"]
            assert cp["base_url"] == "https://api.minimax.io/v1"
            assert cp["key_env"] == "ASH_PROVIDER_MY_MINIMAX_API_KEY"
            assert cp["auth_mode"] == "bearer"
            assert "api_key" not in cp
            env_text = (tmp_path / ".ash" / ".env").read_text()
            assert "ASH_PROVIDER_MY_MINIMAX_API_KEY=sk-cp-test\n" in env_text
            assert "ASH_MODEL=my-minimax/MiniMax-M2.7\n" in env_text

    def test_normalizes_custom_provider_name_to_runtime_identifier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(["MyProvider", "http://127.0.0.1:8000/v1", "local-model"]),
        )
        monkeypatch.setattr("ash.commands.setup.getpass.getpass", _FakeGetpass(""))

        from ash.commands.setup import ModelProbe, _flow_openai_compatible

        with (
            patch(
                "ash.commands.setup._probe_models_detailed",
                return_value=ModelProbe(models=("local-model",)),
            ),
        ):
            _flow_openai_compatible()

        from ash.commands.config import load_config

        saved = load_config(strict=True)
        assert "myprovider" in saved["custom_providers"]
        assert "MyProvider" not in saved["custom_providers"]
        env_text = (tmp_path / ".ash" / ".env").read_text()
        assert "ASH_MODEL=myprovider/local-model\n" in env_text

    @pytest.mark.parametrize("provider_name", ["my:provider", "-provider"])
    def test_rejects_custom_provider_names_runtime_cannot_parse(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        provider_name: str,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr("builtins.input", _fake_input([provider_name]))

        from ash.commands.setup import SetupBack, _flow_openai_compatible

        with (
            patch("ash.commands.setup._probe_models_detailed") as probe,
            patch("ash.commands.setup.mutate_config") as mutate_config,
            pytest.raises(SetupBack),
        ):
            _flow_openai_compatible()

        probe.assert_not_called()
        mutate_config.assert_not_called()

    def test_saves_anonymous_custom_provider_without_a_missing_key_requirement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(["local", "http://127.0.0.1:8000/v1", "local-model"]),
        )
        monkeypatch.setattr("ash.commands.setup.getpass.getpass", _FakeGetpass(""))

        from ash.commands.setup import ModelProbe, _flow_openai_compatible

        with (
            patch(
                "ash.commands.setup._probe_models_detailed",
                return_value=ModelProbe(models=("local-model",)),
            ),
        ):
            _flow_openai_compatible()

        from ash.commands.config import load_config

        custom = load_config(strict=True)["custom_providers"]["local"]
        assert custom["auth_mode"] == "none"
        assert "key_env" not in custom

    def test_rejects_plaintext_remote_endpoint_before_probe_or_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(["remote", "http://gateway.example/v1"]),
        )
        monkeypatch.setattr(
            "ash.commands.setup.getpass.getpass", _FakeGetpass("gateway-secret")
        )

        from ash.commands.setup import _flow_openai_compatible

        with (
            patch("ash.commands.setup._probe_models_detailed") as probe,
            patch("ash.commands.setup.mutate_config") as mutate_config,
            pytest.raises(ValueError, match="must use HTTPS"),
        ):
            _flow_openai_compatible()

        probe.assert_not_called()
        mutate_config.assert_not_called()

    def test_rejects_unbounded_numeric_model_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(
                ["custom", "https://gateway.example/v1", "9" * 5000]
            ),
        )
        monkeypatch.setattr("ash.commands.setup.getpass.getpass", _FakeGetpass(""))

        from ash.commands.setup import ModelProbe, SetupBack, _flow_openai_compatible

        with (
            patch(
                "ash.commands.setup._probe_models_detailed",
                return_value=ModelProbe(models=("model-1",)),
            ),
            pytest.raises(SetupBack),
        ):
            _flow_openai_compatible()

        assert "Invalid selection." in capsys.readouterr().out


def test_setup_numbered_prompts_reject_unbounded_numeric_input(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from ash.commands.setup import _prompt_choice, _prompt_model_list, _prompt_position

    huge_number = "9" * 5000
    monkeypatch.setattr(
        "ash.commands.setup._prompt_setup_text", lambda prompt: huge_number
    )
    assert _prompt_position(3) is None

    monkeypatch.setattr("builtins.input", _fake_input([huge_number, "1"]))
    assert _prompt_model_list(["model-1"], "") == "model-1"

    monkeypatch.setattr("builtins.input", _fake_input(["1"]))
    assert (
        _prompt_model_list(["model\nname\x1b[2J\u202ehidden\u202c"], "")
        == "model\nname\x1b[2J\u202ehidden\u202c"
    )

    monkeypatch.setattr("builtins.input", _fake_input([huge_number, "1"]))
    assert _prompt_choice("Pick", ["one"], 0) == 0

    output = capsys.readouterr().out
    assert "Position must be a number" in output
    assert "Invalid number." in output
    assert "Invalid choice." in output
    assert "model\\x0aname\\x1b[2J\\u202ehidden\\u202c" in output
    assert "model\nname" not in output
    assert "\x1b[2J" not in output
    assert "\u202e" not in output


def test_provider_catalog_render_exposes_full_breadth(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands.setup import PROVIDERS, _render_provider_catalog

    monkeypatch.setattr(
        "ash.commands.setup.get_env_value",
        lambda _name: None,
    )
    config = SimpleNamespace(model="", openai_auth_mode="api_key")

    _render_provider_catalog(config, list(PROVIDERS))

    output = capsys.readouterr().out
    assert "Providers  ·  21 routes" in output
    assert "OpenRou" in output
    assert "Hugging" in output
    assert "Vercel AI Gateway" in output
    assert "Google Gemini" in output
    assert "Ollama" in output
    assert "LM Studio" in output
    assert "vLLM" in output
    assert "Custom endpoint" in output
    assert "manual setup" in output


def test_provider_picker_accepts_name_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import _prompt_provider

    monkeypatch.setattr(
        "ash.commands.setup.get_env_value",
        lambda _name: None,
    )
    monkeypatch.setattr("builtins.input", _fake_input(["/router", "openrouter"]))

    selected = _prompt_provider(
        SimpleNamespace(model="", openai_auth_mode="api_key")
    )

    assert selected.id == "openrouter"


def test_first_run_provider_scope_defaults_to_common_cloud_apis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import _prompt_first_run_provider_scope

    monkeypatch.setattr("ash.commands.setup.get_env_value", lambda _name: None)
    monkeypatch.setattr("builtins.input", _fake_input(["1"]))

    selected = _prompt_first_run_provider_scope(
        SimpleNamespace(model="", openai_auth_mode="api_key")
    )

    assert [descriptor.id for descriptor in selected] == [
        "anthropic",
        "openai",
        "google",
    ]


def test_first_run_provider_scope_surfaces_detected_route_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import _prompt_first_run_provider_scope

    monkeypatch.setattr(
        "ash.commands.setup.get_env_value",
        lambda name: "secret" if name == "OPENAI_API_KEY" else None,
    )
    monkeypatch.setattr("builtins.input", _fake_input(["1"]))

    selected = _prompt_first_run_provider_scope(
        SimpleNamespace(model="", openai_auth_mode="api_key")
    )

    assert [descriptor.id for descriptor in selected] == ["openai"]


def test_scoped_provider_picker_numbers_the_visible_scope(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands.setup import PROVIDERS, _prompt_provider

    gateways = [descriptor for descriptor in PROVIDERS if descriptor.category == "Gateway"]
    monkeypatch.setattr("ash.commands.setup.get_env_value", lambda _name: None)
    monkeypatch.setattr("builtins.input", _fake_input(["1"]))

    selected = _prompt_provider(
        SimpleNamespace(model="", openai_auth_mode="api_key"),
        initial_scope=gateways,
    )

    assert selected.id == "openrouter"
    output = capsys.readouterr().out
    assert "OpenRouter" in output
    assert "Invalid choice." not in output


def test_scoped_provider_picker_does_not_default_to_hidden_current_provider(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands.setup import PROVIDERS, _prompt_provider

    local = [
        descriptor for descriptor in PROVIDERS if descriptor.category == "Local runtime"
    ]
    monkeypatch.setattr("ash.commands.setup.get_env_value", lambda _name: None)
    monkeypatch.setattr("builtins.input", _fake_input(["", "1"]))

    selected = _prompt_provider(
        SimpleNamespace(
            model="anthropic/claude-sonnet-5-5",
            openai_auth_mode="api_key",
        ),
        initial_scope=local,
    )

    assert selected.id == "ollama"
    output = capsys.readouterr().out
    assert "Provider [Anthropic]" not in output
    assert "Choose a visible provider by number or name." in output


def test_provider_catalog_marks_filtered_scope(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands.setup import PROVIDERS, _render_provider_catalog

    monkeypatch.setattr("ash.commands.setup.get_env_value", lambda _name: None)
    common = [
        descriptor
        for descriptor in PROVIDERS
        if descriptor.id in {"anthropic", "openai", "google"}
    ]

    _render_provider_catalog(
        SimpleNamespace(model="", openai_auth_mode="api_key"),
        common,
    )

    assert "3 shown / 21 routes" in capsys.readouterr().out


def test_large_setup_choice_menu_renders_numbered_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands.setup import _prompt_choice

    monkeypatch.setattr("builtins.input", _fake_input(["2"]))
    options = [
        "Common cloud APIs",
        "Other cloud APIs",
        "Gateways and routers",
        "Enterprise cloud",
        "Local runtimes",
    ]

    assert _prompt_choice("How do you want to connect Ash?", options, 0) == 1

    output = capsys.readouterr().out
    assert "How do you want to connect Ash?:" in output
    assert "[1] Common cloud APIs (default)" in output
    assert "[5] Local runtimes" in output
    assert "'Common cloud APIs'/'Other cloud APIs'" not in output


def test_model_picker_bounds_large_catalog_and_filters(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands.setup import _prompt_model_list

    models = [f"provider/model-{index:02d}" for index in range(1, 31)]
    monkeypatch.setattr(
        "builtins.input",
        _fake_input(["/model-30", "30"]),
    )

    assert _prompt_model_list(models, "") == "provider/model-30"

    output = capsys.readouterr().out
    assert "30 discovered" in output
    assert "Showing 18 of 30 matches" in output
    assert "provider/model-30" in output
    assert "provider/model-29" not in output


def test_model_picker_makes_current_default_explicit(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands.setup import _prompt_model_list

    models = [f"provider/model-{index:02d}" for index in range(1, 31)]
    current = "provider/model-30"
    monkeypatch.setattr("builtins.input", _fake_input([""]))

    assert _prompt_model_list(models, current) == current

    output = capsys.readouterr().out
    assert "Current" in output
    assert current in output
    assert "Enter keeps it" in output


def test_provider_catalog_has_compact_narrow_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO

    from rich.console import Console

    from ash.commands.setup import PROVIDERS, _render_provider_catalog

    stream = StringIO()
    monkeypatch.setattr(
        "ash.commands.setup._setup_console",
        lambda: Console(file=stream, width=40, force_terminal=False),
    )
    monkeypatch.setattr(
        "ash.commands.setup.get_env_value",
        lambda _name: None,
    )

    _render_provider_catalog(
        SimpleNamespace(model="", openai_auth_mode="api_key"),
        list(PROVIDERS),
    )

    output = stream.getvalue()
    assert "Providers  ·  21 routes" in output
    assert "OpenRouter" in output
    assert "Hugging Face" in output
    assert "Type" not in output
    assert "About" not in output
    assert " key" in output
    assert "local" in output


def test_setup_status_shows_actionable_optional_capability_next_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO

    from rich.console import Console

    from ash.commands.setup import _render_setup_status

    stream = StringIO()
    monkeypatch.setattr(
        "ash.commands.setup._setup_console",
        lambda: Console(file=stream, width=100, force_terminal=False),
    )
    monkeypatch.setattr(
        "ash.commands.setup._setup_status_payload",
        lambda _config: {
            "profile": "default",
            "model": "openai/gpt-test",
            "provider": {"name": "OpenAI", "ready": True},
            "fallback_models": [],
            "capabilities": {
                "web_search": {"configured": False},
                "browser": {"installed": False},
                "mcp": {"configured": False},
                "observability": {
                    "enabled": False,
                    "available": True,
                    "ready": False,
                    "sample_rate": 1.0,
                    "content_capture": False,
                },
                "memory": {"backend": "sqlite"},
                "sandbox": {"backend": "auto"},
            },
        },
    )

    _render_setup_status(SimpleNamespace())

    output = stream.getvalue()
    assert "ash setup web" in output
    assert "ash setup browser" in output
    assert "ash mcp add" in output
    assert "Observability" in output
    assert "ASH_OBSERVABILITY_ENABLED" in output


def test_fresh_setup_status_does_not_present_default_model_as_user_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO

    from rich.console import Console

    from ash.commands.setup import _render_setup_status

    stream = StringIO()
    monkeypatch.setattr(
        "ash.commands.setup._setup_console",
        lambda: Console(file=stream, width=80, force_terminal=False),
    )
    monkeypatch.setattr(
        "ash.commands.setup._setup_status_payload",
        lambda _config: {
            "profile": "default",
            "model": "anthropic/claude-sonnet-5-5",
            "provider": {"name": "Anthropic", "ready": False},
            "fallback_models": [],
            "capabilities": {
                "web_search": {"configured": False},
                "browser": {"installed": False},
                "mcp": {"configured": False},
                "observability": {
                    "enabled": False,
                    "available": False,
                    "ready": False,
                },
                "memory": {"backend": "sqlite"},
                "sandbox": {"backend": "auto"},
            },
        },
    )
    config = SimpleNamespace(config_source=lambda field: ("default", "built-in"))

    _render_setup_status(config, title="Before you begin", show_capabilities=False)

    output = stream.getvalue()
    assert "Provider" in output
    assert "Not connected" in output
    assert "Not selected" in output
    assert "Anthropic" not in output
    assert "Web search" not in output


class TestProbeModels:
    """Tests for _probe_models and _probe_ollama_models."""

    def test_probe_models_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_probe_models returns model IDs on 200 response."""
        patch_catalog_client(
            monkeypatch,
            lambda request: httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "gpt-4o"},
                        {"id": "gpt-4o-mini"},
                    ],
                },
                request=request,
            ),
        )
        monkeypatch.setenv("HOME", "/tmp")
        from ash.commands.setup import _probe_models

        result = _probe_models("https://api.openai.com/v1", "sk-test")
        assert result == ["gpt-4o", "gpt-4o-mini"]

    def test_openai_probe_uses_shared_catalog_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import _probe_models_detailed

        patch_catalog_client(
            monkeypatch,
            lambda request: httpx.Response(
                200,
                json={"data": [{"id": "model-a"}, {"id": "model-b"}]},
                request=request,
            ),
        )

        result = _probe_models_detailed("https://gateway.example/v1", "sk-test")

        assert result.models == ("model-a", "model-b")
        assert result.error is None

    def test_openai_probe_merges_non_secret_client_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import _probe_models_detailed

        requests = patch_catalog_client(
            monkeypatch,
            lambda request: httpx.Response(
                200,
                json={"data": [{"id": "model-a"}]},
                request=request,
            ),
        )

        result = _probe_models_detailed(
            "https://gateway.example/v1",
            "sk-test",
            extra_headers={"x-goog-api-client": "ash-test-oai/1.0"},
        )

        assert result.models == ("model-a",)
        request, _ = requests[0]
        assert request.headers["authorization"] == "Bearer sk-test"
        assert request.headers["x-goog-api-client"] == "ash-test-oai/1.0"

    def test_anthropic_probe_uses_shared_catalog_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import _probe_anthropic_models_detailed

        calls: list[tuple[str, dict[str, str], str, int]] = []

        def shared_probe(
            endpoint: str,
            *,
            headers: dict[str, str],
            catalog_format: str,
            timeout: int,
        ) -> tuple[str, ...]:
            calls.append((endpoint, headers, catalog_format, timeout))
            return ("claude-a", "claude-b")

        monkeypatch.setattr(
            "ash.providers.readiness.probe_model_catalog", shared_probe
        )

        result = _probe_anthropic_models_detailed(
            "anthropic-secret", "https://gateway.example/v1"
        )

        assert result.models == ("claude-a", "claude-b")
        assert result.error is None
        assert calls == [
            (
                "https://gateway.example/v1/models",
                {
                    "x-api-key": "anthropic-secret",
                    "anthropic-version": "2023-06-01",
                },
                "anthropic",
                10,
            )
        ]

    def test_anthropic_default_probe_uses_v1_models_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import _probe_anthropic_models_detailed

        endpoints: list[str] = []

        def shared_probe(endpoint: str, **_: object) -> tuple[str, ...]:
            endpoints.append(endpoint)
            return ("claude-a",)

        monkeypatch.setattr(
            "ash.providers.readiness.probe_model_catalog", shared_probe
        )

        result = _probe_anthropic_models_detailed("anthropic-secret")

        assert result.models == ("claude-a",)
        assert endpoints == ["https://api.anthropic.com/v1/models"]

    def test_probe_models_http_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_probe_models returns [] on HTTP error."""
        monkeypatch.setenv("HOME", "/tmp")

        def fail_client(*, timeout: float) -> object:
            raise RuntimeError("network error")

        monkeypatch.setattr("ash.providers.readiness.httpx.Client", fail_client)
        from ash.commands.setup import _probe_models

        result = _probe_models("https://api.openai.com/v1", "sk-test")
        assert result == []

    def test_probe_error_redacts_echoed_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import _probe_models_detailed

        patch_catalog_client(
            monkeypatch,
            lambda request: httpx.Response(
                401,
                text="invalid key sk-secret-value",
                request=request,
            ),
        )
        result = _probe_models_detailed(
            "https://api.example.test/v1",
            "sk-secret-value",
        )

        assert result.models == ()
        assert "sk-secret-value" not in (result.error or "")
        assert "[REDACTED]" in (result.error or "")

    def test_probe_ollama_models_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_probe_ollama_models returns model names on 200 response."""
        patch_catalog_client(
            monkeypatch,
            lambda request: httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "llama3"},
                        {"name": "qwen2.5-coder:7b"},
                    ],
                },
                request=request,
            ),
        )
        monkeypatch.setenv("HOME", "/tmp")
        from ash.commands.setup import _probe_ollama_models

        result = _probe_ollama_models("http://localhost:11434")
        assert result == ["llama3", "qwen2.5-coder:7b"]

    def test_ollama_probe_uses_shared_catalog_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import _probe_ollama_models_detailed

        calls: list[tuple[str, dict[str, str], str, int]] = []

        def shared_probe(
            endpoint: str,
            *,
            headers: dict[str, str],
            catalog_format: str,
            timeout: int,
        ) -> tuple[str, ...]:
            calls.append((endpoint, headers, catalog_format, timeout))
            return ("llama3.2:latest", "qwen3:latest")

        monkeypatch.setattr(
            "ash.providers.readiness.probe_model_catalog", shared_probe
        )

        result = _probe_ollama_models_detailed("http://localhost:11434")

        assert result.models == ("llama3.2:latest", "qwen3:latest")
        assert result.error is None
        assert calls == [
            (
                "http://localhost:11434/api/tags",
                {},
                "ollama",
                10,
            )
        ]


class TestSetupValidation:
    @pytest.mark.parametrize(
        "value",
        [
            "localhost:11434",
            "ftp://example.com",
            "https://user:secret@example.com/v1",
            "https://example.com/v1?token=secret",
            "https://example.com:not-a-port/v1",
            "https://example.com:99999/v1",
            "https://example.com:0/v1",
        ],
    )
    def test_base_url_rejects_unsafe_or_ambiguous_values(self, value: str) -> None:
        from ash.commands.setup import _validate_base_url

        with pytest.raises(ValueError):
            _validate_base_url(value)

    def test_base_url_normalizes_trailing_slash(self) -> None:
        from ash.commands.setup import _validate_base_url

        assert _validate_base_url("http://localhost:11434/") == "http://localhost:11434"


class TestCmdSetup:
    """Tests for cmd_setup entry point."""

    def test_cmd_setup_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cmd_setup should return 0 on success."""
        monkeypatch.setenv("HOME", "/tmp")
        monkeypatch.setenv("TERM", "xterm")  # ensure isatty true-ish

        mock_args = MagicMock(
            section="model",
            quick=False,
            non_interactive=False,
        )

        with patch("ash.commands.setup.is_interactive_stdin", return_value=True):
            from ash.commands.setup import SetupOutcome, cmd_setup

            with patch(
                "ash.commands.setup.run_setup_wizard",
                return_value=SetupOutcome.SUCCESS,
            ):
                result = cmd_setup(mock_args)
                assert result == 0

    def test_cmd_setup_non_interactive_returns_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import cmd_setup

        args = MagicMock(section="model", quick=False, non_interactive=True)
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: False)

        with patch("ash.commands.setup._has_provider_configured", return_value=False):
            assert cmd_setup(args) == 2
        assert "requires an interactive terminal" in capsys.readouterr().err

    def test_cmd_setup_non_interactive_accepts_existing_configuration(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import cmd_setup

        args = MagicMock(section="model", quick=False, non_interactive=True)
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: False)

        with patch("ash.commands.setup._has_provider_configured", return_value=True):
            assert cmd_setup(args) == 0
        output = capsys.readouterr().out
        assert "Ash is configured for" in output
        assert "doctor --connect" in output
        assert "providers test" in output
        assert "does not make a billable model completion request" in output

    def test_status_json_is_secret_free_and_reports_capabilities(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import cmd_setup

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: False)
        monkeypatch.setattr("ash.commands.setup._browser_is_installed", lambda: False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-status-secret")
        config = SimpleNamespace(
            model="openai/gpt-status",
            fallback_models=["ollama/local"],
            custom_providers={},
            web_search_provider="auto",
            memory_backend="sqlite",
            sandbox_backend="auto",
            observability_enabled=False,
            observability_sample_rate=1.0,
            workspace_root=tmp_path,
        )
        monkeypatch.setattr("ash.config.AshConfig.load", lambda: config)

        args = SimpleNamespace(
            section="status",
            quick=False,
            non_interactive=True,
            json=True,
        )
        assert cmd_setup(args) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["profile"] == "default"
        assert payload["provider"]["id"] == "openai"
        assert payload["provider"]["ready"] is True
        assert payload["fallback_models"] == ["ollama/local"]
        assert payload["capabilities"]["memory"]["backend"] == "sqlite"
        assert payload["capabilities"]["observability"]["enabled"] is False
        assert payload["capabilities"]["observability"]["content_capture"] is False
        assert "sk-status-secret" not in json.dumps(payload)

    def test_status_human_output_sanitizes_untrusted_model_identifier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import _render_setup_status

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr("ash.commands.setup._browser_is_installed", lambda: False)
        monkeypatch.setattr("ash.commands.setup._has_provider_configured", lambda _: False)
        config = SimpleNamespace(
            model="openai/model\nname\u202ehidden\u202c",
            fallback_models=[],
            custom_providers={},
            web_search_provider="auto",
            memory_backend="sqlite",
            sandbox_backend="auto",
            observability_enabled=False,
            observability_sample_rate=1.0,
            workspace_root=tmp_path,
        )

        _render_setup_status(config)

        rendered = capsys.readouterr().out
        assert "model\\x0aname\\u202ehidden\\u202c" in rendered
        assert "model\nname" not in rendered
        assert "\u202e" not in rendered


class TestWebSearchSetup:
    def test_saves_hidden_brave_credential_and_provider_selection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.setattr("builtins.input", _fake_input(["1"]))
        monkeypatch.setattr(
            "ash.commands.setup.getpass.getpass", _FakeGetpass("brave-search-test-key")
        )

        from ash.commands.setup import SetupOutcome, setup_web_search

        with patch("ash.commands.setup.save_env_values") as save:
            result = setup_web_search()

        assert result == SetupOutcome.SUCCESS
        assert save.call_args.args[0] == {
            "BRAVE_SEARCH_API_KEY": "brave-search-test-key",
            "ASH_WEB_SEARCH_PROVIDER": "brave",
        }

    def test_noninteractive_web_setup_requires_search_credential(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        from ash.commands.setup import cmd_setup

        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: False)
        args = MagicMock(section="web", quick=False, non_interactive=True)

        assert cmd_setup(args) == 2
        assert "BRAVE_SEARCH_API_KEY" in capsys.readouterr().err


class TestBrowserSetup:
    def test_reports_missing_optional_dependency(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        from ash.commands.setup import SetupOutcome, setup_browser

        monkeypatch.setattr(
            "ash.commands.setup.importlib.util.find_spec", lambda name: None
        )

        assert setup_browser() == SetupOutcome.ERROR
        error = capsys.readouterr().err
        assert " -I -c " in error
        assert "api.github.com/repos/Suraj-H675/Ash-Harness/releases/latest" in error
        assert "--extra browser" in error
        assert "curl" not in error
        assert "pipx install" not in error

    def test_existing_browser_never_runs_installer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands.setup import SetupOutcome, setup_browser

        monkeypatch.setattr("ash.commands.setup._browser_is_installed", lambda: True)
        run = MagicMock(side_effect=AssertionError("installer unexpectedly ran"))
        monkeypatch.setattr("ash.commands.setup.run_browser_subprocess", run)

        assert setup_browser() == SetupOutcome.SUCCESS
        run.assert_not_called()

    def test_installs_pinned_chromium_after_confirmation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands.setup import SetupOutcome, setup_browser

        states = iter((False, True))
        monkeypatch.setattr(
            "ash.commands.setup._browser_is_installed", lambda: next(states)
        )
        monkeypatch.setattr("builtins.input", _fake_input([""]))
        completed = MagicMock(returncode=0)
        monkeypatch.setattr(
            "ash.commands.setup.run_browser_subprocess", MagicMock(return_value=completed)
        )

        assert setup_browser() == SetupOutcome.SUCCESS
        from ash.commands import setup

        setup.run_browser_subprocess.assert_called_once_with(
            [
                setup.sys.executable,
                "-I",
                "-m",
                "playwright",
                "install",
                "chromium",
            ],
            check=False,
            timeout=setup.BROWSER_INSTALL_TIMEOUT_SECONDS,
        )

    def test_browser_install_timeout_fails_cleanly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ash.commands.setup import SetupOutcome, setup_browser

        states = iter((False,))
        monkeypatch.setattr(
            "ash.commands.setup._browser_is_installed", lambda: next(states)
        )
        monkeypatch.setattr("builtins.input", _fake_input([""]))
        monkeypatch.setattr(
            "ash.commands.setup.run_browser_subprocess",
            MagicMock(side_effect=subprocess.TimeoutExpired("playwright", 300)),
        )

        assert setup_browser() == SetupOutcome.ERROR
        assert "timed out" in capsys.readouterr().err


class TestSetupNavigation:
    @pytest.mark.parametrize(
        ("section", "entrypoint"),
        [
            ("model", "setup_model_provider"),
            ("fallbacks", "setup_providers"),
            ("providers", "setup_providers"),
        ],
    )
    def test_setup_sections_dispatch_to_their_distinct_entrypoints(
        self, monkeypatch: pytest.MonkeyPatch
        , section: str, entrypoint: str
    ) -> None:
        from ash.commands.setup import SetupOutcome, run_setup_wizard

        config = MagicMock(model="openai/test-model")
        args = SimpleNamespace(section=section, quick=False, non_interactive=False)
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: True)

        with (
            patch("ash.config.AshConfig.load", return_value=config),
            patch("ash.commands.setup._migrate_old_ash_toml"),
            patch(
                "ash.commands.setup.setup_model_provider",
                return_value=SetupOutcome.SUCCESS,
            ) as setup_model,
            patch(
                "ash.commands.setup.setup_providers",
                return_value=SetupOutcome.SUCCESS,
            ) as setup_providers,
            patch("ash.commands.setup._print_header"),
            patch("ash.commands.setup._print_info"),
        ):
            result = run_setup_wizard(args)

        assert result == SetupOutcome.SUCCESS
        if entrypoint == "setup_model_provider":
            setup_model.assert_called_once_with(config, quick=False)
            setup_providers.assert_not_called()
        else:
            setup_providers.assert_called_once_with(config, quick=False)
            setup_model.assert_not_called()

    def test_setup_providers_displays_ordered_chain_without_saving(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import SetupOutcome, setup_providers

        config = SimpleNamespace(
            model="openai/primary",
            fallback_models=["anthropic/backup", "ollama/local"],
        )
        monkeypatch.setattr("builtins.input", _fake_input(["6"]))
        with patch("ash.commands.setup.mutate_config") as mutate_config:
            result = setup_providers(config)

        assert result == SetupOutcome.SUCCESS
        output = capsys.readouterr().out
        assert "Model Fallbacks" in output
        assert "Primary: openai/primary" in output
        assert "1. anthropic/backup" in output
        assert "2. ollama/local" in output
        mutate_config.assert_not_called()
        assert config.fallback_models == ["anthropic/backup", "ollama/local"]

    def test_setup_providers_displays_empty_fallback_state(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import SetupOutcome, setup_providers

        config = SimpleNamespace(model="openai/primary", fallback_models=[])
        monkeypatch.setattr("builtins.input", _fake_input(["6"]))

        assert setup_providers(config) == SetupOutcome.SUCCESS
        assert "No fallback models configured" in capsys.readouterr().out

    def test_setup_providers_routes_add_fallback_selection(self) -> None:
        from ash.commands.setup import (
            ProviderManagementAction,
            _choose_provider_management_action,
        )

        with patch("ash.commands.setup._prompt_choice", return_value=0):
            action = _choose_provider_management_action()

        assert action is ProviderManagementAction.ADD

    def test_setup_providers_dispatches_add_fallback_route(self) -> None:
        from ash.commands.setup import (
            ProviderManagementAction,
            SetupOutcome,
            setup_providers,
        )

        config = SimpleNamespace(model="openai/primary", fallback_models=[])
        with (
            patch(
                "ash.commands.setup._choose_provider_management_action",
                side_effect=[
                    ProviderManagementAction.ADD,
                    ProviderManagementAction.DONE,
                ],
            ) as choose_action,
            patch(
                "ash.commands.setup._handle_add_fallback",
                return_value=SetupOutcome.SUCCESS,
            ) as add_fallback,
        ):
            outcome = setup_providers(config)

        assert choose_action.call_count == 2
        add_fallback.assert_called_once_with(config)
        assert outcome is SetupOutcome.SUCCESS

    def test_setup_providers_adds_multiple_fallbacks_and_persists_each_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import SetupOutcome, setup_providers

        config = SimpleNamespace(model="openai/primary", fallback_models=[])
        monkeypatch.setattr(
            "builtins.input",
            _fake_input(["1", "anthropic/backup", "1", "ollama/local", "6"]),
        )
        saved: list[dict[str, object]] = []
        current: dict[str, object] = {"other": "preserved"}

        def apply_mutation(mutator):
            mutator(current)
            saved.append(json.loads(json.dumps(current)))
            return dict(current)

        with patch("ash.commands.setup.mutate_config", side_effect=apply_mutation):
            outcome = setup_providers(config)

        assert outcome is SetupOutcome.SUCCESS
        assert config.fallback_models == ["anthropic/backup", "ollama/local"]
        assert saved == [
            {
                "other": "preserved",
                "fallback_models": ["anthropic/backup"],
            },
            {
                "other": "preserved",
                "fallback_models": ["anthropic/backup", "ollama/local"],
            },
        ]

    @pytest.mark.parametrize("section", ["model", "providers"])
    def test_noninteractive_configured_route_accepts_provider_sections(
        self,
        section: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        from ash.commands.setup import SetupOutcome, cmd_setup

        args = SimpleNamespace(section=section, quick=False, non_interactive=True)
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: False)
        config = SimpleNamespace(model="openai/test-model")

        with (
            patch("ash.config.AshConfig.load", return_value=config),
            patch("ash.commands.setup._has_provider_configured", return_value=True),
        ):
            assert cmd_setup(args) == int(SetupOutcome.SUCCESS)

        output = capsys.readouterr().out
        assert "Ash is configured for openai/test-model." in output
        assert "doctor --connect" in output

    @pytest.mark.parametrize("section", ["model", "providers"])
    def test_noninteractive_missing_route_rejects_provider_sections(
        self,
        section: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        from ash.commands.setup import SetupOutcome, cmd_setup

        args = SimpleNamespace(section=section, quick=False, non_interactive=True)
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: False)
        config = SimpleNamespace(model="")

        with (
            patch("ash.config.AshConfig.load", return_value=config),
            patch("ash.commands.setup._has_provider_configured", return_value=False),
        ):
            assert cmd_setup(args) == int(SetupOutcome.ERROR)

        output = capsys.readouterr()
        assert "Setup complete!" not in output.out
        assert "requires an interactive terminal" in output.err

    def test_quick_reuses_existing_route_without_provider_prompt(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import SetupOutcome, setup_model_provider

        monkeypatch.setenv("OPENAI_API_KEY", "sk-quick-test")
        config = SimpleNamespace(model="openai/test-model", custom_providers={})

        with patch(
            "ash.commands.setup.select_provider_and_model",
            side_effect=AssertionError("QuickStart should reuse the existing route"),
        ):
            result = setup_model_provider(config, quick=True)

        assert result == SetupOutcome.SUCCESS
        output = capsys.readouterr().out
        assert "reused" in output.lower()
        assert "doctor --connect" in output
        assert "providers test" in output
        assert "sk-quick-test" not in output

    def test_quick_partial_route_enters_provider_flow_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import SetupOutcome, setup_model_provider

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = SimpleNamespace(model="openai/test-model", custom_providers={})
        with patch(
            "ash.commands.setup.select_provider_and_model",
            return_value=SetupOutcome.SUCCESS,
        ) as select:
            result = setup_model_provider(config, quick=True)

        assert result == SetupOutcome.SUCCESS
        select.assert_called_once_with(config)

    def test_quick_all_skips_optional_sections_explicitly(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import SetupOutcome, run_setup_wizard

        config = SimpleNamespace(model="openai/test-model", custom_providers={})
        args = SimpleNamespace(section="all", quick=True, non_interactive=False)
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: True)

        with (
            patch("ash.config.AshConfig.load", return_value=config),
            patch("ash.commands.setup._migrate_old_ash_toml"),
            patch(
                "ash.commands.setup.setup_model_provider",
                return_value=SetupOutcome.SUCCESS,
            ),
            patch(
                "ash.commands.setup._has_provider_configured",
                return_value=True,
            ),
            patch("ash.commands.setup.setup_web_search") as web_setup,
            patch("ash.commands.setup._print_header"),
        ):
            result = run_setup_wizard(args)

        assert result == SetupOutcome.SUCCESS
        web_setup.assert_not_called()
        output = capsys.readouterr().out
        assert "QuickStart skipped optional web search and browser setup." in output
        assert "Setup saved." in output
        assert "providers test" in output

    def test_cancelled_quick_setup_does_not_print_complete(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import SetupOutcome, run_setup_wizard

        config = SimpleNamespace(model="")
        args = SimpleNamespace(section="all", quick=True, non_interactive=False)
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: True)

        with (
            patch("ash.config.AshConfig.load", return_value=config),
            patch("ash.commands.setup._migrate_old_ash_toml"),
            patch(
                "ash.commands.setup.setup_model_provider",
                return_value=SetupOutcome.CANCELLED,
            ),
            patch("ash.commands.setup._print_header"),
        ):
            result = run_setup_wizard(args)

        assert result == SetupOutcome.CANCELLED
        assert "Setup complete!" not in capsys.readouterr().out

    def test_invalid_provider_choice_retries_without_dispatch(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from ash.commands.setup import SetupOutcome, select_provider_and_model

        monkeypatch.setattr("builtins.input", _fake_input(["1", "invalid", "c"]))
        with patch("ash.commands.setup._flow_openai") as flow:
            result = select_provider_and_model(SimpleNamespace(model=""))

        assert result == SetupOutcome.CANCELLED
        flow.assert_not_called()
        assert "Invalid choice." in capsys.readouterr().out

    def test_provider_selection_can_cancel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import SetupOutcome, select_provider_and_model

        monkeypatch.setattr("builtins.input", _fake_input(["c"]))

        assert select_provider_and_model(MagicMock(model="")) == SetupOutcome.CANCELLED

    def test_blank_api_key_returns_to_provider_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ash.commands.setup import SetupOutcome, select_provider_and_model

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr("builtins.input", _fake_input(["1", "1", "c"]))
        monkeypatch.setattr("ash.commands.setup.getpass.getpass", _FakeGetpass(""))

        assert select_provider_and_model(MagicMock(model="")) == SetupOutcome.CANCELLED


class TestLegacyConfigMigration:
    @pytest.fixture(autouse=True)
    def _restore_config_paths(self) -> None:
        from ash.commands import config as cli_config

        original = (
            cli_config.ASH_DIR,
            cli_config.ENV_FILE,
            cli_config.CONFIG_FILE,
        )
        try:
            yield
        finally:
            (
                cli_config.ASH_DIR,
                cli_config.ENV_FILE,
                cli_config.CONFIG_FILE,
            ) = original

    @pytest.fixture(autouse=True)
    def _trusted_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ash.commands.setup.is_workspace_trusted",
            lambda workspace: True,
        )

    @pytest.fixture(autouse=True)
    def _clear_destination_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (
            "ASH_MODEL",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "GROQ_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)

    @staticmethod
    def _configure_paths(tmp_path: Path) -> None:
        from ash.commands import config as cli_config

        cli_config.ASH_DIR = tmp_path / "home" / ".ash"
        cli_config.ENV_FILE = cli_config.ASH_DIR / ".env"
        cli_config.CONFIG_FILE = cli_config.ASH_DIR / "ash.toml"

    def test_migrates_complete_historical_config_and_records_backup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        legacy.write_text(
            "\n".join(
                [
                    'provider = "openai"',
                    'model_name = "gpt-test"',
                    'api_key = "legacy-secret"',
                    "temperature = 0.3",
                    "max_context_tokens = 64000",
                    "max_completion_tokens = 2048",
                    "max_tool_result_tokens = 9000",
                    'safety_tier = "dry_run"',
                    'workspace_root = "."',
                    'command_blocklist = ["danger"]',
                    'db_directory = ".ash-db"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input([""]))

        _migrate_old_ash_toml()

        env = cli_config.load_env()
        assert "OPENAI_API_KEY" not in env
        assert env["ASH_MODEL"] == "openai/gpt-test"
        assert "ANTHROPIC_API_KEY" not in env
        user = cli_config.load_config(strict=True)
        assert user["config_schema_version"] == 2
        assert user["temperature"] == 0.3
        assert user["max_context_tokens"] == 64000
        assert user["max_completion_tokens"] == 2048
        assert user["max_tool_result_tokens"] == 9000
        assert "safety_tier" not in user
        assert "workspace_root" not in user
        assert "db_directory" not in user
        assert "command_blocklist" not in user
        backups = list((cli_config.ASH_DIR / "backups").glob("legacy-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == legacy.read_bytes()
        assert cli_config.is_config_migration_recorded(legacy) is True
        output = capsys.readouterr().out
        assert "api_key" in output
        assert "safety_tier" in output
        assert "db_directory" in output
        assert "legacy-secret" not in output

        repeated_prompt = MagicMock(side_effect=AssertionError("prompted twice"))
        monkeypatch.setattr("builtins.input", repeated_prompt)
        _migrate_old_ash_toml()
        repeated_prompt.assert_not_called()

    def test_preserves_existing_destinations_and_backs_up_user_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        (project / "ash.toml").write_text(
            'provider = "anthropic"\n'
            'model_name = "legacy-model"\n'
            'api_key = "legacy-key"\n'
            "temperature = 0.8\n"
            "max_context_tokens = 32000\n",
            encoding="utf-8",
        )
        cli_config.save_config({"temperature": 0.1})
        cli_config.save_env_values(
            {
                "ANTHROPIC_API_KEY": "new-key",
                "ASH_MODEL": "anthropic/new-model",
            }
        )
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))

        _migrate_old_ash_toml()

        assert cli_config.load_env()["ANTHROPIC_API_KEY"] == "new-key"
        assert cli_config.load_env()["ASH_MODEL"] == "anthropic/new-model"
        user = cli_config.load_config(strict=True)
        assert user["temperature"] == 0.1
        assert user["max_context_tokens"] == 32000
        destination_backups = list(
            (cli_config.ASH_DIR / "backups").glob("user-ash.toml-pre-migration.*.bak")
        )
        assert len(destination_backups) == 1
        assert "temperature = 0.1" in destination_backups[0].read_text()
        output = capsys.readouterr().out
        assert "ANTHROPIC_API_KEY" in output
        assert "ASH_MODEL" in output
        assert "temperature" in output

    def test_ignores_unrelated_toml_and_placeholder_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        legacy.write_text('name = "another-tool"\n', encoding="utf-8")
        monkeypatch.chdir(project)
        prompt = MagicMock(side_effect=AssertionError("unexpected prompt"))
        monkeypatch.setattr("builtins.input", prompt)

        _migrate_old_ash_toml()
        prompt.assert_not_called()
        assert not cli_config.ENV_FILE.exists()

        legacy.write_text(
            'provider = "anthropic"\n'
            'model_name = "test"\n'
            'api_key = "replace-with-your-api-key"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))
        _migrate_old_ash_toml()
        assert "ANTHROPIC_API_KEY" not in cli_config.load_env()

    def test_untrusted_workspace_is_not_offered_migration(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        (project / "ash.toml").write_text(
            'model_name = "legacy"\nsafety_tier = "auto_approve"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(project)
        prompt = MagicMock(side_effect=AssertionError("migration was offered"))
        monkeypatch.setattr("builtins.input", prompt)
        monkeypatch.setattr("ash.commands.setup.is_workspace_trusted", lambda _: False)

        _migrate_old_ash_toml()

        prompt.assert_not_called()
        assert not cli_config.CONFIG_FILE.exists()
        assert not cli_config.ENV_FILE.exists()
        assert "not trusted" in capsys.readouterr().out

    def test_trusted_workspace_still_excludes_sensitive_legacy_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        (project / "ash.toml").write_text(
            'model_name = "legacy"\n'
            'provider = "custom"\n'
            "temperature = 0.25\n"
            'safety_tier = "auto_approve"\n'
            'sandbox_backend = "direct"\n'
            'db_directory = "outside"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))

        _migrate_old_ash_toml()

        user = cli_config.load_config(strict=True)
        assert user["temperature"] == 0.25
        assert "safety_tier" not in user
        assert "sandbox_backend" not in user
        assert "db_directory" not in user
        assert "ASH_MODEL" not in cli_config.load_env()
        output = capsys.readouterr().out
        assert "model_name" in output
        assert "provider" in output
        assert "safety_tier" in output
        assert "sandbox_backend" in output
        assert "db_directory" in output

    def test_refuses_to_overwrite_malformed_user_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        legacy.write_text('model_name = "legacy"\n', encoding="utf-8")
        cli_config.ensure_ash_dir()
        cli_config.CONFIG_FILE.write_text("invalid = [", encoding="utf-8")
        original = cli_config.CONFIG_FILE.read_bytes()
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))

        with pytest.raises(Exception):
            _migrate_old_ash_toml()

        assert cli_config.CONFIG_FILE.read_bytes() == original
        assert cli_config.is_config_migration_recorded(legacy) is False

    def test_refuses_invalid_supported_values_before_persisting_migration(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        legacy.write_text(
            'provider = "openai"\n'
            'model_name = "gpt-test"\n'
            "max_context_tokens = -1\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))

        with pytest.raises(ValueError, match="max_context_tokens"):
            _migrate_old_ash_toml()

        assert not cli_config.CONFIG_FILE.exists()
        assert not cli_config.ENV_FILE.exists()
        assert cli_config.is_config_migration_recorded(legacy) is False

    def test_refuses_invalid_toml_model_even_with_valid_legacy_model_name(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        legacy.write_text(
            'model = "/invalid"\n'
            'provider = "anthropic"\n'
            'model_name = "valid-legacy-model"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))

        with pytest.raises(ValueError, match="legacy configuration migration"):
            _migrate_old_ash_toml()

        assert not cli_config.CONFIG_FILE.exists()
        assert not cli_config.ENV_FILE.exists()
        assert cli_config.is_config_migration_recorded(legacy) is False

    def test_setup_uses_migrated_config_for_followup_provider_decisions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands.setup import SetupOutcome, run_setup_wizard

        self._configure_paths(tmp_path)
        home = tmp_path / "home"
        project = tmp_path / "project"
        project.mkdir()
        project.joinpath("ash.toml").write_text(
            'provider = "anthropic"\nmodel_name = "legacy-model"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(project)
        monkeypatch.setattr("ash.commands.setup.is_interactive_stdin", lambda: True)
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))
        observed_models: list[str] = []

        def record_model(config, *, quick=False):
            del quick
            observed_models.append(str(config.model))
            return SetupOutcome.SUCCESS

        monkeypatch.setattr("ash.commands.setup.setup_model_provider", record_model)
        args = SimpleNamespace(section="model", quick=False, non_interactive=False)

        assert run_setup_wizard(args) is SetupOutcome.SUCCESS
        assert observed_models == ["anthropic/legacy-model"]

    def test_refuses_source_changed_while_migration_prompt_is_open(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands.setup import _migrate_old_ash_toml

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        legacy.write_text(
            'provider = "anthropic"\nmodel_name = "snapshot-a"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(project)

        def change_source(_prompt: str) -> str:
            legacy.write_text(
                'provider = "anthropic"\nmodel_name = "snapshot-b"\n',
                encoding="utf-8",
            )
            return "y"

        monkeypatch.setattr("builtins.input", change_source)

        with pytest.raises(ValueError, match="changed while migration was pending"):
            _migrate_old_ash_toml()

        assert not cli_config.CONFIG_FILE.exists()
        assert not cli_config.ENV_FILE.exists()
        assert cli_config.is_config_migration_recorded(legacy) is False

    def test_source_changed_after_backup_does_not_leave_partial_migration_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ash.commands import config as cli_config
        from ash.commands import setup as setup_module

        self._configure_paths(tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        legacy = project / "ash.toml"
        snapshot_a = (
            'provider = "anthropic"\n'
            'model_name = "snapshot-a"\n'
            "temperature = 0.2\n"
        )
        snapshot_b = (
            'provider = "anthropic"\n'
            'model_name = "snapshot-b"\n'
            "temperature = 0.9\n"
        )
        legacy.write_text(snapshot_a, encoding="utf-8")
        monkeypatch.chdir(project)
        monkeypatch.setattr("builtins.input", _fake_input(["y"]))
        real_replace = setup_module.replace_config_if_current

        def replace_then_change(expected, updated):
            real_replace(expected, updated)
            legacy.write_text(snapshot_b, encoding="utf-8")

        monkeypatch.setattr(
            setup_module,
            "replace_config_if_current",
            replace_then_change,
        )

        setup_module._migrate_old_ash_toml()

        assert cli_config.load_config(strict=True)["temperature"] == 0.2
        assert cli_config.load_env()["ASH_MODEL"] == "anthropic/snapshot-a"
        backups = list((cli_config.ASH_DIR / "backups").glob("legacy-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == snapshot_a
        assert legacy.read_text(encoding="utf-8") == snapshot_b
        assert cli_config.is_config_migration_recorded(legacy) is False



def test_lmstudio_probe_uses_native_model_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import _probe_models_detailed

    calls: list[tuple[str, dict[str, str], str, int]] = []

    def shared_probe(
        endpoint: str,
        *,
        headers: dict[str, str],
        catalog_format: str,
        timeout: int,
    ) -> tuple[str, ...]:
        calls.append((endpoint, headers, catalog_format, timeout))
        return ("local-agent",)

    monkeypatch.setattr("ash.providers.readiness.probe_model_catalog", shared_probe)

    result = _probe_models_detailed(
        "http://localhost:1234/v1",
        None,
        catalog_format="lmstudio",
    )

    assert result.models == ("local-agent",)
    assert result.error is None
    assert calls == [
        ("http://localhost:1234/api/v1/models", {}, "lmstudio", 10)
    ]
