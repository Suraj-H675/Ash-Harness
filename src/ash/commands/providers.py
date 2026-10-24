"""Provider catalog and connectivity commands."""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import dataclass, replace
from io import StringIO
from typing import TYPE_CHECKING, Any, Literal
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from ash.core.redaction import redact_text
from ash.provider_catalog import BUILTIN_PROVIDERS
from ash.providers.base import (
    CanonicalToolCall,
    CompletionStopCategory,
    completion_stop_category,
)
from ash.providers.readiness import (
    ProviderConfigurationError,
    ProviderVerification,
    ProviderVerificationError,
    resolve_provider_connection,
    verify_provider_connection,
)
from ash.providers.openai_chatgpt_verification import verify_chatgpt_plan_connection
from ash.ui.safe_text import terminal_safe_text

if TYPE_CHECKING:
    from ash.config import AshConfig


PROVIDER_TEST_MAX_TOKENS = 8
PROVIDER_NATIVE_TOOL_TEST_MAX_TOKENS = 128
MAX_PROVIDER_TEST_RESPONSE_CHARS = 4096
_PROVIDER_TEST_PROMPT = "Reply with exactly OK."
_NATIVE_PROBE_TOOL_NAME = "ash_diagnostic_echo"
_NATIVE_PROBE_VALUE = "ASH_TOOL_OK"
_NATIVE_PROBE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": _NATIVE_PROBE_TOOL_NAME,
            "description": "A harmless diagnostic echo. No action will be executed.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }
]

NativeToolFailureKind = Literal[
    "timeout",
    "output_truncated",
    "no_native_tool_call",
    "invalid_native_tool_call",
    "malformed_stream",
    "provider_error",
    "cleanup_error",
]


@dataclass(frozen=True)
class NativeToolProbeResult:
    verified: bool
    error: str | None = None
    failure_kind: NativeToolFailureKind | None = None


_NATIVE_TOOL_FAILURE_GUIDANCE: dict[str, str] = {
    "timeout": "The model did not finish within the probe timeout; retry with a larger --timeout if needed.",
    "output_truncated": "The response hit its output limit; inspect model/server token limits.",
    "no_native_tool_call": "The model returned no structured tool call; check its tool support and server chat template.",
    "invalid_native_tool_call": "The structured tool call did not match the requested name and arguments.",
    "malformed_stream": "The response stream was incomplete or invalid; inspect the local runtime logs.",
    "provider_error": "The model/server request failed; inspect the runtime logs and endpoint configuration.",
    "cleanup_error": "Provider resource cleanup failed after the probe; inspect the runtime logs.",
}


async def verify_provider_catalog(
    config: "AshConfig",
    *,
    timeout: float = 10.0,
) -> ProviderVerification:
    """Verify the configured provider's live model catalog without a completion."""

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("provider catalog timeout must be positive and finite")
    probe_config = config.model_copy(update={"fallback_models": []})
    from ash.providers.identifiers import parse_model_string

    selected_provider, _selected_model = parse_model_string(probe_config.model)
    if selected_provider == "bedrock":
        from ash.providers.bedrock import discover_bedrock_models

        connection = resolve_provider_connection(probe_config)
        region = (
            str(getattr(probe_config, "bedrock_region", "") or "").strip()
            or os.environ.get("AWS_REGION", "").strip()
            or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        )
        profile = (
            str(getattr(probe_config, "bedrock_profile", "") or "").strip()
            or os.environ.get("AWS_PROFILE", "").strip()
        )
        models = await asyncio.to_thread(
            discover_bedrock_models,
            region=region,
            profile=profile,
        )
        return ProviderVerification(
            connection=connection,
            models=models,
            selected_model_available=connection.model_name in models,
            catalog_authoritative=False,
        )
    if selected_provider == "vertex":
        raise ProviderVerificationError(
            "Vertex AI does not expose a trustworthy OpenAI-compatible live model "
            "catalog for this route; enter the Vertex model ID explicitly."
        )
    if (
        selected_provider == "openai"
        and getattr(probe_config, "openai_auth_mode", "api_key") == "chatgpt"
    ):
        return await verify_chatgpt_plan_connection(probe_config, timeout=timeout)
    return await asyncio.to_thread(
        verify_provider_connection,
        probe_config,
        timeout=timeout,
    )


