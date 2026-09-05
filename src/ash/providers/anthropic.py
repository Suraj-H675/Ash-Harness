"""Anthropic Claude provider adapter for Ash.

The real adapter uses the official ``anthropic`` SDK to stream completions.
The SDK is optional at runtime: if it is not importable, the adapter
raises :class:`ProviderBackendUnavailable` on construction so the loop can
surface a clear error to the user instead of crashing mid-turn.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, AsyncGenerator

from ash.context.tokens import AnthropicTokenCounter
from ash.providers.base import ProviderABC, StreamChunk, TokenCounterLike
from ash.providers.messages import CanonicalToolCall, MessageInput, normalize_messages
from ash.providers.readiness import ProviderConfigurationError


class ProviderBackendUnavailable(ImportError):
    """Raised when the optional ``anthropic`` SDK is not installed."""


def prepare_anthropic_messages(
    messages: Sequence[MessageInput],
) -> tuple[str, list[dict[str, Any]]]:
    """Translate Ash's canonical history to Anthropic message blocks."""

    system_blocks: list[str] = []
    conversation: list[dict[str, Any]] = []
    for message in normalize_messages(messages):
        role = message.get("role")
        content = message.get("content", "")
        if role == "system":
            system_blocks.append(str(content))
            continue
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call["call_id"],
                    "name": call["name"],
                    "input": call.get("arguments", {}),
                }
                for call in message["tool_calls"]
            )
            conversation.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                "content": str(content),
            }
            if (
                conversation
                and conversation[-1]["role"] == "user"
                and isinstance(conversation[-1]["content"], list)
                and all(
                    item.get("type") == "tool_result"
                    for item in conversation[-1]["content"]
                )
            ):
                conversation[-1]["content"].append(block)
            else:
                conversation.append({"role": "user", "content": [block]})
            continue
        conversation.append(
            {"role": role, "content": _prepare_anthropic_content(content)}
        )
    return "\n\n".join(system_blocks), conversation


def _prepare_anthropic_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    prepared: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            prepared.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": str(block.get("media_type", "")),
                        "data": str(block.get("data", "")),
                    },
                }
            )
        elif block.get("type") == "text":
            prepared.append({"type": "text", "text": str(block.get("text", ""))})
    return prepared


def prepare_anthropic_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Translate OpenAI-style function declarations to Anthropic tools."""

    if not tools:
        return None
    return [
        {
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "input_schema": tool["function"].get(
                "parameters", {"type": "object", "properties": {}}
            ),
        }
        for tool in tools
    ]


class AnthropicProvider(ProviderABC):
    provider_family = "anthropic"
    """Streaming adapter for Anthropic Claude models.

    The adapter is constructed with a model name, an API key, and an
    optional pre-built async client. Tests can inject a fake client to
    drive the loop without hitting the network. When ``client`` is
    ``None``, the adapter lazily builds an :class:`anthropic.AsyncAnthropic`
    from ``api_key``. With no explicit ``base_url``, an empty key retains the
    SDK's ``ANTHROPIC_API_KEY`` environment fallback; custom endpoints require
    an explicit key to prevent credentials being redirected unexpectedly.
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        *,
        base_url: str | None = None,
        client: Any | None = None,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url
        self._client = client
        self._owns_client = client is None
        self._token_counter = token_counter or AnthropicTokenCounter()
        self._prompt_cache_enabled = False
        self._prompt_cache_retention = "memory"

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._base_url and not self._api_key:
            raise ProviderConfigurationError(
                "Anthropic API key is required when using a custom base URL; "
                "pass api_key explicitly (ambient ANTHROPIC_API_KEY is not used)."
            )

        try:
            from anthropic import AsyncAnthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on host env
            raise ProviderBackendUnavailable(
                "The 'anthropic' package is not installed. "
                "Install it with `pip install anthropic` or inject a client."
            ) from exc

        kwargs: dict[str, Any] = {"max_retries": 0}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        client = self._resolve_client()

        system_prompt, conversation = prepare_anthropic_messages(messages)

        request_kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": conversation,
            "temperature": temperature,
        }
        if system_prompt:
            request_kwargs["system"] = system_prompt
        anthropic_tools = prepare_anthropic_tools(tools)
        if anthropic_tools:
            request_kwargs["tools"] = anthropic_tools
        if self._prompt_cache_enabled:
            cache_control = {"type": "ephemeral"}
            if self._prompt_cache_retention == "extended":
                cache_control["ttl"] = "1h"
            request_kwargs["cache_control"] = cache_control

        # Cap output to keep runaway generations bounded; the spec exposes
        # ``max_completion_tokens`` on the AshConfig for the loop to wire.
        max_tokens = getattr(self, "_max_tokens", None)
        if max_tokens:
            request_kwargs["max_tokens"] = max_tokens

        async with client.messages.stream(**request_kwargs) as stream:
            async for text in stream.text_stream:
                if text:
                    yield StreamChunk(content=text, model=self._model_name)

            final_message = await stream.get_final_message()
            usage = getattr(final_message, "usage", None)
            uncached_input_tokens = getattr(usage, "input_tokens", 0) or 0
            cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
            stop_reason = getattr(final_message, "stop_reason", None)
            native_tool_calls: list[CanonicalToolCall] = []
            reasoning_blocks: list[dict[str, Any]] = []
            for block in getattr(final_message, "content", []) or []:
                if getattr(block, "type", None) == "tool_use":
                    native_tool_calls.append(
                        CanonicalToolCall(
                            call_id=block.id,
                            name=block.name,
                            arguments=block.input,
                        )
                    )
                elif getattr(block, "type", None) in {
                    "thinking",
                    "redacted_thinking",
                    "web_search_tool_result",
                }:
                    reasoning_blocks.append(
                        {
                            "type": block.type,
                            **(
                                {"thinking": str(block.thinking)[:20_000]}
                                if getattr(block, "thinking", None)
                                else {}
                            ),
                            **(
                                {"data": getattr(block, "data", "")[:20_000]}
                                if getattr(block, "type", None) == "redacted_thinking"
                                else {}
                            ),
                            **(
                                {"content": getattr(block, "content", [])}
                                if block.type == "web_search_tool_result"
                                else {}
                            ),
                        }
                    )
            yield StreamChunk(
                content="",
                is_done=True,
                prompt_tokens=(
                    uncached_input_tokens + cache_read_tokens + cache_write_tokens
                ),
                completion_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                usage_source="provider" if usage is not None else "unavailable",
                stop_reason=stop_reason,
                model=self._model_name,
                native_tool_calls=native_tool_calls or None,
                reasoning=reasoning_blocks or None,
            )

    def configure_max_tokens(self, max_tokens: int) -> None:
        """Set the per-completion cap. Call before streaming."""

        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens

    def configure_prompt_cache(
        self,
        *,
        enabled: bool,
        retention: str = "memory",
    ) -> None:
        if retention not in {"memory", "extended"}:
            raise ValueError("prompt cache retention must be memory or extended")
        self._prompt_cache_enabled = enabled
        self._prompt_cache_retention = retention

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            close = getattr(self._client, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            self._client = None
