"""Provider catalog and connectivity commands."""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import replace
from io import StringIO
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from ash.core.redaction import redact_text
from ash.provider_catalog import BUILTIN_PROVIDERS
from ash.providers.base import CompletionStopCategory, completion_stop_category
from ash.providers.readiness import (
    ProviderConfigurationError,
    ProviderVerification,
    ProviderVerificationError,
    verify_provider_connection,
)
from ash.ui.safe_text import terminal_safe_text

if TYPE_CHECKING:
    from ash.config import AshConfig


PROVIDER_TEST_MAX_TOKENS = 8
MAX_PROVIDER_TEST_RESPONSE_CHARS = 4096
_PROVIDER_TEST_PROMPT = "Reply with exactly OK."


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
                "auth": "none" if descriptor.local else "api-key",
                "key_env": descriptor.key_env,
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
    table.add_column("Authentication")
    table.add_column("Default endpoint")
    for descriptor in BUILTIN_PROVIDERS:
        table.add_row(
            descriptor.id,
            descriptor.category,
            descriptor.protocol,
            "none" if descriptor.local else descriptor.key_env or "api key",
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
        "selected_model_available": verification.selected_model_available,
        "completion_attempted": verification.completion_attempted,
        "completion_verified": verification.completion_verified,
        "completion_error": verification.completion_error,
        "ok": verification.ready_to_use,
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
    selected = "available" if verification.selected_model_available else "not returned"
    connection = verification.connection
    if verification.completion_verified:
        completion = "verified"
    elif verification.completion_attempted:
        detail = verification.completion_error or "completion probe failed"
        completion = "failed: " + terminal_safe_text(detail, single_line=True)
    else:
        completion = "not attempted"
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
            f"Catalog: {len(verification.models)} model(s); selected model {selected}",
            f"Completion: {completion}",
            (
                "Result: ready to use"
                if verification.ready_to_use
                else "Result: endpoint is reachable, but the selected model is unavailable"
                if not verification.selected_model_available
                else "Result: model is available, but a completion could not be verified"
            ),
        ]
    )


def _redact_completion_error(
    exc: Exception,
    verification: ProviderVerification,
) -> str:
    message = str(exc).strip() or type(exc).__name__
    api_key = verification.connection.api_key
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return redact_text(message)


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
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        error = "provider completion probe timed out"
    except Exception as exc:  # noqa: BLE001 - convert provider failures into stage evidence
        error = _redact_completion_error(exc, verification)
    finally:
        if provider is not None:
            try:
                await provider.aclose()
            except Exception as exc:  # noqa: BLE001 - cleanup is part of the explicit probe
                if verified:
                    verified = False
                    error = "provider completion probe cleanup failed: " + _redact_completion_error(
                        exc, verification
                    )
    return verified, error


def test_provider(
    config: "AshConfig",
    *,
    model: str | None = None,
    timeout: float = 10.0,
) -> ProviderVerification:
    """Verify one route, optionally overriding the active model for the probe."""

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("provider test timeout must be positive and finite")
    test_config = (
        config.model_copy(update={"model": model, "fallback_models": []})
        if model
        else config.model_copy(update={"fallback_models": []})
    )
    verification = verify_provider_connection(test_config, timeout=timeout)
    if not verification.selected_model_available:
        return verification
    completion_verified, completion_error = asyncio.run(
        _probe_provider_completion(
            test_config,
            verification,
            timeout=timeout,
        )
    )
    return replace(
        verification,
        completion_attempted=True,
        completion_verified=completion_verified,
        completion_error=completion_error,
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
]