def provider_catalog_payload() -> dict[str, Any]:
    """Return the safe, declarative provider catalog for scripts and UIs."""

    return {
        "providers": [
            {
                "id": descriptor.id,
                "name": descriptor.name,
                "category": descriptor.category,
                "description": descriptor.description,
                "protocol": descriptor.protocol,
                "base_url": descriptor.base_url,
                "auth": (
                    "none"
                    if descriptor.local
                    else "google-adc"
                    if descriptor.id == "vertex"
                    else "aws-sigv4"
                    if descriptor.id == "bedrock"
                    else "azure-key-or-entra"
                    if descriptor.id == "azure"
                    else "api-key-or-chatgpt"
                    if descriptor.id == "openai"
                    else "api-key"
                ),
                "key_env": descriptor.key_env,
                "key_envs": list(descriptor.key_envs),
                "local": descriptor.local,
            }
            for descriptor in BUILTIN_PROVIDERS
        ]
    }


def render_provider_catalog(*, json_output: bool = False) -> str:
    """Render provider choices without making network requests or loading keys."""

    payload = provider_catalog_payload()
    if json_output:
        return json.dumps(payload, indent=2, sort_keys=True)

    buffer = StringIO()
    console = Console(
        file=buffer,
        no_color=bool(os.environ.get("NO_COLOR") or os.environ.get("ASH_NO_COLOR")),
        soft_wrap=True,
    )
    table = Table(title="Ash provider catalog", header_style="bold cyan")
    table.add_column("Provider", style="bold")
    table.add_column("Route")
    table.add_column("Protocol")
    table.add_column("Authentication", overflow="fold")
    table.add_column("Default endpoint")
    for descriptor in BUILTIN_PROVIDERS:
        table.add_row(
            descriptor.id,
            descriptor.category,
            descriptor.protocol,
            (
                "none"
                if descriptor.local
                else "Google ADC"
                if descriptor.id == "vertex"
                else "AWS SigV4"
                if descriptor.id == "bedrock"
                else "API key / Entra"
                if descriptor.id == "azure"
                else "API key / ChatGPT plan"
                if descriptor.id == "openai"
                else " / ".join(descriptor.key_envs) or "api key"
            ),
            descriptor.base_url,
        )
    console.print(table)
    return buffer.getvalue().rstrip()


def provider_test_payload(verification: ProviderVerification) -> dict[str, Any]:
    """Return a secret-free connectivity result."""

    connection = verification.connection
    return {
        "provider": connection.provider,
        "model": connection.model_name,
        "endpoint": connection.base_url,
        "catalog_endpoint": connection.catalog_endpoint,
        "authentication": connection.credential_description,
        "discovered_model_count": len(verification.models),
        "discovered_models": list(verification.models),
        "catalog_authoritative": verification.catalog_authoritative,
        "selected_model_available": verification.selected_model_available,
        "completion_attempted": verification.completion_attempted,
        "completion_verified": verification.completion_verified,
        "completion_error": verification.completion_error,
        "native_tool_probe_attempted": verification.native_tool_probe_attempted,
        "native_tool_probe_verified": verification.native_tool_probe_verified,
        "native_tool_probe_error": verification.native_tool_probe_error,
        "native_tool_probe_failure_kind": verification.native_tool_probe_failure_kind,
        "native_tool_probe_guidance": _NATIVE_TOOL_FAILURE_GUIDANCE.get(
            verification.native_tool_probe_failure_kind or ""
        ),
        "ok": verification.requested_checks_passed,
    }


def render_provider_test(
    verification: ProviderVerification,
    *,
    json_output: bool = False,
) -> str:
    """Render a successful provider verification."""

    payload = provider_test_payload(verification)
    if json_output:
        return json.dumps(payload, indent=2, sort_keys=True)
    connection = verification.connection
    if verification.completion_verified:
        completion = "verified"
    elif verification.completion_attempted:
        detail = verification.completion_error or "completion probe failed"
        completion = "failed: " + terminal_safe_text(detail, single_line=True)
    else:
        completion = "not attempted"
    if verification.native_tool_probe_verified:
        tool_probe = "verified (no tool executed)"
    elif verification.native_tool_probe_attempted:
        detail = verification.native_tool_probe_error or "no valid tool call returned"
        tool_probe = "failed: " + terminal_safe_text(detail, single_line=True)
    else:
        tool_probe = "not attempted"
    if verification.requested_checks_passed:
        result = "Result: ready to use"
    elif verification.ready_to_use:
        result = "Result: completion works, but native tool calling was not verified"
    elif (
        verification.catalog_authoritative and not verification.selected_model_available
    ):
        result = "Result: endpoint is reachable, but the selected model is unavailable"
    elif verification.catalog_authoritative:
        result = "Result: model is available, but a completion could not be verified"
    else:
        result = "Result: completion could not be verified"
    return "\n".join(
        [
            "Provider: "
            + terminal_safe_text(
                f"{connection.provider}/{connection.model_name}", single_line=True
            ),
            "Endpoint: "
            + terminal_safe_text(connection.base_url, single_line=True),
            "Authentication: "
            + terminal_safe_text(
                connection.credential_description, single_line=True
            ),
            (
                f"Catalog: {len(verification.models)} model(s); selected model "
                + (
                    "available"
                    if verification.selected_model_available
                    else "not returned"
                )
                if verification.catalog_authoritative
                else (
                    f"Catalog: {len(verification.models)} candidate model(s); "
                    "not authoritative for this route"
                )
            ),
            f"Completion: {completion}",
            f"Native tool protocol: {tool_probe}",
            *(
                ["  Next: " + _NATIVE_TOOL_FAILURE_GUIDANCE[verification.native_tool_probe_failure_kind]]
                if verification.native_tool_probe_failure_kind in _NATIVE_TOOL_FAILURE_GUIDANCE
                else []
            ),
            result,
            *(
                [
                    "Next: Run 'ash setup model' to update the saved route. If ASH_MODEL "
                    "is set or you passed a model argument to 'ash providers test', "
                    "correct that override instead (provider/model format). "
                    "Retest the corrected route."
                ]
                if (
                    verification.catalog_authoritative
                    and not verification.selected_model_available
                )
                else []
            ),
        ]
    )


def _redact_completion_error(
    exc: BaseException,
    verification: ProviderVerification,
) -> str:
    message = str(exc).strip() or type(exc).__name__
    api_key = verification.connection.api_key
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return redact_text(message)


async def _settle_probe_provider_close(provider: Any) -> tuple[BaseException | None, bool]:
    task = asyncio.create_task(
        provider.aclose(),
        name="ash-provider-completion-probe-close",
    )
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                continue
            interrupted = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except BaseException:
            break
    try:
        task.result()
    except BaseException as exc:
        return exc, interrupted
    return None, interrupted


async def _probe_provider_completion(
    config: "AshConfig",
    verification: ProviderVerification,
    *,
    timeout: float,
) -> tuple[bool, str | None]:
    from ash.providers.registry import get_provider_registry

    provider = None
    verified = False
    error: str | None = None
    primary_cancellation: asyncio.CancelledError | None = None
    try:
        provider = get_provider_registry().build(config)
        provider.configure_max_tokens(PROVIDER_TEST_MAX_TOKENS)
        saw_terminal = False
        terminal_stop_reason: str | None = None
        response_chars = 0
        response_parts: list[str] = []
        async with asyncio.timeout(timeout):
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": _PROVIDER_TEST_PROMPT}],
                temperature=0.0,
                tools=None,
            ):
                chunk_has_output = bool(
                    chunk.content or chunk.tool_call_delta or chunk.native_tool_calls
                )
                if saw_terminal and chunk_has_output:
                    raise RuntimeError(
                        "provider emitted output after its terminal completion"
                    )
                if chunk.tool_call_delta or chunk.native_tool_calls:
                    raise RuntimeError(
                        "provider completion probe returned an unexpected tool call"
                    )
                if chunk.content:
                    response_chars += len(chunk.content)
                    if response_chars > MAX_PROVIDER_TEST_RESPONSE_CHARS:
                        raise RuntimeError("provider completion probe response was too large")
                    response_parts.append(chunk.content)
                if chunk.is_done:
                    if not saw_terminal:
                        saw_terminal = True
                        terminal_stop_reason = chunk.stop_reason
                    elif chunk.stop_reason is not None:
                        next_category = completion_stop_category(chunk.stop_reason)
                        current_category = completion_stop_category(terminal_stop_reason)
                        if (
                            terminal_stop_reason is not None
                            and next_category != current_category
                        ):
                            raise RuntimeError(
                                "provider emitted conflicting terminal stop reasons"
                            )
                        if terminal_stop_reason is None:
                            terminal_stop_reason = chunk.stop_reason
        if not saw_terminal:
            raise RuntimeError("provider stream ended before a terminal completion")
        category = completion_stop_category(terminal_stop_reason)
        if category is not CompletionStopCategory.COMPLETE:
            detail = terminal_stop_reason or category.value
            raise RuntimeError(
                "provider completion probe did not finish normally: "
                f"{detail} ({category.value})"
            )
        if not "".join(response_parts).strip():
            raise RuntimeError("provider completion probe returned no text")
        verified = True
    except asyncio.CancelledError as cancellation:
        primary_cancellation = cancellation
        raise
    except TimeoutError:
        error = "provider completion probe timed out"
    except Exception as exc:  # noqa: BLE001 - convert provider failures into stage evidence
        error = _redact_completion_error(exc, verification)
    finally:
        if provider is not None:
            cleanup_error, cleanup_interrupted = await _settle_probe_provider_close(
                provider
            )
            if cleanup_error is not None:
                cleanup_text = _redact_completion_error(cleanup_error, verification)
                if primary_cancellation is not None:
                    primary_cancellation.add_note(
                        "provider completion probe cleanup failed: " + cleanup_text
                    )
                elif isinstance(cleanup_error, asyncio.CancelledError):
                    if error:
                        cleanup_error.add_note(
                            "provider completion probe failed before cleanup cancellation: "
                            + error
                        )
                    raise cleanup_error
                elif verified:
                    verified = False
                    error = "provider completion probe cleanup failed: " + cleanup_text
                elif error:
                    error += "; provider completion probe cleanup failed: " + cleanup_text
                else:
                    error = "provider completion probe cleanup failed: " + cleanup_text
            if cleanup_interrupted:
                if primary_cancellation is not None:
                    primary_cancellation.add_note(
                        "provider completion probe cleanup was interrupted by cancellation"
                    )
                else:
                    cleanup_cancellation = asyncio.CancelledError()
                    if error:
                        cleanup_cancellation.add_note(
                            "provider completion probe result before cancellation: " + error
                        )
                    raise cleanup_cancellation from cleanup_error
    return verified, error


async def _probe_native_tool_protocol(
    config: "AshConfig | None",
    verification: ProviderVerification,
    *,
    timeout: float,
    probe_provider: Any | None = None,
) -> NativeToolProbeResult:
    """Verify native tool-call encoding, without registering or running a tool."""

    from ash.providers.registry import get_provider_registry

    provider = probe_provider
    verified = False
    error: str | None = None
    failure_kind: NativeToolFailureKind | None = None
    primary_cancellation: asyncio.CancelledError | None = None
    try:
        if provider is None:
            if config is None:
                raise ValueError("provider config is required without a probe provider")
            provider = get_provider_registry().build(config)
        provider.configure_max_tokens(PROVIDER_NATIVE_TOOL_TEST_MAX_TOKENS)
        calls: list[CanonicalToolCall] = []
        response_chars = 0
        saw_terminal = False
        terminal_stop_reason: str | None = None
        async with asyncio.timeout(timeout):
            async for chunk in provider.stream_chat(
                [
                    {
                        "role": "user",
                        "content": (
                            "Call ash_diagnostic_echo once with value ASH_TOOL_OK. "
                            "Do not answer in text. This is a no-side-effect diagnostic."
                        ),
                    }
                ],
                temperature=0.0,
                tools=_NATIVE_PROBE_TOOLS,
            ):
                has_output = bool(
                    chunk.content or chunk.tool_call_delta or chunk.native_tool_calls
                )
                if saw_terminal and has_output:
                    failure_kind = "malformed_stream"
                    raise RuntimeError("provider emitted output after its terminal completion")
                if chunk.tool_call_delta:
                    failure_kind = "no_native_tool_call"
                    raise RuntimeError("provider returned text/XML instead of native tool calls")
                response_chars += len(chunk.content)
                if response_chars > MAX_PROVIDER_TEST_RESPONSE_CHARS:
                    failure_kind = "malformed_stream"
                    raise RuntimeError("provider native tool probe response was too large")
                if chunk.native_tool_calls:
                    calls.extend(chunk.native_tool_calls)
                    if len(calls) > 1:
                        failure_kind = "invalid_native_tool_call"
                        raise RuntimeError("provider returned more than one diagnostic tool call")
                if chunk.is_done:
                    if not saw_terminal:
                        saw_terminal = True
                        terminal_stop_reason = chunk.stop_reason
                    elif chunk.stop_reason is not None and (
                        completion_stop_category(chunk.stop_reason)
                        != completion_stop_category(terminal_stop_reason)
                    ):
                        failure_kind = "malformed_stream"
                        raise RuntimeError("provider emitted conflicting terminal stop reasons")

        if not saw_terminal:
            failure_kind = "malformed_stream"
            raise RuntimeError("provider stream ended before a terminal completion")
        category = completion_stop_category(terminal_stop_reason)
        if category is not CompletionStopCategory.COMPLETE:
            failure_kind = (
                "output_truncated"
                if category is CompletionStopCategory.TRUNCATED
                else "provider_error"
            )
            raise RuntimeError(
                "provider native tool probe stopped: " + (terminal_stop_reason or category.value)
            )
        if not calls:
            failure_kind = "no_native_tool_call"
            raise RuntimeError("provider returned text or no tool calls instead of a native call")
        if len(calls) != 1 or calls[0].name != _NATIVE_PROBE_TOOL_NAME or (
            calls[0].arguments != {"value": _NATIVE_PROBE_VALUE}
        ):
            failure_kind = "invalid_native_tool_call"
            raise RuntimeError("provider did not return the requested native diagnostic tool call")
        verified = True
    except asyncio.CancelledError as cancellation:
        primary_cancellation = cancellation
        raise
    except TimeoutError:
        error = "provider native tool probe timed out"
        failure_kind = "timeout"
    except Exception as exc:  # noqa: BLE001 - probe errors become diagnostic evidence
        error = _redact_completion_error(exc, verification)
        if failure_kind is None:
            failure_kind = "malformed_stream" if isinstance(exc, ValidationError) else "provider_error"
    finally:
        if provider is not None:
            cleanup_error, cleanup_interrupted = await _settle_probe_provider_close(provider)
            if cleanup_error is not None:
                cleanup_text = _redact_completion_error(cleanup_error, verification)
                if primary_cancellation is not None:
                    primary_cancellation.add_note(
                        "native tool probe cleanup failed: " + cleanup_text
                    )
                elif isinstance(cleanup_error, asyncio.CancelledError):
                    raise cleanup_error
                else:
                    verified = False
                    if failure_kind is None:
                        failure_kind = "cleanup_error"
                    error = (error + "; " if error else "") + (
                        "provider native tool probe cleanup failed: " + cleanup_text
                    )
            if cleanup_interrupted and primary_cancellation is None:
                raise asyncio.CancelledError() from cleanup_error
    return NativeToolProbeResult(verified, error, failure_kind)


def probe_local_native_tool_protocol(
    provider_name: str,
    model: str,
    base_url: str,
    *,
    timeout: float = 60.0,
) -> tuple[bool, str | None]:
    """Probe the selected local endpoint before setup persists a new route."""
    from ash.providers.openai import OpenAIProvider
    from ash.providers.readiness import ProviderConnection

    if provider_name not in {"lmstudio", "vllm"}:
        raise ValueError("native tool setup probe supports only lmstudio and vllm")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("native tool probe timeout must be positive and finite")
    connection = ProviderConnection(
        provider=provider_name,
        model_name=model,
        base_url=base_url,
        catalog_endpoint="",
        catalog_format="openai",
        auth_mode="none",
    )
    probe = OpenAIProvider(model_name=model, base_url=base_url, allow_anonymous=True)
    verification = ProviderVerification(
        connection=connection,
        models=(model,),
        selected_model_available=True,
    )
    result = asyncio.run(
        _probe_native_tool_protocol(
            None, verification, timeout=timeout, probe_provider=probe
        )
    )
    guidance = _NATIVE_TOOL_FAILURE_GUIDANCE.get(result.failure_kind or "")
    detail = result.error
    if detail and guidance:
        detail += "; " + guidance
    return result.verified, detail


def test_provider(
    config: "AshConfig",
    *,
    model: str | None = None,
    timeout: float = 10.0,
    tools: bool = False,
) -> ProviderVerification:
    """Verify one route, optionally overriding the active model for the probe."""

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("provider test timeout must be positive and finite")
    test_config = (
        config.model_copy(update={"model": model, "fallback_models": []})
        if model
        else config.model_copy(update={"fallback_models": []})
    )
    from ash.providers.identifiers import parse_model_string

    provider_name, _model_name = parse_model_string(test_config.model)
    if tools and provider_name not in {"vllm", "lmstudio"}:
        raise ValueError("--tools is supported only for vllm and lmstudio local routes")
    if provider_name in {"vertex", "bedrock", "azure"}:
        connection = resolve_provider_connection(test_config)
        verification = ProviderVerification(
            connection=connection,
            models=(connection.model_name,),
            selected_model_available=False,
            catalog_authoritative=False,
        )
    else:
        verification = asyncio.run(
            verify_provider_catalog(test_config, timeout=timeout)
        )
    if (
        verification.catalog_authoritative
        and not verification.selected_model_available
    ):
        return verification
    completion_verified, completion_error = asyncio.run(
        _probe_provider_completion(
            test_config,
            verification,
            timeout=timeout,
        )
    )
    verification = replace(
        verification,
        completion_attempted=True,
        completion_verified=completion_verified,
        completion_error=completion_error,
    )
    if not tools or not completion_verified:
        return verification
    native_result = asyncio.run(
        _probe_native_tool_protocol(test_config, verification, timeout=timeout)
    )
    return replace(
        verification,
        native_tool_probe_attempted=True,
        native_tool_probe_verified=native_result.verified,
        native_tool_probe_error=native_result.error,
        native_tool_probe_failure_kind=native_result.failure_kind,
    )


def provider_test_error(exc: Exception, *, json_output: bool = False) -> str:
    """Render a stable, secret-free provider test failure."""

    message = redact_text(str(exc))
    if json_output:
        return json.dumps({"ok": False, "error": message}, sort_keys=True)
    return "Provider test failed: " + terminal_safe_text(message, single_line=True)


__all__ = [
    "ProviderConfigurationError",
    "ProviderVerificationError",
    "provider_catalog_payload",
    "provider_test_error",
    "render_provider_catalog",
    "render_provider_test",
    "test_provider",
    "verify_provider_catalog",
]
